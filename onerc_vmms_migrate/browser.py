"""System Manager browser controls. All slow work runs on the long worker."""

import csv
import hashlib
import json
import os
import uuid

import frappe
from frappe.utils import now_datetime

from onerc_vmms_migrate import crosswalk, normalise, workspace

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
	"rehearsal",
	"batch_test",
	"import",
	"reconcile",
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


def fingerprint(include_code=True):
	conf = dict(workspace.config())
	digest = hashlib.sha256(json.dumps(conf, sort_keys=True, default=str).encode())
	for kind in sorted(crosswalk.KINDS):
		path = os.path.join(workspace.crosswalk_dir(), kind + ".csv")
		if os.path.isfile(path):
			with open(path, "rb") as handle:
				digest.update(handle.read())
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
	value = state()
	value["busy"] = bool(frappe.cache.get(lock_key()))
	from onerc_vmms_migrate.batches import checkpoint, public_state
	batch = checkpoint()
	if batch:
		value["batch"] = public_state(batch)
	from onerc_vmms_migrate.trial_attachments import state as attachment_state, public as attachment_public
	value["attachment_batch"] = attachment_public(attachment_state())
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
						"files": sorted(f for f in os.listdir(directory) if f.endswith((".csv", ".json")))[-100:],
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
) -> dict:
	authorize()
	if action not in ACTIONS:
		frappe.throw("Unknown migration action.")
	if resume_capture_id:
		_validate_resume_capture(action, resume_capture_id)
	settings = frappe.get_single(SETTINGS)
	if action not in ("capture", "audit_logins") and not settings.browser_capture:
		frappe.throw("Capture the old site first.")
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
	if existing_batch and action not in ("check", "selftest", "profile", "plan", "reconcile"):
		frappe.throw("A batch session already exists. Use its Pause/Resume controls; trial records cannot be promoted to a live import.")
	if action == "batch_test" or (action == "import" and batch_size):
		if action == "batch_test" and (confirmation != "TEST" or not test_destination or not backup_confirmed):
			frappe.throw("Confirm TEST, a disposable test destination, and a current backup. Test batches retain destination records.")
		size = batches.validate_size(batch_size or 1000)
		batch_browser.assert_job_idle()
		frappe.cache.delete(batch_browser.pause_key())
		return batch_browser.queue(action, frappe.session.user, size)
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
		if action != "capture":
			value.pop("transfer", None)
		save_state(value)
		frappe.enqueue(
			"onerc_vmms_migrate.browser.execute",
			queue=frappe.conf.get("onerc_vmms_migrate_queue") or "long",
			timeout=18000,
			job_id=token,
			token=token,
			action=action,
			user=frappe.session.user,
		)
	except Exception:
		frappe.cache.delete(lock_key())
		raise
	return {"job_id": token}



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

	from onerc_vmms_migrate import run, full_run, full_reconcile
	from onerc_vmms_migrate.browser_source import capture

	if frappe.cache.get(lock_key()) != token.encode():
		return
	value = state()
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
			if frappe.db.count("Legacy Migration Record"):
				frappe.throw(
					"This target already has migration records. Keep its capture for reconciliation; use a fresh target for another capture."
				)
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
				"rehearsal": full_run.execute,
				"reconcile": full_reconcile.execute,
			}
			result = funcs[action](progress=report_progress) if action == "rehearsal" else funcs[action]()
			if action == "rehearsal":
				value["rehearsal"] = {
					"fingerprint": fingerprint(),
					"passed": bool(result.get("passed")),
					"authentication_policy": result.get("authentication_policy"),
				}
			if action == "reconcile":
				value["reconciled"] = not result.get("mismatches") and bool(result.get("people_checked"))
		value.update({"status": "finished", "result": result, "finished_at": str(now_datetime())})
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
def finish_cutover(confirmation: str = "") -> None:
	authorize()
	assert_idle()
	value = state()
	original = value.get("cutover")
	if not original:
		frappe.throw("There is no migration cutover to finish.")
	if confirmation != "RESTORE" and (
		not value.get("reconciled")
		or not (value.get("import_result") or {}).get("passed")
	):
		frappe.throw("Resolve import failures and run reconciliation successfully before finishing cutover.")
	from frappe.installer import update_site_config
	from frappe.utils.scheduler import toggle_scheduler

	toggle_scheduler(original["scheduler_enabled"])
	update_site_config("mute_emails", original["mute_emails"])
	value.pop("cutover", None)
	value["services_restored_after_failure"] = confirmation == "RESTORE"
	save_state(value)


@frappe.whitelist(methods=["POST"])
def clear_capture() -> None:
	authorize()
	assert_idle()
	from onerc_vmms_migrate.batches import checkpoint
	if checkpoint():
		frappe.throw("The capture belongs to a batch session. Preserve it for recovery; restore the target backup or use a fresh target for another capture.")
	if frappe.db.count("Legacy Migration Record"):
		frappe.throw("A capture cannot be replaced after records have been migrated.")
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
