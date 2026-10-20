"""Committed, resumable migration work. A test session is deliberately persistent.

The checkpoint lives in InnoDB, in the same transaction as the records. Files
and Redis are presentation/control only, never evidence that work committed.
Test sessions never grant the live-import rehearsal authorization.
"""

import datetime
import json
import os
import time

import frappe
from frappe.utils import today

from onerc_vmms_migrate import accounts, historical, load, workspace
from onerc_vmms_migrate.silence import Guard, SilenceBreached, silenced
from onerc_vmms_migrate.transaction import large_transaction

DOCTYPE = "VMMS Migration Batch State"
PHASES = (
	"preflight",
	"archive",
	"accounts",
	"people",
	"history",
	"files",
	"embedded_files",
	"avatars",
	"people_verification",
	"verification",
)
PUBLIC = (
	"session_id",
	"mode",
	"phase",
	"offset",
	"total",
	"batch_number",
	"batch_size",
	"status",
	"failed",
	"passed",
	"updated_at",
	"run_id",
	"last_batch",
	"error",
	"versions_reviewed",
)


def validate_size(value):
	if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
		raise ValueError("Batch size must be an integer from 1 to 1000.")
	return value


def checkpoint():
	if not frappe.db.exists("DocType", DOCTYPE):
		return None
	raw = frappe.db.get_single_value(DOCTYPE, "checkpoint")
	return json.loads(raw) if isinstance(raw, str) and raw else None


def commit_checkpoint(value):
	value["updated_at"] = str(frappe.utils.now_datetime())
	frappe.db.set_single_value(DOCTYPE, "checkpoint", json.dumps(value, default=str))
	frappe.db.commit()


def public_state(value):
	return {key: value[key] for key in PUBLIC if key in value}


def advance(value, processed, total, failures):
	if not processed and total is not None and value["offset"] < total and not failures:
		raise ValueError("The source page made no progress; its advertised record count needs review.")
	value.update(total=total, failed=failures)
	value["last_batch"] = {"processed": processed, "failed": failures, "offset": value["offset"]}
	if failures:
		value["status"] = "blocked"
		return
	value["offset"] += processed
	value["batch_number"] += 1
	value["status"] = "ready"
	if total is not None and value["offset"] >= total and value["phase"] != "verification":
		value["phase"] = PHASES[PHASES.index(value["phase"]) + 1]
		value["offset"] = 0
		value["total"] = None


def history_page(snapshot, start, size):
	store = snapshot.get("history", {})
	if hasattr(store, "page"):
		return store.page(start, size)
	return [store[key] for key in sorted(store)[start : start + size]]


def archive_page(snapshot, start, size):
	history_total = len(snapshot.get("history", {}))
	result = history_page(snapshot, start, size) if start < history_total else []
	if len(result) < size:
		users = snapshot.get("users", {})
		user_start = max(0, start - history_total)
		result.extend(
			{"doctype": "User", "record": users[key]}
			for key in sorted(users)[user_start : user_start + size - len(result)]
		)
	return result


def build(snapshot, conf, crosswalks=None):
	from onerc_vmms_migrate import transform
	from onerc_vmms_migrate.crosswalk import Crosswalks

	return transform.build(
		snapshot,
		crosswalks if crosswalks is not None else Crosswalks.read(),
		conf,
		datetime.date.fromisoformat(today()),
	)


def reference_failures(crosswalks):
	return [{**row, "error": row["problem"]} for row in crosswalks.problems() if row["problem"] != "unmapped"]


def excluded_identities(snapshot, built):
	from onerc_vmms_migrate import normalise

	snapshot["excluded_native_identities"] = {
		(dt, name)
		for bundle in built["bundles"]
		for dt, name in bundle["sources"]
		if dt != "User" and (bundle["errors"] or (dt == "Employee" and bundle.get("volunteer_excluded")))
	}
	snapshot["excluded_native_identities"].update(
		("User", r["name"])
		for r in snapshot["users"].values()
		if r["name"] not in ("Administrator", "Guest")
		and not (normalise.email(r.get("email")) or normalise.email(r["name"]))
	)


def mark_person(snapshot, bundle, written, run_id):
	targets = {
		"User": ("User", written["user"]),
		"Employee": ("VMMS Volunteer", written["volunteer"]),
		"VM Member": ("VMMS Member", written["member"]),
	}
	for dt, name in bundle["sources"]:
		target_dt, target = targets.get(dt, (None, None))
		if target:
			historical.mark(snapshot, dt, name, target_dt, target, run_id)
	for membership in bundle["memberships"]:
		target = frappe.db.get_value(
			load.LEDGER,
			{
				"source_doctype": "VM Membership",
				"source_name": membership["source_name"],
				"target_doctype": "VMMS Membership",
			},
			"target_name",
		)
		if target:
			historical.mark(
				snapshot, "VM Membership", membership["source_name"], "VMMS Membership", target, run_id
			)


def people(snapshot, source, conf, value, stop, progress):
	built = build(snapshot, conf)
	bundles = sorted(built["bundles"], key=lambda row: row["key"])
	selected = bundles[value["offset"] : value["offset"] + value["batch_size"]]
	failures = []
	phone_review = []
	processed = loaded = existing = rejected = 0
	for bundle in selected:
		if processed and stop():
			break
		processed += 1
		if bundle["errors"]:
			rejected += 1
			continue
		done = load.already_migrated(bundle["key"])
		if done:
			if done.source_hash != bundle["source_hash"]:
				failures.append(
					{
						"person": bundle["key"],
						"error": "Source or mapped person changed after a committed batch. Restore/review; existing data was not overwritten.",
					}
				)
			else:
				existing += 1
			continue
		frappe.db.savepoint("vmms_batch_person")
		try:
			written = load.load_bundle(bundle, source, conf, value["run_id"], dry_run=value["mode"] == "test")
			mark_person(snapshot, bundle, written, value["run_id"])
			phone_review.extend({"person": bundle["key"], **row} for row in written.get("phone_review", []))
			loaded += 1
		except SilenceBreached:
			raise
		except Exception as exc:
			frappe.db.rollback(save_point="vmms_batch_person")
			frappe.clear_messages()
			failures.append({"person": bundle["key"], "error": frappe.utils.strip_html(str(exc))})
		if progress and processed % 25 == 0:
			progress(
				{
					"phase": "people",
					"processed": value["offset"] + processed,
					"total": len(bundles),
					"loaded": loaded,
					"existing": existing,
					"failed": len(failures),
				}
			)
	return processed, len(bundles), failures, {"loaded": loaded, "existing": existing, "rejected": rejected, "phone_review": phone_review}


def work(snapshot, source, conf, value, progress):
	"""One bounded unit; transaction ownership belongs to step()."""
	from onerc_vmms_migrate import full_run

	phase, start, size = value["phase"], value["offset"], value["batch_size"]
	deadline = time.monotonic() + 240

	def stop():
		return time.monotonic() >= deadline

	if phase == "preflight":
		from onerc_vmms_migrate import run
		from onerc_vmms_migrate.crosswalk import Crosswalks

		cw = Crosswalks.read()
		built = build(snapshot, conf, crosswalks=cw)
		summary = run._report_plan(value["run_id"], cw, built)
		invalid = [
			{
				"person": bundle["key"],
				"error": "Phone fails native destination validation. Correct/review it or explicitly allow phone values requiring review in migration settings.",
			}
			for bundle in built["bundles"]
			if not bundle["errors"]
			and bundle["profile"].get("phone")
			and not frappe.utils.validate_phone_number(bundle["profile"]["phone"])
		]
		failures = reference_failures(cw)
		summary["missing_reference_targets"] = len(failures)
		if not conf.get("drop_invalid_phones"):
			failures.extend(invalid)
		summary["native_phones_requiring_review"] = len(invalid)
		return 1, 1, failures, summary
	if phase == "archive":
		rows = archive_page(snapshot, start, size)
		summary = historical.archive(snapshot, value["run_id"], records=rows)
		return len(rows), len(snapshot["history"]) + len(snapshot["users"]), [], summary
	if phase == "accounts":
		keys = sorted(snapshot["users"])[start : start + size]
		part = {**snapshot, "users": {key: snapshot["users"][key] for key in keys}}
		summary = accounts.transfer(part, conf, value["run_id"], progress=None, stop=stop)
		failures = summary.pop("failed")
		return summary.pop("processed"), len(snapshot["users"]), failures, summary
	if phase == "people":
		return people(snapshot, source, conf, value, stop, progress)
	if phase == "history":
		from onerc_vmms_migrate.history_load import Loader

		excluded_identities(snapshot, build(snapshot, conf))
		summary = Loader(snapshot, value["run_id"], source, conf, dry_run=value["mode"] == "test").run_batch(
			size, deadline, progress
		)
		failures = summary.pop("failures")
		processed = summary["attempted"]
		# History adapters resolve dependencies in native order. Completed
		# source identities are skipped by the archive conversion index.
		total = start + processed if summary["complete"] else None
		return processed, total, failures, summary
	if phase in ("files", "embedded_files", "avatars"):
		manifest = snapshot.get("all_file_manifest", snapshot.get("captured_file_manifest", {}))
		rows, users = {}, {}
		if phase == "files":
			selected = sorted(manifest)[start : start + size]
			manifest = {key: manifest[key] for key in selected}
			total = len(snapshot.get("all_file_manifest", snapshot.get("captured_file_manifest", {})))
		elif phase == "embedded_files":
			selected = history_page(snapshot, start, size)
			rows = {str(i): row for i, row in enumerate(selected)}
			total = len(snapshot["history"])
		else:
			selected = sorted(snapshot["users"])[start : start + size]
			users = {key: snapshot["users"][key] for key in selected}
			total = len(snapshot["users"])
		# Embedded ownership and avatars use the full manifest, but do not
		# re-verify all captured bytes for each page.
		part = {**snapshot, "history": rows, "users": users, "all_file_manifest": manifest}
		summary = full_run.attachments(
			part, source, value["run_id"], dry_run=value["mode"] == "test", verify_manifest=phase == "files"
		)
		return len(selected), total, [], summary
	if phase == "people_verification":
		from onerc_vmms_migrate import run

		bundles = sorted(build(snapshot, conf)["bundles"], key=lambda row: row["key"])
		selected = bundles[start : start + size]
		summary = run.reconcile(bundles=selected)
		failures = (
			[
				{
					"problem": "People field reconciliation failed; review the reconciliation report",
					"count": summary["mismatches"],
				}
			]
			if summary["mismatches"]
			else []
		)
		failures.extend(
			{"person": row["key"], "problem": "Ready person has no committed migration ledger"}
			for row in selected
			if not row["errors"] and not load.already_migrated(row["key"])
		)
		return len(selected), len(bundles), failures, summary
	if phase == "verification":
		return verify(snapshot, conf, value)
	raise ValueError("Unknown batch phase.")


def verify(snapshot, conf, value):
	from onerc_vmms_migrate import full_reconcile, login_policy, normalise, run
	from onerc_vmms_migrate.crosswalk import Crosswalks
	from onerc_vmms_migrate.policy import source_exclusions_accepted

	summary, failures = full_reconcile.verify(snapshot)
	cw = Crosswalks.read()
	built = build(snapshot, conf, crosswalks=cw)
	run._report_plan(value["run_id"], cw, built)
	failures.extend(reference_failures(cw))
	if not any(not bundle["errors"] for bundle in built["bundles"]):
		failures.append({"problem": "There are no ready people to verify"})
	rejected = sum(bool(bundle["errors"]) for bundle in built["bundles"])
	accounts_excluded = sum(
		not (normalise.email(row.get("email")) or normalise.email(row["name"]))
		for row in snapshot["users"].values()
		if row["name"] not in ("Administrator", "Guest")
	)
	if not source_exclusions_accepted(
		rejected,
		len(built["bundles"]),
		accounts_excluded,
		len(snapshot["users"]),
		bool(conf.get("allow_source_exclusions")),
	):
		failures.append(
			{"problem": "Source identity exclusions require review or exceed the permitted limit"}
		)
	if snapshot.get("capture_complete") is False and not conf.get("allow_missing_files"):
		failures.append({"problem": "Attachment exceptions require review and explicit approval"})
	# Re-run the idempotent adapters to account for every history exclusion.
	from onerc_vmms_migrate.history_load import Loader

	excluded_identities(snapshot, built)
	history = Loader(snapshot, value["run_id"], None, conf, dry_run=value["mode"] == "test").run_batch(
		1000, time.monotonic() + 240
	)
	failures.extend(history["failures"])
	if not history["complete"] or history["attempted"]:
		raise ValueError("Unconverted history remains. Resume the history phase before verification.")
	summary["history"] = {
		key: history[key] for key in ("converted", "excluded_count", "by_source", "excluded_by_source")
	}
	volunteer_exclusions = sum(bool(bundle.get("volunteer_excluded")) for bundle in built["bundles"])
	volunteer_total = sum(any(dt == "Employee" for dt, _ in bundle["sources"]) for bundle in built["bundles"])
	if volunteer_exclusions and not (
		conf.get("allow_source_exclusions") and volunteer_exclusions / max(1, volunteer_total) <= 0.01
	):
		failures.append(
			{"problem": "Volunteer source exclusions require review or exceed the permitted limit"}
		)
	if history["excluded_count"] and not (
		conf.get("allow_source_exclusions")
		and history["excluded_count"] / max(1, history["converted"] + history["excluded_count"]) <= 0.01
	):
		failures.append({"problem": "History source exclusions require review or exceed the permitted limit"})
	directory = workspace.report_dir(value["run_id"])
	run._write_csv(directory, "history-source-exclusions.csv", history["exclusions"])
	run._write_csv(directory, "history-warnings.csv", history["warnings"])
	value["passed"] = not failures
	if value["passed"] and value["mode"] == "import":
		login_policy.apply(value["authentication_policy"])
	return 1, 1, failures, summary


@large_transaction
def step(value, source, conf, progress=None):
	"""One commit includes successes AND their resumable cursor.

	Validation failures stop automatic sequencing. Successes in the same batch
	remain committed; a retry uses the native ledgers to revisit only failures.
	Unexpected errors/timeouts roll the entire uncommitted batch back.
	"""
	guard = Guard()
	with silenced():
		processed, total, failures, summary = work(source.data, source, conf, value, progress)
		guard.check()
	phase = value["phase"]
	value["attempt_number"] = value.get("attempt_number", 0) + 1
	directory = workspace.report_dir(value["run_id"])
	from onerc_vmms_migrate import run

	name = f"batch-{value['attempt_number']:06d}-{phase}"
	run._write_csv(directory, name + "-failures.csv", failures)
	if summary.get("phone_review"):
		run._write_csv(directory, name + "-phone-review.csv", summary["phone_review"])
	if summary.get("warnings"):
		run._write_csv(directory, name + "-warnings.csv", summary["warnings"])
	if summary.get("exclusions"):
		run._write_csv(directory, name + "-exclusions.csv", summary["exclusions"])
	workspace.write_json(
		os.path.join(directory, name + ".json"),
		{
			"phase": phase,
			"offset": value["offset"],
			"processed": processed,
			"total": total,
			"failed": len(failures),
			"summary": summary,
			"fingerprint": value["fingerprint"],
		},
	)
	advance(value, processed, total, len(failures))
	if phase == "verification" and not failures:
		value["status"] = "finished"
		value["verification"] = summary
	commit_checkpoint(value)
	workspace.write_json(os.path.join(directory, "batch-session.json"), value)
	return value
