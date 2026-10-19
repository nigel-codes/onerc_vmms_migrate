"""Browser controls for independent, checkpointed background batches."""

import uuid

import frappe

from onerc_vmms_migrate import batches, browser, workspace


def pause_key():
	return frappe.cache.make_key("onerc_vmms_migrate:batch-pause")


def assert_job_idle():
	"""Release only a lock whose RQ job is positively known to have ended."""
	token = frappe.cache.get(browser.lock_key())
	if token:
		from frappe.utils.background_jobs import get_job_status

		job_id = token.decode() if isinstance(token, bytes) else str(token)
		status = get_job_status(job_id)
		if status in ("failed", "finished", "stopped", "canceled"):
			if frappe.cache.get(browser.lock_key()) == token:
				frappe.cache.delete(browser.lock_key())
		else:
			frappe.throw(
				"A migration job is queued/running or its state is unknown. Wait for the worker; it was not stopped."
			)


def reserve_lock():
	token = uuid.uuid4().hex
	if not frappe.cache.set(browser.lock_key(), token, nx=True, ex=21600):
		frappe.throw("Another migration job is active.")
	return token


def queue(action, user, size, token=None):
	token = token or reserve_lock()
	if frappe.cache.get(browser.lock_key()) != token.encode():
		frappe.throw("The migration lock changed; no job was queued.")
	value = browser.state()
	value.update(
		action=action,
		job_id=token,
		status="queued",
		error=None,
		result=None,
		started_by=user,
		started_at=str(frappe.utils.now_datetime()),
	)
	value.pop("transfer", None)
	batch = batches.checkpoint()
	value["progress"] = (
		{"phase": batch["phase"], "processed": batch["offset"], "total": batch.get("total")}
		if batch
		else {"phase": "initializing"}
	)
	value["progress_updated_at"] = str(frappe.utils.now_datetime())
	browser.save_state(value)
	try:
		frappe.enqueue(
			"onerc_vmms_migrate.batch_browser.execute",
			queue=frappe.conf.get("onerc_vmms_migrate_queue") or "long",
			timeout=900,
			job_id=token,
			token=token,
			action=action,
			user=user,
			size=size,
		)
	except Exception:
		if frappe.cache.get(browser.lock_key()) == token.encode():
			frappe.cache.delete(browser.lock_key())
		raise
	return {"job_id": token}


@frappe.whitelist(methods=["POST"])
def pause():
	browser.authorize()
	if not batches.checkpoint():
		frappe.throw("There is no batch session.")
	frappe.cache.set(pause_key(), 1)
	return {"message": "Pause requested. The current batch will finish and commit first."}


@frappe.whitelist(methods=["POST"])
def resume(batch_size: int = 1000, accept_changes: bool = False):
	browser.authorize()
	assert_job_idle()
	size = batches.validate_size(batch_size)
	token = reserve_lock()
	try:
		return resume_locked(size, accept_changes, token)
	except Exception:
		if frappe.cache.get(browser.lock_key()) == token.encode():
			frappe.cache.delete(browser.lock_key())
		raise


def resume_locked(size, accept_changes, token):
	value = batches.checkpoint()
	if not value or value["status"] == "finished":
		frappe.throw("There is no unfinished batch session to resume.")
	current = browser.fingerprint()
	if current != value["fingerprint"]:
		# Trial work is not a cutover approval. Record every reviewed code or
		# mapping revision; never silently bless new inputs for a live import.
		if value["mode"] != "test" or not accept_changes:
			frappe.throw(
				"Code, capture, settings or mappings changed. A test session needs explicit review; live imports cannot change inputs midway."
			)
		if workspace.config().get("browser_capture") != value["capture_id"]:
			frappe.throw("The selected capture changed. Restore the original capture before resuming.")
		value.setdefault("versions_reviewed", []).append(value["fingerprint"])
		value["fingerprint"] = current
		value["passed"] = False
	value.update(batch_size=size, status="ready", error=None)
	batches.commit_checkpoint(value)
	frappe.cache.delete(pause_key())
	return queue(
		"batch_test" if value["mode"] == "test" else "import", frappe.session.user, size, token=token
	)


def begin_cutover(value):
	from frappe.installer import update_site_config
	from frappe.utils.scheduler import disable_scheduler

	if not value.get("cutover"):
		value["cutover"] = {
			"scheduler_enabled": bool(frappe.db.get_single_value("System Settings", "enable_scheduler")),
			"mute_emails": frappe.conf.get("mute_emails", 0),
		}
		browser.save_state(value)
	disable_scheduler()
	frappe.db.commit()
	update_site_config("mute_emails", 1)
	frappe.conf.mute_emails = 1


def initialize(action, size, conf, source):
	from onerc_vmms_migrate import login_policy, run

	if not frappe.db.exists("DocType", batches.DOCTYPE):
		frappe.throw("Run bench migrate to install batch checkpoint storage before starting.")
	if not conf.get("preserve_passwords"):
		frappe.throw("Enable protected password preservation first.")
	if not source.data.get("schemas") or not source.data.get("history"):
		frappe.throw("A full captured snapshot is required.")
	if action == "batch_test" and frappe.db.count("Legacy Migration Record"):
		frappe.throw(
			"Start the batch test on a target without migration ledger records; use Resume for an existing batch session."
		)
	if (
		action == "import"
		and source.data.get("capture_complete") is False
		and not conf.get("allow_missing_files")
	):
		frappe.throw("Review and explicitly approve attachment exceptions before live import.")
	checks = run.selftest()
	if not checks.get("passed"):
		frappe.throw("The application self-test failed. Review its report before starting batches.")
	value = {
		"session_id": uuid.uuid4().hex,
		"mode": "test" if action == "batch_test" else "import",
		"run_id": run._run_id("batch-test" if action == "batch_test" else "batch-import"),
		"phase": "preflight",
		"offset": 0,
		"batch_number": 0,
		"batch_size": size,
		"status": "ready",
		"failed": 0,
		"passed": False,
		"fingerprint": browser.fingerprint(),
		"capture_id": conf.get("browser_capture"),
		"authentication_policy": login_policy.plan(source.data, conf),
	}
	batches.commit_checkpoint(value)
	return value


def execute(token, action, user, size):
	"""Every invocation owns only one batch, with a fresh worker timeout."""
	from onerc_vmms_migrate.source import Source

	if frappe.cache.get(browser.lock_key()) != token.encode():
		return
	automatic_next = False
	view = browser.state()
	value = None
	try:
		frappe.set_user(user)
		browser.authorize()
		conf = workspace.config()
		value = batches.checkpoint()
		if value:
			if value["fingerprint"] != browser.fingerprint():
				frappe.throw("Inputs changed. Resume only after reviewing the change.")
			if value["mode"] != ("test" if action == "batch_test" else "import"):
				frappe.throw(
					"A different batch session already exists. Trial records cannot authorize live cutover."
				)
			if value["status"] == "finished":
				frappe.throw("This batch session already finished.")
		if action == "import":
			rehearsal = view.get("rehearsal") or {}
			if not rehearsal.get("passed") or rehearsal.get("fingerprint") != browser.fingerprint():
				frappe.throw(
					"A successful full rehearsal with the current inputs is required for live import."
				)
		begin_cutover(view)
		with Source(conf) as source:
			if not value:
				value = initialize(action, size, conf, source)
			if frappe.cache.get(pause_key()):
				value["status"] = "paused"
				batches.commit_checkpoint(value)
			else:
				view.update(status="running", batch=batches.public_state(value))
				browser.save_state(view)
				batches.step(value, source, conf, progress=browser._progress_reporter(view))
				if value["status"] == "ready" and frappe.cache.get(pause_key()):
					value["status"] = "paused"
					batches.commit_checkpoint(value)
				automatic_next = value["status"] == "ready"
		view["batch"] = batches.public_state(value)
		view.update(
			status="finished" if value["status"] == "finished" else value["status"],
			result=batches.public_state(value),
			error=None,
		)
		if action == "import" and value["status"] == "finished":
			view["import_result"] = {"passed": value["passed"], "run_id": value["run_id"]}
			view["reconciled"] = bool(value["passed"])
	except Exception as exc:
		frappe.db.rollback()
		# Reload the LAST committed cursor, not mutated in-memory state from
		# work that just rolled back. Hard worker death needs no cursor repair.
		durable = batches.checkpoint()
		from onerc_vmms_migrate.source import SourceError

		safe = isinstance(exc, (frappe.ValidationError, frappe.PermissionError, SourceError))
		message = (
			frappe.utils.strip_html(str(exc))
			if safe
			else f"{type(exc).__name__}: batch stopped. Review worker logs and retry the saved checkpoint."
		)
		if durable:
			durable.update(status="blocked", error=message)
			batches.commit_checkpoint(durable)
			view["batch"] = batches.public_state(durable)
		view.update(status="failed", error=message)
		frappe.log_error(title="VMMS migration batch stopped")
	finally:
		browser.save_state(view)
		if frappe.cache.get(browser.lock_key()) == token.encode():
			frappe.cache.delete(browser.lock_key())
	if automatic_next:
		try:
			queue(action, user, size)
		except Exception:
			# Cursor is already durable. Resume can recover an enqueue failure.
			frappe.log_error(title="VMMS next batch could not be queued; resume from checkpoint")
