"""System Manager browser controls. All slow work runs on the long worker."""

import csv
import hashlib
import json
import os
import time
import uuid

import frappe
from frappe.utils import now_datetime

from onerc_vmms_migrate import crosswalk, marks, normalise, workspace

SETTINGS = "VMMS Migration Settings"
ACTIONS = {
	"capture",
	"prepare",
	"audit_logins",
	"check",
	"selftest",
	"profile",
	"crosswalks",
	"plan",
	"project_plan",
	"rehearsal",
	"batch_test",
	"people_import",
	"import",
	"reconcile",
	"repairs_preview",
	"repairs_apply",
	"history_preview",
	"history_import",
}


def authorize():
	if frappe.session.user == "Guest" or "System Manager" not in frappe.get_roles():
		frappe.throw("Only System Managers can run migrations.", frappe.PermissionError)


@frappe.whitelist()
def login_transfer_public_key() -> dict:
	authorize()
	from onerc_vmms_migrate.logins import public_key_info

	return public_key_info()


def lock_key():
	return frappe.cache.make_key("onerc_vmms_migrate:browser-lock")


def assert_idle():
	from onerc_vmms_migrate.date_repair_control import state as date_state
	if (date_state() or {}).get("status") == "ready":
		frappe.throw("Pause the historical date repair before changing migration inputs or restoring services.")
	from onerc_vmms_migrate.batches import checkpoint
	batch = checkpoint()
	if batch and batch["status"] == "ready":
		frappe.throw("Pause the batch session before changing migration inputs.")
	from onerc_vmms_migrate.trial_attachments import state as attachment_state
	files = attachment_state()
	if files and files["status"] == "ready":
		frappe.throw("Pause the attachment copy before changing migration inputs or restoring services.")
	if frappe.cache.get(lock_key()):
		frappe.throw("A migration job is queued or running. Wait for it to finish before making changes.")


def state():
	path = workspace.path("browser-state.json")
	if not os.path.exists(path):
		return {}
	with open(path) as handle:
		return json.load(handle)


def save_state(value):
	path = workspace.path("browser-state.json")
	temporary = path + ".tmp"
	workspace.write_json(temporary, value)
	os.replace(temporary, path)


def _input_digest():
	conf = dict(workspace.config())
	digest = hashlib.sha256(json.dumps(conf, sort_keys=True, default=str).encode())
	registry = os.path.join(os.path.dirname(workspace.crosswalk_dir()), "placeholder-emails.json")
	if os.path.isfile(registry):
		with open(registry, "rb") as handle:
			digest.update(b"placeholder-emails\0" + handle.read())
	for kind in sorted(crosswalk.KINDS):
		path = os.path.join(workspace.crosswalk_dir(), kind + ".csv")
		if os.path.isfile(path):
			with open(path, "rb") as handle:
				digest.update(handle.read())
	return digest


def fingerprint(include_code=True):
	digest = _input_digest()
	if not include_code:
		return digest.hexdigest()
	# A code/schema update invalidates a previous rehearsal too.
	from pathlib import Path
	for file in sorted(Path(__file__).parent.rglob("*")):
		if file.is_file() and file.suffix in (".py", ".json") and "tests" not in file.parts:
			digest.update(file.read_bytes())
	return digest.hexdigest()


@frappe.whitelist()
def status() -> dict:
	authorize()
	_history_clock()
	value = state()
	value["busy"] = bool(frappe.cache.get(lock_key()))
	from onerc_vmms_migrate.batches import checkpoint, public_state
	batch = checkpoint()
	if batch:
		value["batch"] = public_state(batch)
	from onerc_vmms_migrate.trial_attachments import state as attachment_state, public as attachment_public
	value["attachment_batch"] = attachment_public(attachment_state())
	from onerc_vmms_migrate.date_repair_control import state as date_state, public as date_public
	value["date_repair"] = date_public(date_state())
	value["reports"] = []
	reports = workspace.path("reports", "placeholder")
	reports = os.path.dirname(reports)
	if os.path.isdir(reports):
		for run in sorted(os.listdir(reports), reverse=True)[:30]:
			directory = os.path.join(reports, run)
			if os.path.isdir(directory):
				value["reports"].append(
					{
						"run": run,
						"files": sorted(f for f in os.listdir(directory) if f.endswith((".csv", ".json"))),
					}
				)
	return value


@frappe.whitelist(methods=["POST"])
def start(
	action: str,
	confirmation: str = "",
	backup_confirmed: bool = False,
	source_frozen: bool = False,
	exclusions_reviewed: bool = False,
	email_login_confirmed: bool = False,
	resume_capture_id: str = "",
	batch_size: int = 0,
	test_destination: bool = False,
	repairs: list | None = None,
) -> dict:
	authorize()
	if action not in ACTIONS:
		frappe.throw("Unknown migration action.")
	if action == "repairs_apply":
		from onerc_vmms_migrate import post_import_repairs
		repairs = post_import_repairs.validate_request(repairs, confirmation, backup_confirmed)
	if action == "history_import":
		from onerc_vmms_migrate import history_import
		history_import.validate_request(confirmation, backup_confirmed)
	if resume_capture_id:
		_validate_resume_capture(action, resume_capture_id)
	settings = frappe.get_single(SETTINGS)
	if action not in ("capture", "audit_logins") and not settings.browser_capture:
		frappe.throw("Capture the old site first.")
	if action=="people_import" and confirmation!="IMPORT PEOPLE":
		frappe.throw("Confirm IMPORT PEOPLE to start the first committed people batch.")
	if action == "import":
		value = state()
		rehearsal = value.get("rehearsal") or {}
		if rehearsal.get("fingerprint") != fingerprint() or not rehearsal.get("passed"):
			frappe.throw(
				"Run a successful full rehearsal with the current capture, settings and mappings first."
			)
		if (rehearsal.get("authentication_policy") or {}).get("mail_delivery_test_required") and not email_login_confirmed:
			frappe.throw("Test destination email-link sign-in delivery and confirm it before importing. The source uses this login method.")
		if confirmation != "LOAD" or not all((backup_confirmed, source_frozen, exclusions_reviewed)):
			frappe.throw("Confirm LOAD, the target backup, source freeze, and review of excluded records.")
	from onerc_vmms_migrate import batches, batch_browser
	existing_batch = batches.checkpoint()
	if existing_batch and existing_batch.get("scope")=="people" and existing_batch["status"]=="finished" and action in ("capture","people_import"):
		existing_batch=None
	if existing_batch and action not in ("check", "selftest", "profile", "plan", "project_plan", "reconcile", "repairs_preview", "repairs_apply", "history_preview", "history_import"):
		frappe.throw("A batch session already exists. Use its Pause/Resume controls; trial records cannot be promoted to a live import.")
	if action in ("batch_test","people_import") or (action == "import" and batch_size):
		if action == "batch_test" and (confirmation != "TEST" or not test_destination or not backup_confirmed):
			frappe.throw("Confirm TEST, a disposable test destination, and a current backup. Test batches retain destination records.")
		size = batches.validate_size(batch_size or (50 if action=="people_import" else 1000))
		batch_browser.assert_job_idle()
		frappe.cache.delete(batch_browser.pause_key())
		return batch_browser.queue(action, frappe.session.user, size)
	from onerc_vmms_migrate.date_repair_control import state as date_state
	if (date_state() or {}).get("status") == "ready":
		frappe.throw("Pause the historical date repair before starting another migration action.")
	token = uuid.uuid4().hex
	if not frappe.cache.set(lock_key(), token, nx=True, ex=21600):
		frappe.throw("A migration job is already queued or running.")
	try:
		value = state()
		if resume_capture_id:
			value["pending_bulk_capture"] = resume_capture_id
		value.update(
			{
				"action": action,
				"job_id": token,
				"status": "queued",
				"started_by": frappe.session.user,
				"started_at": str(now_datetime()),
				"error": None,
				"result": None,
			}
		)
		value.pop("progress", None)
		value.pop("progress_updated_at", None)
		for item in ("continued", "prepared_once", "jobs", "stalls", "last_marker", "resume_after",
			"waiting_user", "server_stops", "stop_marker"):
			value.pop(item, None)
		if action != "capture":
			value.pop("transfer", None)
		if action == "repairs_apply":
			value["repairs_selected"] = repairs
		save_state(value)
		_enqueue(token, action, frappe.session.user)
	except Exception:
		frappe.cache.delete(lock_key())
		raise
	return {"job_id": token}


def _enqueue(token, action, user):
	frappe.enqueue(
		"onerc_vmms_migrate.browser.execute",
		queue=frappe.conf.get("onerc_vmms_migrate_queue") or "long",
		timeout=18000,
		job_id=token,
		token=token,
		action=action,
		user=user,
	)


HISTORY_ACTIONS = ("history_preview", "history_import")
HISTORY_JOBS = 400  # about a day of 4-minute stretches
HISTORY_STALLS = 3
WAITING = {"phase": "waiting for the 4-minute mark", "processed": 0, "total": None}


def _carry_on(value, result):
	"""Whether a history job's result asks for a new job: it was not inside a
	stretch, it saved the small inputs (history_inputs), or it stood aside
	for a mark (marks.py).

	Once only after saving the inputs: a job queued that way that prepares
	again stops, rather than queueing itself for ever. A run that stands
	aside three times without getting any further stops with its place kept."""
	result = result or {}
	if result.get("waiting"):
		return True
	if result.get("prepared"):
		if value.get("prepared_once"):
			return False
		value["prepared_once"] = True
		return True
	if not result.get("stepped_aside"):
		return False
	marker = result.get("marker")
	stalls = int(value.get("stalls") or 0) + 1 if marker == value.get("last_marker") else 0
	jobs = int(value.get("jobs") or 0) + 1
	value.update({"jobs": jobs, "stalls": stalls, "last_marker": marker})
	if stalls >= HISTORY_STALLS or jobs >= HISTORY_JOBS:
		frappe.throw(
			f"The history import stopped for the 4-minute marks {jobs} times and its last "
			f"{stalls + 1} jobs got no further (at {result.get('phase')}). Its place is kept: "
			"press Import history to carry on, and send the worker log if it stops here again."
		)
	return True


def _wait_for_stretch(value, user):
	"""The run carries on when the next stretch begins; until then no job of it exists."""
	value.update({"status": "waiting", "resume_after": marks.next_start(), "waiting_user": user,
		"progress": dict(WAITING), "progress_updated_at": str(now_datetime())})


def _history_clock():
	"""The clock of a history import (marks.py), run by the open migration
	panel every few seconds: it queues the next job when a stretch begins,
	and treats a job the server stopped as one that stood aside, so the next
	job carries on from the run's kept place. Four stops in a row at the same
	place end the run with its place kept."""
	value = state()
	if value.get("action") not in HISTORY_ACTIONS:
		return
	if value.get("status") == "waiting":
		if time.time() >= float(value.get("resume_after") or 0):
			_continue(value["action"], value.get("waiting_user") or value.get("started_by"))
		return
	if value.get("status") not in ("queued", "running") or not value.get("job_id"):
		return
	token = frappe.cache.get(lock_key())
	if token != value["job_id"].encode():
		return
	from frappe.utils.background_jobs import get_job_status

	if get_job_status(value["job_id"]) not in ("failed", "stopped", "canceled"):
		return
	from onerc_vmms_migrate import history_progress

	place = history_progress.load()
	marker = history_progress.marker(place) if place else None
	stops = int(value.get("server_stops") or 0) + 1 if marker == value.get("stop_marker") else 1
	if frappe.cache.get(lock_key()) == token:
		frappe.cache.delete(lock_key())
	value.update({"server_stops": stops, "stop_marker": marker, "result": None, "error": None})
	if stops > HISTORY_STALLS:
		value.update({"status": "failed", "finished_at": str(now_datetime()), "error": (
			f"The server stopped the history import {stops} times at the same place. Its place is "
			"kept: press Import history to carry on, and send the worker log if it stops there again.")})
	else:
		_wait_for_stretch(value, value.get("started_by"))
	save_state(value)


def _continue(action, user):
	"""Queue the same action again as a new job, for a run that stopped on
	purpose (history_inputs, marks.py). The request was confirmed when the
	first job was queued; nothing else may have taken the lock since."""
	token = uuid.uuid4().hex
	if not frappe.cache.set(lock_key(), token, nx=True, ex=21600):
		return
	try:
		value = state()
		value.update(
			{
				"action": action,
				"job_id": token,
				"status": "queued",
				"started_by": user,
				"started_at": str(now_datetime()),
				"error": None,
				"result": None,
				"continued": True,
			}
		)
		value.pop("progress", None)
		value.pop("progress_updated_at", None)
		save_state(value)
		_enqueue(token, action, user)
	except Exception:
		frappe.cache.delete(lock_key())
		raise



def _validate_resume_capture(action, capture_id):
	import re
	from pathlib import Path
	if action != "capture" or not re.fullmatch(r"[a-f0-9]{32}", capture_id):
		frappe.throw("Resume requires a valid existing capture identifier.")
	if not Path(workspace.path("captures", capture_id, "transfer.json")).is_file():
		frappe.throw("This capture has no saved bulk transfer on this site.")


def _register_capture(capture_id):
	# Source transfer can exceed the destination DB connection's idle timeout.
	# Capture has no pending business writes; use a fresh connection to register it.
	frappe.db.close()
	frappe.db.connect()
	from onerc_vmms_migrate.people_run import check_source
	from onerc_vmms_migrate.batches import DOCTYPE
	with open(workspace.path("captures",capture_id,"snapshot.json")) as handle:
		check_source(frappe.db.get_single_value(DOCTYPE,"people_source_id"),json.load(handle))
	frappe.db.set_single_value(SETTINGS, "browser_capture", capture_id)
	frappe.db.commit()


def _progress_reporter(value):
	# Only aggregate counters leave the worker; never record source identities or credentials.
	fields = {"phase", "processed", "total", "loaded", "existing", "failed", "reused", "transferred"}
	def report(details):
		value["progress"] = {key: item for key, item in details.items() if key in fields}
		value["progress_updated_at"] = str(now_datetime())
		save_state(value)
	return report


def execute(token, action, user):
	from frappe.installer import update_site_config
	from frappe.utils.scheduler import disable_scheduler

	from onerc_vmms_migrate import run, full_run, full_reconcile, history_import, post_import_repairs, project_mapping
	from onerc_vmms_migrate.browser_source import capture

	if frappe.cache.get(lock_key()) != token.encode():
		return
	value = state()
	again = False
	try:
		frappe.set_user(user)
		authorize()  # Revoked access must also prevent a queued job from running.
		value.update({"status": "running"})
		value.pop("progress", None)
		value.pop("progress_updated_at", None)
		if action != "capture":
			value.pop("transfer", None)
		save_state(value)
		report_progress = _progress_reporter(value)
		if action == "capture":
			from onerc_vmms_migrate.batches import checkpoint
			batch=checkpoint()
			if batch and batch["status"]!="finished":
				frappe.throw("Finish the current batch session before taking a later capture.")
			settings = frappe.get_single(SETTINGS)
			capture_id = token
			if settings.get("capture_mode") == "Bulk Export":
				from onerc_vmms_migrate.bulk_source import capture as bulk_capture

				capture_id = value.get("pending_bulk_capture") or token
				value["pending_bulk_capture"] = capture_id
				save_state(value)

				def progress(details):
					value["transfer"] = details
					save_state(value)

				result = bulk_capture(settings, capture_id, progress=progress)
			else:
				result = capture(settings, capture_id)
			_register_capture(capture_id)
			value.pop("pending_bulk_capture", None)
			value.pop("rehearsal", None)
		elif action == "audit_logins":
			from onerc_vmms_migrate.bulk_source import BulkClient
			client = BulkClient(frappe.get_single(SETTINGS))
			try:
				result = client.json("authentication_audit")
			finally:
				client.close()
		elif action == "import":
			# Check again inside the worker, after the exclusive lock was acquired.
			rehearsal = value.get("rehearsal") or {}
			if not rehearsal.get("passed") or rehearsal.get("fingerprint") != fingerprint():
				frappe.throw("The capture or mappings changed. Rehearse again.")
			if not value.get("cutover"):
				value["cutover"] = {
					"scheduler_enabled": bool(
						frappe.db.get_single_value("System Settings", "enable_scheduler")
					),
					"mute_emails": frappe.conf.get("mute_emails", 0),
				}
				save_state(value)  # Persist recovery information before changing either setting.
			disable_scheduler()
			frappe.db.commit()
			update_site_config("mute_emails", 1)
			frappe.conf.mute_emails = 1
			value["reconciled"] = False
			save_state(value)
			result = full_run.execute(confirm="LOAD", progress=report_progress)
			value["import_result"] = result
		else:
			funcs = {
				"prepare": full_run.prepare,
				"check": run.check,
				"selftest": run.selftest,
				"profile": run.profile,
				"crosswalks": run.crosswalks,
				"plan": run.plan,
				"project_plan": project_mapping.preview,
				"rehearsal": full_run.execute,
				"reconcile": full_reconcile.execute,
				"repairs_preview": post_import_repairs.preview,
				"repairs_apply": post_import_repairs.apply,
				"history_preview": history_import.preview,
				"history_import": history_import.apply,
			}
			with_progress = ("rehearsal", "repairs_preview", "repairs_apply", "history_preview", "history_import")
			result = funcs[action](progress=report_progress) if action in with_progress else funcs[action]()
			if action == "rehearsal":
				value["rehearsal"] = {
					"fingerprint": fingerprint(),
					"passed": bool(result.get("passed")),
					"authentication_policy": result.get("authentication_policy"),
				}
			if action == "reconcile":
				value["reconciled"] = not result.get("mismatches") and bool(result.get("people_checked"))
			again = action in HISTORY_ACTIONS and _carry_on(value, result)
		value.update({"status": "finished", "result": result, "finished_at": str(now_datetime())})
		if again:
			_wait_for_stretch(value, user)
	except Exception as exc:
		frappe.db.rollback()
		# Never include request headers, source response bodies or password fields in browser errors.
		from onerc_vmms_migrate.source import SourceError

		safe = isinstance(exc, (SourceError, frappe.ValidationError, frappe.PermissionError))
		value.update(
			{
				"status": "failed",
				"error": frappe.utils.strip_html(str(exc))
				if safe
				else f"{type(exc).__name__}: migration stopped. Ask the administrator to review the worker log.",
				"finished_at": str(now_datetime()),
			}
		)
		if action == "rehearsal":
			value.pop("rehearsal", None)
	finally:
		save_state(value)
		if frappe.cache.get(lock_key()) == token.encode():
			frappe.cache.delete(lock_key())


@frappe.whitelist(methods=["POST"])
def finish_cutover(confirmation: str = "", send_held_mail: bool = False) -> None:
	authorize()
	assert_idle()
	value = state()
	original = value.get("cutover")
	if not original:
		frappe.throw("There is no migration cutover to finish.")
	assert_no_held_mail(send_held_mail)
	if confirmation != "RESTORE" and (
		not value.get("reconciled")
		or not (value.get("import_result") or {}).get("passed")
	):
		frappe.throw("Resolve import failures and run reconciliation successfully before finishing cutover.")
	from frappe.installer import update_site_config
	from frappe.utils.scheduler import toggle_scheduler

	toggle_scheduler(original["scheduler_enabled"])
	update_site_config("mute_emails", original["mute_emails"])
	frappe.db.set_single_value(SETTINGS,"people_import_active",0)
	value.pop("cutover", None)
	value["services_restored_after_failure"] = confirmation == "RESTORE"
	save_state(value)


def assert_no_held_mail(send_held_mail) -> None:
	"""Muting holds mail in the queue rather than dropping it, and the first
	scheduler run after the cutover sends all of it, however old. Finishing
	waits until the operator has read it and chosen."""
	from onerc_vmms_migrate.post_import_repairs import HELD_STATUSES, REPAIRS

	if send_held_mail in (True, 1, "1", "true", "True"):
		return
	held = frappe.db.count("Email Queue", {"status": ["in", HELD_STATUSES]})
	if held:
		frappe.throw(
			f"{held} emails were queued while email was held, and finishing the cutover would send them all. "
			f"Read them in Preview repairs, then either apply \"{REPAIRS['held_mail']}\" or tick "
			"\"Send the mail held during the cutover\" here."
		)


@frappe.whitelist(methods=["POST"])
def clear_capture() -> None:
	authorize()
	assert_idle()
	from onerc_vmms_migrate.batches import checkpoint
	batch=checkpoint()
	if batch and (batch.get("scope")!="people" or batch["status"]!="finished"):
		frappe.throw("Finish the current session before selecting a later capture. Previous captures and ledgers are retained.")
	frappe.db.set_single_value(SETTINGS, "browser_capture", "")
	value = state()
	value.pop("rehearsal", None)
	value.pop("reconciled", None)
	value.pop("pending_bulk_capture", None)
	value.pop("transfer", None)
	save_state(value)


@frappe.whitelist()
def mappings(kind: str) -> dict:
	authorize()
	if kind not in crosswalk.KINDS:
		frappe.throw("Unknown mapping type.")
	spec = crosswalk.KINDS[kind]
	rows = list(crosswalk.Crosswalks.read().entries.get(kind, {}).values())
	return {
		"rows": rows,
		"about": spec["about"],
		"doctype": spec.get("doctype"),
		"choices": [crosswalk.DROP, *spec.get("choices", []), *spec.get("special", [])],
	}


@frappe.whitelist(methods=["POST"])
def save_mappings(kind: str, rows: list[dict]) -> None:
	authorize()
	assert_idle()
	if kind not in crosswalk.KINDS:
		frappe.throw("Unknown mapping type.")
	original = crosswalk.Crosswalks.read().entries.get(kind, {})
	if not isinstance(rows, list) or len(rows) != len(original):
		frappe.throw("Refresh the mapping list and try again.")
	seen = set()
	clean = []
	for row in rows:
		key = normalise.key(row.get("source_value"))
		if key not in original or key in seen:
			frappe.throw("Mapping source values cannot be changed.")
		seen.add(key)
		target = normalise.text(row.get("target"))
		if target and target != crosswalk.DROP and not crosswalk.target_exists(kind, target):
			frappe.throw(f"The selected target does not exist: {target}")
		clean.append(
			{
				**original[key],
				"target": target,
				"note": normalise.text(row.get("note")),
				"target_label": crosswalk.label_of(kind, target),
			}
		)
	path = os.path.join(workspace.crosswalk_dir(), kind + ".csv")
	with open(path + ".tmp", "w", newline="") as handle:
		writer = csv.DictWriter(handle, fieldnames=crosswalk.COLUMNS, extrasaction="ignore")
		writer.writeheader()
		writer.writerows(clean)
	os.replace(path + ".tmp", path)


@frappe.whitelist()
def download_report(run_id: str, filename: str) -> None:
	authorize()
	if (
		not run_id
		or os.path.basename(run_id) != run_id
		or not filename
		or os.path.basename(filename) != filename
		or not filename.endswith((".csv", ".json"))
		or run_id in (".", "..")
	):
		frappe.throw("Invalid report path.")
	path = os.path.realpath(os.path.join(workspace.root(), "reports", run_id, filename))
	reports = os.path.realpath(os.path.join(workspace.root(), "reports"))
	if not path.startswith(reports + os.sep) or not os.path.isfile(path):
		frappe.throw("Report not found.")
	with open(path, "rb") as handle:
		content = handle.read()
	frappe.local.response.update({"filename": filename, "filecontent": content, "type": "download"})


@frappe.whitelist()
def people_preflight() -> dict:
	authorize()
	from onerc_vmms_migrate.file_transfer import assert_supported
	assert_supported()
	return {"file_copy_supported":True,"frappe_version":getattr(frappe,"__version__","unknown"),
		"review_mode":workspace.config().get("import_with_review"),"scope":"People, files, then Version history"}
