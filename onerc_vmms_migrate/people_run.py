"""Production people batches: no trial prerequisite and no operational history.

A page of people commits before its file plan. Each file page has its own
transaction/cursor. The first people page pauses after its files; an operator
may then resume continuously. A later capture uses the same immutable ledger.
"""

import hashlib
import itertools
import os
import time

import frappe
from onerc_vmms_migrate import batches, historical, load, workspace
from onerc_vmms_migrate.crosswalk import Crosswalks
from onerc_vmms_migrate.silence import SilenceBreached


def source_identity(snapshot):
	return str(snapshot["source"].get("source_id") or snapshot["source"].get("db_name") or "")


def check_source(expected, snapshot):
	actual = source_identity(snapshot)
	if not actual or expected and actual != expected:
		raise ValueError(
			"This capture belongs to a different source. Existing people and identity mappings were preserved."
		)
	return actual


def core_records(snapshot, bundle):
	groups = {"User": "users", "Employee": "employees", "Job Applicant": "applicants", "VM Member": "members"}
	store = snapshot.get("history", {})

	def original(dt, name):
		if hasattr(store, "get_record"):
			return store.get_record(dt, name) or {}
		from onerc_vmms_migrate.history_model import key

		return store.get(key(dt, name), {}).get("record", {})

	return [
		{"doctype": dt, "record": {**original(dt, name), **snapshot[groups[dt]][name]}}
		for dt, name in bundle["sources"]
		if dt in groups
	] + [
		{"doctype": "VM Membership", "record": snapshot["memberships"][row["source_name"]]}
		for row in bundle["memberships"]
	]


def unchanged(ledger, bundle):
	return ledger["source_hash"] == bundle["source_hash"] and (
		not ledger.get("source_evidence_hash")
		or ledger["source_evidence_hash"] == bundle.get("source_evidence_hash")
	)


def committed_person(bundle):
	done = load.already_migrated(bundle["key"])
	if done:
		return done
	# Email is mutable. An original Employee/member/application ID still proves
	# this is an existing imported person when the source changes the address.
	for dt, name in bundle.get("sources", []):
		row = frappe.db.get_value(
			load.LEDGER,
			{"source_doctype": dt, "source_name": name},
			["person_key", "source_hash"],
			as_dict=True,
		)
		if row and row.get("person_key") and not row["person_key"].startswith("account:"):
			prior = load.already_migrated(row["person_key"])
			if prior:
				return prior
	return None


def import_people(snapshot, source, conf, value, selected, progress=None):
	report = {
		"loaded": 0,
		"existing": 0,
		"rejected": 0,
		"changed": [],
		"failures": [],
		"phone_review": [],
		"held": [],
		"review_values": [],
		"password_restorations": [],
		"processed": 0,
	}
	deadline = time.monotonic() + 240
	for bundle in selected:
		if report["processed"] and time.monotonic() >= deadline:
			break
		report["processed"] += 1
		if bundle["errors"]:
			report["rejected"] += 1
			report["held"].extend({"person": bundle["key"], "reason": error} for error in bundle["errors"])
			continue
		done = committed_person(bundle)
		if done:
			report["existing"] += 1
			if not unchanged(done, bundle):
				report["changed"].append(
					{
						"person": bundle["key"],
						"previous_source_hash": done["source_hash"],
						"captured_source_hash": bundle["source_hash"],
						"action": "Reported only; no existing person or files overwritten",
					}
				)
			continue
		frappe.db.savepoint("people_record")
		try:
			historical.archive(snapshot, value["run_id"], records=core_records(snapshot, bundle))
			written = load.load_bundle(bundle, source, {**conf, "defer_profile_files": True}, value["run_id"])
			batches.mark_person(snapshot, bundle, written, value["run_id"])
			report["password_restorations"].extend(written.get("password_restorations", []))
			report["phone_review"].extend(
				{"person": bundle["key"], **row} for row in written.get("phone_review", [])
			)
			report["review_values"].extend(
				{"person": bundle["key"], **row} for row in written.get("review_values", [])
			)
			report["loaded"] += 1
		except SilenceBreached:
			raise
		except Exception as exc:
			frappe.db.rollback(save_point="people_record")
			frappe.clear_messages()
			report["failures"].append({"person": bundle["key"], "error": frappe.utils.strip_html(str(exc))})
	return report


def work(snapshot, source, conf, value, progress=None):
	from onerc_vmms_migrate import run
	from onerc_vmms_migrate.extract import enrich
	from onerc_vmms_migrate.continuity import require_capture

	enrich(snapshot)
	require_capture(snapshot)
	check_source(value.get("source_id"), snapshot)
	phase, start, size = value["phase"], value["offset"], value["batch_size"]
	directory = workspace.report_dir(value["run_id"])
	if phase == "preflight":
		from onerc_vmms_migrate.file_transfer import assert_supported

		assert_supported()  # Runs on the hosted worker before any business insert.
		built = batches.build(snapshot, conf)
		cw = Crosswalks.read()
		from onerc_vmms_migrate import people_reports

		people_reports.write(snapshot, built, value["run_id"])
		run._report_plan(value["run_id"], cw, built)
		from onerc_vmms_migrate.naming_continuity import reserve_people

		value["people_naming"] = reserve_people(snapshot)
		value["people_total"] = len(built["bundles"])
		return (
			1,
			1,
			[],
			{
				"people": value["people_total"],
				"file_copy_supported": True,
				"frappe_version": getattr(frappe, "__version__", "unknown"),
				"naming": value["people_naming"],
			},
		)
	if phase == "file_recovery":
		from onerc_vmms_migrate import file_recovery

		candidates = [
			url
			for url, item in sorted(snapshot.get("all_file_manifest", {}).items())
			if item.get("original_status") == "corrupt" or item.get("status") == "corrupt"
		]
		selected = candidates[start : start + size]
		summary = {"recovered": 0, "held": []}
		for url in selected:
			try:
				if snapshot["all_file_manifest"][url].get(
					"original_status"
				) == "corrupt" or file_recovery.recover(
					source, url, frappe.get_single("VMMS Migration Settings")
				):
					summary["recovered"] += 1
				else:
					summary["held"].append(
						{"url": url, "reason": "No bytes matched captured SHA256 and a source File row"}
					)
			except Exception as exc:
				summary["held"].append({"url": url, "reason": frappe.utils.strip_html(str(exc))})
		if summary["recovered"]:
			file_recovery.persist(source)
		return len(selected), len(candidates), [], summary
	if phase == "people":
		built = batches.build(snapshot, conf)
		bundles = sorted(built["bundles"], key=lambda row: row["key"])
		first = value.get("people_offset", 0)
		selected = bundles[first : first + size]
		value["people_total"] = len(bundles)
		summary = import_people(snapshot, source, conf, value, selected, progress)
		processed = summary["processed"]
		value["page_keys"] = [b["key"] for b in selected[:processed]]
		value["next_person_offset"] = first + processed
		return processed, processed, summary.pop("failures"), summary
	if phase == "applications":
		from onerc_vmms_migrate import people_applications

		bundles = [b for b in batches.build(snapshot, conf)["bundles"] if b["key"] in set(value["page_keys"])]
		summary = people_applications.transfer(snapshot, bundles, Crosswalks.read(), value["run_id"])
		return 1, 1, [], summary
	if phase == "file_plan":
		from onerc_vmms_migrate import attachment_plan, trial_attachments

		plan = attachment_plan.build(snapshot, conf, only_people=value["page_keys"])
		files = {
			"session_id": hashlib.sha256(
				(value["session_id"] + ":" + str(value["people_offset"])).encode()
			).hexdigest()[:32],
			"offset": 0,
			"total": len(plan["tasks"]),
			"batch_size": size,
		}
		trial_attachments.save_plan(files, plan, commit_state=False)
		value["files"] = files
		report_name = "files-" + str(value["people_offset"]).zfill(8)
		workspace.write_json(os.path.join(directory, report_name + ".json"), plan)
		run._write_csv(directory, report_name + "-held.csv", plan["held"])
		run._write_csv(directory, report_name + "-external.csv", plan.get("external", []))
		return 1, 1, [], plan["summary"]
	if phase == "profile_files":
		from onerc_vmms_migrate import trial_attachments

		files = {**value["files"], "batch_size": size}
		selected = trial_attachments.read_tasks(files)
		deadline = time.monotonic() + 240
		done = 0
		for task in selected:
			if done and time.monotonic() >= deadline:
				break
			trial_attachments.copy_task(task, source, value["run_id"])
			done += 1
		return done, files["total"], [], {"copied": done, "total": files["total"]}
	if phase == "accounts":
		keys = sorted(snapshot["users"])[start : start + size]
		part = {**snapshot, "users": {k: snapshot["users"][k] for k in keys}}
		deadline = time.monotonic() + 240
		summary = batches.accounts.transfer(
			part, conf, value["run_id"], stop=lambda: time.monotonic() >= deadline
		)
		return summary.pop("processed"), len(snapshot["users"]), summary.pop("failed"), summary
	if phase == "versions":
		from onerc_vmms_migrate import people_versions

		selected = people_versions.page(snapshot, value.get("last_version_name", ""), size)
		summary = people_versions.transfer(snapshot, selected, value["run_id"])
		if selected:
			value["last_version_name"] = selected[-1]["name"]
		# Full pages advertise an unknown total until the final page.
		return len(selected), start + len(selected) if len(selected) < size else None, [], summary
	if phase == "counters":
		from onerc_vmms_migrate.naming_continuity import reserve_people

		value["people_naming"] = reserve_people(snapshot)
		workspace.write_json(os.path.join(directory, "people-counters.json"), value["people_naming"])
		return 1, 1, [], value["people_naming"]
	if phase == "people_verification":
		built = batches.build(snapshot, conf)
		selected = sorted(built["bundles"], key=lambda b: b["key"])[start : start + size]
		failures = []
		changed = []
		for bundle in selected:
			if bundle["errors"]:
				continue
			row = committed_person(bundle)
			if not row:
				failures.append(
					{"person": bundle["key"], "reason": "Ready person has no committed profile ledger"}
				)
			elif not unchanged(row, bundle):
				changed.append({"person": bundle["key"], "action": "Source changed; existing data preserved"})
		if start + len(selected) >= len(built["bundles"]) and not failures:
			from onerc_vmms_migrate import login_policy

			login_policy.apply(value["authentication_policy"])
		return len(selected), len(built["bundles"]), failures, {"changed": changed, "verified": len(selected)}
	raise ValueError("Unknown people migration phase: " + phase)


def advance(value, processed, total, failures):
	value["failed"] = failures
	value["total"] = total
	value["last_batch"] = {
		"phase": value["phase"],
		"processed": processed,
		"failed": failures,
		"offset": value["offset"],
	}
	if failures:
		value["status"] = "blocked"
		return
	if not processed and total is None:
		raise ValueError("People batch made no progress")
	value["offset"] += processed
	value["batch_number"] += 1
	value["status"] = "ready"
	if value["phase"] == "profile_files":
		value["files"]["offset"] = value["offset"]
	if total is None or value["offset"] < total:
		return
	phases = (
		"preflight",
		"file_recovery",
		"people",
		"applications",
		"file_plan",
		"profile_files",
		"accounts",
		"versions",
		"counters",
		"people_verification",
	)
	phase = value["phase"]
	if phase == "profile_files":
		value["people_offset"] = value["next_person_offset"]
		value["phase"] = "people" if value["people_offset"] < value["people_total"] else "accounts"
		if not value.get("auto_continue"):
			value["status"] = "paused"
	elif phase == "people_verification":
		value.update(status="finished", passed=True)
	else:
		value["phase"] = phases[phases.index(phase) + 1]
	value["offset"] = 0
	value["total"] = None
