"""Explicit account-specific approvals for captured password restoration."""

import hashlib
import hmac
import json
from functools import partial

import frappe

from onerc_vmms_migrate import batches

# Release-scoped approval binds site, source, capture, session and both logins.
# It contains no password material. Add approvals only after an explicit decision.
APPROVED_RESTORATIONS = frozenset(
	{
		"1f99b513ec8b2ce4abee514dcff23f59e13ccc76ca236f316353637ab014b191",
	}
)


def approval_key(site, source_id, capture_id, session_id, source_user, target_user):
	return hashlib.sha256(
		json.dumps(
			[site, source_id, capture_id, session_id, source_user, target_user],
			separators=(",", ":"),
		).encode()
	).hexdigest()


def restore_approved(ctx, login):
	if not frappe.flags.vmms_migration_import or not ctx["conf"].get("preserve_passwords"):
		return False
	snapshot = getattr(ctx.get("source"), "data", {})
	source = snapshot.get("source", {})
	value = batches.checkpoint() or {}
	if any(
		(
			value.get("scope") != "people",
			value.get("mode") != "import",
			value.get("status") != "ready",
			value.get("phase") != "people",
			value.get("run_id") != ctx["run_id"],
			not source.get("source_id"),
			value.get("source_id") != source.get("source_id"),
			value.get("capture_id") != source.get("snapshot_id"),
			ctx["conf"].get("browser_capture") != source.get("snapshot_id"),
			not source.get("logins", {}).get("included"),
		)
	):
		return False
	key = approval_key(
		frappe.local.site,
		source["source_id"],
		value["capture_id"],
		value["session_id"],
		login.get("source_name"),
		login["email"],
	)
	if key not in APPROVED_RESTORATIONS:
		return False
	captured = snapshot.get("password_hashes", {}).get(login.get("source_name"))
	if (
		not captured
		or not login.get("password_hash")
		or not hmac.compare_digest(captured, login["password_hash"])
	):
		return False
	frappe.db.sql(
		"INSERT INTO __Auth (doctype, name, fieldname, `password`, encrypted) "
		"VALUES ('User', %s, 'password', %s, 0) "
		"ON DUPLICATE KEY UPDATE `password`=VALUES(`password`), encrypted=0",
		(login["email"], captured),
	)
	event = {
		"user": login["email"],
		"source_user": login["source_name"],
		"capture_id": value["capture_id"],
		"run_id": value["run_id"],
		"action": "restored approved captured source password",
		"actor": frappe.session.user,
	}
	audit = frappe.get_doc(
		{
			"doctype": "Activity Log",
			"user": frappe.session.user,
			"subject": "Approved migration password restoration from capture " + value["capture_id"],
			"reference_doctype": "User",
			"reference_name": login["email"],
			"operation": "",
			"status": "Success",
		}
	).insert(ignore_permissions=True)
	ctx.setdefault("password_restorations", []).append(event)
	# No commit: the credential, native audit and person ledger roll back
	# together if any later field on this person fails validation.
	frappe.db.after_commit.add(partial(_expire_sessions, login["email"], audit.name))
	return True


def _expire_sessions(user, audit_name):
	# Savepoint rollback can retain a batch after-commit callback. Only the
	# committed audit row authorises cleanup for that person's restored login.
	try:
		if not frappe.db.exists("Activity Log", audit_name):
			return
		from frappe.sessions import clear_sessions

		clear_sessions(user=user, keep_current=True, force=True)
	except Exception:
		# Cleanup outages cannot turn a committed page into a false import failure.
		try:
			frappe.log_error(
				title="Migration login session cleanup needs review",
				message="Approved password restoration committed; review session cleanup for Activity Log "
				+ audit_name,
			)
		except Exception:
			# The committed Activity Log remains the audit if logging is unavailable.
			pass
