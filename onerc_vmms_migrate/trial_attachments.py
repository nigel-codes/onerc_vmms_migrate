"""Separate attachment review/copy checkpoints for a completed disposable trial."""

import json
import os
import time
import uuid
from contextlib import contextmanager
from functools import wraps

import frappe

from onerc_vmms_migrate import attachment_plan, batch_browser, batches, browser, historical, workspace
from onerc_vmms_migrate.bulk_source import sha256_file
from onerc_vmms_migrate.file_transfer import copy_verified
from onerc_vmms_migrate.silence import Guard, silenced
from onerc_vmms_migrate.transaction import large_transaction

FIELD = "attachment_checkpoint"
TASKS = "VMMS Migration Attachment Task"
PUBLIC = (
	"run_id",
	"session_id",
	"capture_id",
	"status",
	"offset",
	"total",
	"batch_size",
	"batch_number",
	"plan_digest",
	"summary",
	"error",
	"updated_at",
	"source_test_session",
	"fingerprint",
	"versions_reviewed",
)


def state():
	if not frappe.db.exists("DocType", batches.DOCTYPE):
		return None
	if not frappe.get_meta(batches.DOCTYPE).has_field(FIELD) or not frappe.db.exists("DocType", TASKS):
		return None
	raw = frappe.db.get_single_value(batches.DOCTYPE, FIELD)
	return json.loads(raw) if raw else None


def commit(value):
	value["updated_at"] = str(frappe.utils.now_datetime())
	frappe.db.set_single_value(batches.DOCTYPE, FIELD, json.dumps(value, default=str))
	frappe.db.commit()


def public(value):
	return {key: value[key] for key in PUBLIC if key in value} if value else None


def qualified(conf):
	proof = batches.checkpoint()
	if (
		not proof
		or proof.get("mode") != "test"
		or proof.get("status") != "finished"
		or proof.get("passed") is not True
	):
		raise ValueError("A completed successful persistent test is required; no people are imported here.")
	if not conf.get("browser_capture") or proof.get("capture_id") != conf["browser_capture"]:
		raise ValueError("The capture differs from the successful test.")
	return proof


def review_digest(value):
	return attachment_plan.digest({key: value[key] for key in ("plan_digest", "total", "review_summary")})


@large_transaction
def save_plan(value, plan):
	"""Immutable tasks are written once; each copy commit stores only its cursor."""
	columns = [
		"name",
		"creation",
		"modified",
		"owner",
		"modified_by",
		"docstatus",
		"idx",
		"plan_id",
		"task_index",
		"task_payload",
	]
	now = frappe.utils.now_datetime()
	pending = []
	for index, task in enumerate(plan["tasks"]):
		name = value["session_id"] + ":" + str(index).zfill(8) + ":" + attachment_plan.digest(task)
		pending.append(
			(
				name,
				now,
				now,
				frappe.session.user,
				frappe.session.user,
				0,
				index + 1,
				value["session_id"],
				index,
				json.dumps(task, sort_keys=True, default=str),
			)
		)
		if len(pending) == 1000:
			frappe.db.bulk_insert(TASKS, columns, pending)
			pending = []
	if pending:
		frappe.db.bulk_insert(TASKS, columns, pending)
	commit(value)


def read_tasks(value):
	if not 0 <= value["offset"] <= value["total"]:
		raise ValueError("Attachment cursor is outside its reviewed plan.")
	rows = frappe.get_all(
		TASKS,
		filters={
			"plan_id": value["session_id"],
			"name": [
				"between",
				[
					value["session_id"] + ":" + str(value["offset"]).zfill(8) + ":",
					value["session_id"] + ":99999999:~",
				],
			],
		},
		fields=["name", "task_index", "task_payload"],
		order_by="name asc",
		limit_page_length=value["batch_size"],
	)
	result = []
	for index, row in enumerate(rows, start=value["offset"]):
		task = json.loads(row.task_payload)
		expected = value["session_id"] + ":" + str(index).zfill(8) + ":" + attachment_plan.digest(task)
		if row.task_index != index or row.name != expected or index >= value["total"]:
			raise ValueError("Stored attachment task differs from its reviewed identity or sequence.")
		result.append(task)
	if not result and value["offset"] < value["total"]:
		raise ValueError("Reviewed attachment tasks are missing; the cursor was not advanced.")
	return result


def ensure_current(value, accept_code_changes=False):
	if review_digest(value) != value["review_digest"]:
		raise ValueError("Stored attachment plan differs from the reviewed digest.")
	proof = qualified(workspace.config())
	if proof["session_id"] != value["source_test_session"] or value.get(
		"inputs_fingerprint"
	) != browser.fingerprint(False):
		raise ValueError(
			"Capture, mappings, settings or successful test changed. Attachment copying stopped."
		)
	current = browser.fingerprint()
	if value["fingerprint"] != current:
		if not accept_code_changes:
			raise ValueError(
				"Code changed. Review the deployment and explicitly confirm it when resuming attachments."
			)
		value.setdefault("versions_reviewed", []).append(value["fingerprint"])
		value["fingerprint"] = current


def pause_key():
	return frappe.cache.make_key("onerc_vmms_migrate:trial-attachments-pause")


@contextmanager
def control_lock():
	from onerc_vmms_migrate.date_repair_control import state as date_state
	if (date_state() or {}).get("status") == "ready":
		raise ValueError("Pause the historical date repair before controlling attachment copying.")
	batch_browser.assert_job_idle()
	token = batch_browser.reserve_lock()
	try:
		yield token
	except Exception:
		if frappe.cache.get(browser.lock_key()) == token.encode():
			frappe.cache.delete(browser.lock_key())
		raise


def queue(stage, token=None):
	if token is None:
		batch_browser.assert_job_idle()
		token = batch_browser.reserve_lock()
	try:
		view = browser.state()
		view.update(
			action="trial_attachments",
			job_id=token,
			status="queued",
			error=None,
			result=None,
			started_at=str(frappe.utils.now_datetime()),
			started_by=frappe.session.user,
		)
		view.pop("transfer", None)
		current = state()
		view["progress"] = {
			"phase": "attachment_plan" if stage == "prepare" else "attachment_copy",
			"processed": current.get("offset", 0) if current else 0,
			"total": current.get("total") if current else None,
		}
		browser.save_state(view)
		frappe.enqueue(
			"onerc_vmms_migrate.trial_attachments.execute",
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


def review_endpoint(function):
	@wraps(function)
	def wrapped(*args, **kwargs):
		try:
			return function(*args, **kwargs)
		except ValueError as exc:
			frappe.throw(frappe.utils.strip_html(str(exc)))

	return wrapped


@frappe.whitelist(methods=["POST"])
@review_endpoint
def prepare():
	browser.authorize()
	if not frappe.get_meta(batches.DOCTYPE).has_field(FIELD):
		raise ValueError("Run the site migration to install trial attachment checkpoint storage.")
	batch_browser.assert_job_idle()
	browser.assert_idle()
	qualified(workspace.config())
	old = state()
	if old and old["status"] in ("ready", "paused", "blocked") and old.get("copy_authorized"):
		raise ValueError("Resume the existing attachment copy instead of replacing its checkpoint.")
	return queue("prepare")


@frappe.whitelist(methods=["POST"])
@review_endpoint
def start(
	plan_digest: str,
	confirmation: str,
	backup_confirmed: bool = False,
	test_destination: bool = False,
	review_confirmed: bool = False,
	batch_size: int = 100,
):
	browser.authorize()
	with control_lock() as token:
		value = state()
		if not value or value["status"] != "review" or value["plan_digest"] != plan_digest:
			raise ValueError("Prepare and review the current attachment plan first.")
		if confirmation != "COPY FILES" or not all(
			flag is True for flag in (backup_confirmed, test_destination, review_confirmed)
		):
			raise ValueError(
				"Confirm COPY FILES, a disposable test site, backup and review of held/missing files."
			)
		ensure_current(value)
		value.update(copy_authorized=True, batch_size=batches.validate_size(batch_size), status="ready")
		commit(value)
		frappe.cache.delete(pause_key())
		return queue("copy", token)


@frappe.whitelist(methods=["POST"])
@review_endpoint
def resume(batch_size: int = 100, accept_code_changes: bool = False):
	browser.authorize()
	value = state()
	if not value or not value.get("copy_authorized"):
		raise ValueError("The attachment plan has not been approved for copying.")
	with control_lock() as token:
		value = state()
		if not value or not value.get("copy_authorized"):
			raise ValueError("The attachment plan has not been approved for copying.")
		if value["status"] not in ("ready", "paused", "blocked"):
			raise ValueError("There is no unfinished attachment copy.")
		ensure_current(value, accept_code_changes is True)
		value.update(batch_size=batches.validate_size(batch_size), status="ready", error=None)
		commit(value)
		frappe.cache.delete(pause_key())
		return queue("copy", token)


@frappe.whitelist(methods=["POST"])
@review_endpoint
def pause():
	browser.authorize()
	frappe.cache.set(pause_key(), 1)
	return {"message": "Attachment pause requested; the current job will commit first."}


def apply_binding(link, url):
	doc = frappe.get_doc(link["doctype"], link["name"])
	parent = link.get("parent")
	if parent and (
		str(doc.get("parent")) != parent["name"]
		or doc.get("parenttype") != parent["doctype"]
		or doc.get("parentfield") != parent["field"]
	):
		raise ValueError("Attachment child parent changed since review.")
	field = frappe.get_meta(link["doctype"]).get_field(link["field"])
	if not field or field.fieldtype != link["kind"]:
		raise ValueError("Attachment field schema changed since review.")
	current = doc.get(link["field"])
	if field.fieldtype == "Text Editor" and link["kind"] == "Text Editor":
		from onerc_vmms_migrate.html_files import rewrite

		updated = rewrite(current, lambda old: url if old == link["original"] else None)
		if updated == current and url not in (current or ""):
			raise ValueError("Native HTML attachment link changed since review.")
	else:
		if current not in (None, "", link["original"], url):
			raise ValueError("Native attachment field changed; existing file was not overwritten.")
		updated = url
	if updated != current:
		frappe.db.set_value(link["doctype"], link["name"], link["field"], updated, update_modified=False)
		frappe.clear_document_cache(link["doctype"], link["name"])


def copy_task(task, source, run_id):
	if not frappe.db.exists(task["doctype"], task["name"]):
		raise ValueError("Reviewed attachment target no longer exists.")
	if attachment_plan.owner_chain(task["doctype"], task["name"], for_update=True) != task["owner_chain"]:
		raise ValueError("Native attachment owner links changed since review.")
	manifest = source.data.get("all_file_manifest", source.data.get("captured_file_manifest", {}))
	item = manifest.get(task["url"], {})
	if item.get("status") != "ready" or item.get("sha256") != task["sha256"]:
		raise ValueError("Captured attachment manifest changed since review.")
	path = source.file_path(task["url"])
	if not path:
		raise ValueError("Reviewed captured attachment bytes are no longer available.")
	source.verify_file(task["url"], path)
	url = None
	for row in frappe.get_all(
		"File",
		filters={"attached_to_doctype": task["doctype"], "attached_to_name": task["name"], "is_private": 1},
		fields=["name", "file_url"],
	):
		if not str(row.file_url).startswith("/private/files/vmms-" + task["sha256"]):
			continue
		existing = frappe.get_doc("File", row.name).get_full_path()
		if os.path.isfile(existing) and sha256_file(existing) == task["sha256"]:
			url = row.file_url
			break
	if not url:
		url = copy_verified(
			path, source.file_name(task["url"]), task["doctype"], task["name"], True, task["sha256"]
		)
	receipt = frappe.db.get_value(
		"File",
		{
			"file_url": url,
			"is_private": 1,
			"attached_to_doctype": task["doctype"],
			"attached_to_name": task["name"],
		},
		["name", "file_url"],
		as_dict=True,
	)
	if not receipt or not str(receipt.file_url).startswith("/private/files/"):
		raise ValueError("Copied file ownership or private storage verification failed.")
	native_path = frappe.get_doc("File", receipt.name).get_full_path()
	if not os.path.isfile(native_path) or sha256_file(native_path) != task["sha256"]:
		raise ValueError("Copied attachment checksum verification failed.")
	for link in task["bindings"]:
		apply_binding(link, url)
	for name in task["file_records"]:
		historical.mark(source.data, "File", name, "File", receipt.name, run_id)
	return {"file": receipt.name, "doctype": task["doctype"], "name": task["name"], "private": True}


@large_transaction
def step(value, source):
	guard = Guard()
	offset = value["offset"]
	tasks = read_tasks(value)
	deadline = time.monotonic() + 240
	receipts = []
	with silenced():
		for task in tasks:
			if receipts and (time.monotonic() >= deadline or frappe.cache.get(pause_key())):
				break
			value["current_task"] = {
				"index": offset + len(receipts),
				"doctype": task.get("doctype"),
				"name": task.get("name"),
				"url": task.get("url"),
			}
			receipts.append(copy_task(task, source, value["run_id"]))
			if len(receipts) % 5 == 0:
				browser._progress_reporter(browser.state())(
					{"phase": "attachment_copy", "processed": offset + len(receipts), "total": value["total"]}
				)
		guard.check()
	value.pop("current_task", None)
	value["offset"] += len(receipts)
	value["batch_number"] += 1
	value["status"] = "copied" if value["offset"] == value["total"] else "ready"
	value["summary"].update(copied_tasks=value["offset"], access_checks_pending=True)
	if value["status"] != "copied" and frappe.cache.get(pause_key()):
		value["status"] = "paused"
	commit(value)
	workspace.write_json(
		os.path.join(workspace.report_dir(value["run_id"]), "attachment-copy.json"), public(value)
	)


def execute(token, stage, user):
	from onerc_vmms_migrate.source import Source

	if frappe.cache.get(browser.lock_key()) != token.encode():
		return
	view = browser.state()
	value = None
	next_job = False
	try:
		frappe.set_user(user)
		browser.authorize()
		conf = workspace.config()
		proof = qualified(conf)
		view["status"] = "running"
		browser.save_state(view)
		with Source(conf) as source:
			capture = source.data["source"].get("capture_id") or source.data["source"].get("snapshot_id")
			if capture != proof["capture_id"]:
				raise ValueError("Loaded source capture differs from the successful test.")
			if stage == "prepare":
				old = state()
				if old and old.get("copy_authorized") and old["status"] in ("ready", "paused", "blocked"):
					raise ValueError("Resume the unfinished attachment copy; its plan was not replaced.")
				from onerc_vmms_migrate import full_reconcile, run
				from onerc_vmms_migrate.file_transfer import assert_supported

				try:
					assert_supported()
				except RuntimeError as exc:
					raise ValueError(str(exc)) from exc

				_, failures = full_reconcile.verify(source.data)
				if failures:
					raise ValueError(
						"Native verification changed since the completed test. Reconcile before preparing attachments."
					)
				plan = attachment_plan.build(source.data, conf)
				value = {
					"run_id": run._run_id("trial-attachments"),
					"session_id": uuid.uuid4().hex,
					"source_test_session": proof["session_id"],
					"capture_id": proof["capture_id"],
					"fingerprint": browser.fingerprint(),
					"inputs_fingerprint": browser.fingerprint(False),
					"plan_digest": attachment_plan.digest(plan),
					"status": "review",
					"offset": 0,
					"total": len(plan["tasks"]),
					"batch_size": 100,
					"batch_number": 0,
					"summary": dict(plan["summary"]),
					"review_summary": dict(plan["summary"]),
					"error": None,
				}
				value["review_digest"] = review_digest(value)
				save_plan(value, plan)
				directory = workspace.report_dir(value["run_id"])
				workspace.write_json(os.path.join(directory, "attachment-plan.json"), plan)
				workspace.write_json(os.path.join(directory, "attachment-review.json"), public(value))
				run._write_csv(directory, "attachment-held.csv", plan["held"])
			else:
				value = state()
				if not value or not value.get("copy_authorized") or value["status"] != "ready":
					raise ValueError("There is no approved attachment batch ready to run.")
				ensure_current(value)
				batch_browser.begin_cutover(view)
				if frappe.cache.get(pause_key()):
					value["status"] = "paused"
					commit(value)
				else:
					step(value, source)
				next_job = value["status"] == "ready"
		view.update(status=value["status"], result=public(value), error=None)
	except Exception as exc:
		frappe.db.rollback()
		durable = state()
		message = "Attachment work stopped. Review worker logs and the saved attachment checkpoint."
		if isinstance(exc, (ValueError, frappe.ValidationError)):
			message = frappe.utils.strip_html(str(exc))
		if durable and stage != "prepare":
			durable.update(status="blocked", error=message)
			commit(durable)
			workspace.write_json(
				os.path.join(workspace.report_dir(durable["run_id"]), "attachment-failure.json"),
				{
					"task": value.get("current_task") if value else None,
					"error": message,
					"committed_offset": durable["offset"],
				},
			)
		view.update(status="failed", error=message)
		frappe.log_error(title="VMMS trial attachment job stopped")
	finally:
		browser.save_state(view)
		if frappe.cache.get(browser.lock_key()) == token.encode():
			frappe.cache.delete(browser.lock_key())
	if next_job:
		try:
			queue("copy")
		except Exception:
			frappe.log_error(title="VMMS attachment next job not queued; resume saved checkpoint")
