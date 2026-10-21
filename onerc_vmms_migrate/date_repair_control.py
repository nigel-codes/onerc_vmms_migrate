"""Reviewed, checkpointed date repair for an already migrated destination."""

import json
import os
import uuid
from contextlib import contextmanager

import frappe

from onerc_vmms_migrate import batch_browser, batches, browser, date_repair, workspace
from onerc_vmms_migrate.attachment_plan import digest
from onerc_vmms_migrate.transaction import large_transaction
from onerc_vmms_migrate.trial_attachments import review_endpoint

FIELD = "date_repair_checkpoint"
SIZE = 1000


def state():
	if not frappe.db.exists("DocType", batches.DOCTYPE) or not frappe.get_meta(batches.DOCTYPE).has_field(
		FIELD
	):
		return None
	raw = frappe.db.get_single_value(batches.DOCTYPE, FIELD)
	return json.loads(raw) if isinstance(raw, str) and raw else None


def public(value):
	return (
		{
			key: value.get(key)
			for key in (
				"status",
				"run_id",
				"plan_digest",
				"offset",
				"total",
				"summary",
				"error",
				"updated_at",
			)
		}
		if value
		else None
	)


def commit(value):
	value["updated_at"] = str(frappe.utils.now_datetime())
	frappe.db.set_single_value(batches.DOCTYPE, FIELD, json.dumps(value, default=str))
	frappe.db.commit()


def qualified():
	proof = batches.checkpoint()
	if not proof or proof["status"] != "finished" or proof.get("passed") is not True:
		raise ValueError("Finish the persistent migration and verification before repairing dates.")
	if proof["capture_id"] != workspace.config().get("browser_capture"):
		raise ValueError("The selected capture differs from the completed migration.")
	if not browser.state().get("cutover"):
		raise ValueError("Keep migration cutover active while repairing dates.")
	return proof


def ensure_current(value):
	proof = qualified()
	if proof["session_id"] != value["source_session"] or browser.fingerprint() != value["fingerprint"]:
		raise ValueError(
			"The completed migration, configuration or code changed; prepare a new date preview."
		)
	if (
		digest({key: value[key] for key in ("pages", "total", "source_session", "fingerprint")})
		!= value["plan_digest"]
	):
		raise ValueError("Date repair plan integrity check failed.")


@contextmanager
def control_lock():
	batch_browser.assert_job_idle()
	browser.assert_idle()
	token = batch_browser.reserve_lock()
	try:
		yield token
	except Exception:
		if frappe.cache.get(browser.lock_key()) == token.encode():
			frappe.cache.delete(browser.lock_key())
		raise


def pause_key():
	return frappe.cache.make_key("onerc_vmms_migrate:date-repair-pause")


@frappe.whitelist(methods=["POST"])
def pause():
	browser.authorize()
	frappe.cache.set(pause_key(), 1)
	return {"message": "Date repair will pause after the current committed batch."}


def queue(stage, token=None):
	token = token or batch_browser.reserve_lock()
	view = browser.state()
	view.update(
		action="date_repair",
		job_id=token,
		status="queued",
		error=None,
		result=None,
		started_at=str(frappe.utils.now_datetime()),
		started_by=frappe.session.user,
	)
	view.pop("transfer", None)
	view.pop("progress", None)
	browser.save_state(view)
	try:
		frappe.enqueue(
			"onerc_vmms_migrate.date_repair_control.execute",
			queue=frappe.conf.get("onerc_vmms_migrate_queue") or "long",
			timeout=900,
			job_id=token,
			token=token,
			stage=stage,
			user=frappe.session.user,
		)
	except Exception:
		if frappe.cache.get(browser.lock_key()) == token.encode():
			frappe.cache.delete(browser.lock_key())
		raise
	return {"job_id": token}


@frappe.whitelist(methods=["POST"])
@review_endpoint
def prepare():
	browser.authorize()
	if not frappe.get_meta(batches.DOCTYPE).has_field(FIELD):
		raise ValueError("Run site migration to install historical date repair storage.")
	with control_lock() as token:
		qualified()
		return queue("preview", token)


@frappe.whitelist(methods=["POST"])
@review_endpoint
def start(plan_digest: str, confirmation: str, backup_confirmed: bool = False):
	browser.authorize()
	with control_lock() as token:
		value = state()
		if not value or value["status"] != "review" or plan_digest != value["plan_digest"]:
			raise ValueError("Prepare and review the current historical date report first.")
		if confirmation != "REPAIR DATES" or backup_confirmed is not True:
			raise ValueError("Confirm REPAIR DATES and a current database backup.")
		ensure_current(value)
		frappe.cache.delete(pause_key())
		value.update(status="ready", authorized=True)
		commit(value)
		return queue("apply", token)


@frappe.whitelist(methods=["POST"])
@review_endpoint
def resume():
	browser.authorize()
	from onerc_vmms_migrate.trial_attachments import state as attachment_state

	if (attachment_state() or {}).get("status") == "ready":
		raise ValueError("Pause the attachment copy before resuming date repair.")
	# assert_idle deliberately excludes this repair; the shared job lock still serializes jobs.
	batch_browser.assert_job_idle()
	token = batch_browser.reserve_lock()
	try:
		value = state()
		if not value or not value.get("authorized") or value["status"] not in ("blocked", "paused", "ready"):
			raise ValueError("There is no approved unfinished date repair.")
		ensure_current(value)
		frappe.cache.delete(pause_key())
		value.update(status="ready", error=None)
		commit(value)
		return queue("apply", token)
	except Exception:
		if frappe.cache.get(browser.lock_key()) == token.encode():
			frappe.cache.delete(browser.lock_key())
		raise


def page_path(run_id, index):
	return workspace.path("date-repair-pages", run_id, f"page-{index:05d}.json")


def preview(proof):
	previous = state()
	from onerc_vmms_migrate import run
	from onerc_vmms_migrate.source import Source

	with Source() as source:
		capture = source.data["source"].get("capture_id") or source.data["source"].get("snapshot_id")
		if capture != proof["capture_id"]:
			raise ValueError("Loaded capture differs from the completed migration.")
		plan = date_repair.build_plan(source.data, workspace.config(), proof)
	run_id = run._run_id("historical-date-repair") + "-" + uuid.uuid4().hex[:8]
	directory = workspace.report_dir(run_id)
	pages = []
	for offset in range(0, len(plan["tasks"]), SIZE):
		page = plan["tasks"][offset : offset + SIZE]
		workspace.write_json(page_path(run_id, offset // SIZE), page)
		pages.append(digest(page))
	value = {
		"run_id": run_id,
		"status": "review",
		"offset": 0,
		"total": len(plan["tasks"]),
		"pages": pages,
		"source_session": proof["session_id"],
		"capture_id": proof["capture_id"],
		"fingerprint": browser.fingerprint(),
		"summary": plan["summary"],
		"error": None,
		"previous_run": previous.get("run_id") if previous else None,
	}
	value["plan_digest"] = digest(
		{key: value[key] for key in ("pages", "total", "source_session", "fingerprint")}
	)
	workspace.write_json(os.path.join(directory, "historical-dates-plan.json"), plan)
	run._write_csv(directory, "historical-dates-held.csv", plan["held"])
	commit(value)
	workspace.write_json(os.path.join(directory, "historical-dates-review.json"), public(value))
	return value


def apply_task(task):
	from onerc_vmms_migrate import load

	dt, name = task["doctype"], task["name"]
	if dt not in date_repair.TARGETS or not task["after"] or set(task["after"]) - {"creation", "joined_on"}:
		raise ValueError("Date repair task contains fields outside its reviewed scope.")
	if "joined_on" in task["after"] and dt not in date_repair.JOINED:
		raise ValueError("Joining date is not supported on this target.")
	for link in task["ledger"]:
		current = frappe.db.get_value(load.LEDGER, link["name"], list(link), as_dict=True, for_update=True)
		if not current or any(str(current.get(k)) != str(v) for k, v in link.items()):
			raise ValueError("A reviewed source-to-destination mapping changed.")
	current = frappe.db.get_value(dt, name, list(task["before"]), as_dict=True, for_update=True)
	if not current:
		raise ValueError("A reviewed native date target is missing.")
	expected = {**task["before"], **task["after"]}

	def same(values):
		return all(
			date_repair.comparable(k, current.get(k)) == date_repair.comparable(k, v)
			for k, v in values.items()
		)

	if same(expected):
		return
	if not same(task["before"]):
		raise ValueError("A native record changed after preview. No unreviewed values were overwritten.")
	# Deliberately avoid activation/save hooks, ownership changes or a synthetic modified timestamp.
	frappe.db.set_value(dt, name, task["after"], update_modified=False)
	frappe.clear_document_cache(dt, name)


@large_transaction
def step(value):
	ensure_current(value)
	index = value["offset"] // SIZE
	if value["offset"] < value["total"]:
		with open(page_path(value["run_id"], index)) as handle:
			page = json.load(handle)
		if digest(page) != value["pages"][index] or len(page) != min(SIZE, value["total"] - value["offset"]):
			raise ValueError("Immutable date repair page changed or is incomplete.")
		from onerc_vmms_migrate.silence import Guard

		guard = Guard()
		for task in page:
			apply_task(task)
		guard.check()
		value["offset"] += len(page)
	value["status"] = (
		"finished"
		if value["offset"] == value["total"]
		else ("paused" if frappe.cache.get(pause_key()) else "ready")
	)
	commit(value)
	return value


def execute(token, stage, user):
	if frappe.cache.get(browser.lock_key()) != token.encode():
		return
	view, value, next_job = browser.state(), None, False
	try:
		frappe.set_user(user)
		browser.authorize()
		proof = qualified()
		view.update(status="running")
		browser.save_state(view)
		if stage == "preview":
			value = preview(proof)
		else:
			value = state()
			if not value or not value.get("authorized") or value["status"] != "ready":
				raise ValueError("There is no approved date repair ready to run.")
			value = step(value)
			next_job = value["status"] == "ready"
			workspace.write_json(
				os.path.join(workspace.report_dir(value["run_id"]), "historical-dates-result.json"),
				public(value),
			)
			view.pop("reconciled", None)
		view.update(
			status="finished",
			result=public(value),
			error=None,
			progress={
				"phase": "date_preview" if stage == "preview" else "date_repair",
				"processed": value["offset"],
				"total": value["total"],
			},
		)
	except Exception as exc:
		next_job = False
		frappe.db.rollback()
		# Reload the committed cursor; never record an uncommitted in-memory offset.
		saved = state()
		message = (
			str(exc)
			if isinstance(exc, (ValueError, frappe.ValidationError))
			else type(exc).__name__ + ": review the worker log"
		)
		if stage == "apply" and saved:
			saved.update(status="blocked", error=message)
			commit(saved)
		view.update(status="failed", error=message)
		frappe.log_error(title="Historical date repair stopped", message=frappe.get_traceback())
	finally:
		browser.save_state(view)
		if frappe.cache.get(browser.lock_key()) == token.encode():
			frappe.cache.delete(browser.lock_key())
	if next_job:
		# A competing migration control can acquire the lock first; then Resume is safe.
		batch_browser.assert_job_idle()
		queue("apply")
