"""Write one bundle to the target site: all of it or none of it.

The order is the order vmmsx itself creates a person in: login -> Red Profile ->
files -> volunteer + the application that makes it Active -> member +
memberships -> ledger rows -> `refresh()`, which writes core's affiliation index
and grants the society's self-service roles.

**Where this goes around vmmsx, and why each detour is safe.**

* *The volunteer's application is inserted as a Draft that already names its
  volunteer, then marked Approved from the recorded source status.* Going
  through the approval engine would re-run today's approval gate (an emergency
  contact, a verified guardian) against a decision the society made years ago
  under different rules. vmmsx's own `validate_approval` docstring says an old
  approval must not be re-judged. Linking the volunteer first means
  `try_accept()` has nothing to create.
* *A membership is inserted already Active or Expired, with its old dates.*
  `activate()` would restart the clock today. Inserting in a final state makes
  `try_activate()` a no-op (Active is not activatable) and keeps the dates.
* *Nothing is sent.* See `silence.py`.

Everything else goes through vmmsx's services: `volunteer.ensure`,
`member.ensure`, `refresh()` and `token.ensure`. The records therefore carry
the product's native links and roles without inventing historical actors.
"""

import os
from copy import deepcopy

import frappe
from frappe.utils import now_datetime

from onerc_vmms_migrate.silence import Guard

LEDGER = "Legacy Migration Record"

PROFILE_SCALARS = (
	"legacy_phone",
	"legacy_untyped_id_number",
	"legacy_identity_claims",
	"first_name",
	"middle_name",
	"last_name",
	"phone",
	"gender",
	"date_of_birth",
	"marital_status",
	"citizenship_status",
	"country_of_citizenship",
	"residency_type",
	"home_geo_node",
	"vmms_disability_status",
	"vmms_profession",
	"vmms_other_profession",
)
PROFILE_TABLES = (
	"identifications",
	"vmms_disabilities",
	"vmms_education",
	"vmms_work_experience",
	"vmms_references",
	"vmms_training",
	"vmms_licences",
	"vmms_driving_licences",
)


def already_migrated(key: str) -> dict | None:
	row = frappe.db.get_value(LEDGER, {"person_key": key}, ["source_hash", "run_id"], as_dict=True)
	return row


def load_bundle(bundle: dict, source, conf, run_id: str, dry_run: bool = False) -> dict:
	"""Write the bundle. The caller owns the transaction (commit or rollback)."""
	guard = Guard()
	ctx = {
		"bundle": bundle,
		"source": source,
		"conf": conf,
		"run_id": run_id,
		"dry_run": dry_run,
		"ledger": [],
		"warnings": [],
	}

	user = _user(ctx)
	profile = _profile(ctx, user)
	_files(ctx, profile)

	volunteer = _volunteer(ctx, profile) if bundle["volunteer"] else None
	member = _member(ctx, profile) if bundle.get("member") else None

	for row in ctx["ledger"]:
		existing=frappe.db.get_value(LEDGER,{"source_doctype":row["source_doctype"],
			"source_name":row["source_name"],"target_doctype":row["target_doctype"]},"target_name")
		if existing:
			if existing!=row["target_name"]:
				raise ValueError("An existing source mapping points to a different destination record; it was not overwritten.")
			continue
		frappe.get_doc(
			{
				"doctype": LEDGER,
				"person_key": bundle["key"],
				"source_hash": bundle["source_hash"],
				"run_id": run_id,
				"migrated_on": now_datetime(),
				**row,
			}
		).insert(ignore_permissions=True)

	# Affiliations, derived statuses, self-service roles. After the ledger so a
	# failure here rolls the ledger back with everything else.
	from vmmsx.cards.services import token
	from vmmsx.member.services import member as member_service
	from vmmsx.volunteer.services import volunteer as volunteer_service

	if volunteer:
		volunteer_service.refresh(volunteer)
		token.ensure(frappe.get_doc("VMMS Volunteer", volunteer))
	if member:
		member_service.refresh(member)
		for name in frappe.get_all(
			"VMMS Membership", {"member": member, "membership_status": "Active"}, pluck="name"
		):
			token.ensure(frappe.get_doc("VMMS Membership", name))

	guard.check()

	return {
		"user": user,
		"profile": profile,
		"volunteer": volunteer,
		"member": member,
		"warnings": ctx["warnings"],
		"phone_review": ctx.get("phone_review", []),
	}


def _ledger(ctx, source_doctype, source_name, target_doctype, target_name, outcome="created"):
	ctx["ledger"].append(
		{
			"source_doctype": source_doctype,
			"source_name": source_name,
			"target_doctype": target_doctype,
			"target_name": target_name,
			"outcome": outcome,
		}
	)


# --- login ----------------------------------------------------------------------


def _user(ctx) -> str | None:
	login = ctx["bundle"]["user"]
	profile = ctx["bundle"]["profile"]

	if not login:
		return None

	email = login["email"]

	if frappe.db.exists("User", email):
		if ctx["conf"].get("preserve_passwords") and login.get("password_hash"):
			import hmac

			rows = frappe.db.sql("SELECT password FROM __Auth WHERE doctype='User' AND name=%s AND fieldname='password' AND encrypted=0", (email,))
			if not rows or not hmac.compare_digest(str(rows[0][0]), login["password_hash"]):
				raise ValueError("An existing target login has a different password credential. Resolve the account conflict before migration; its password was not changed.")
		# Somebody already signed up on the new site, or it is a staff account
		# made by hand. Linked, never altered: not the password, not the roles.
		ctx["warnings"].append(f"login {email} already existed on this site; linked, not changed")
		_ledger(ctx, "User", login["source_name"], "User", email, "adopted")
		return email

	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": profile["first_name"],
			"middle_name": profile.get("middle_name"),
			"last_name": profile["last_name"],
			"enabled": login["enabled"],
			# A volunteer or member uses the portal. Old volunteers were System
			# Users with a role profile. That grant does not carry over.
			"user_type": "Website User",
			"send_welcome_email": 0,
			"social_logins": [dict(row) for row in login["social_logins"]],
		}
	)
	user.flags.no_welcome_mail = True
	if "buzz" in frappe.get_installed_apps():
		# Buzz grants this basic portal role to every new user. Include the same
		# role in the first native insert, avoiding its redundant second save.
		user.append("roles", {"role":"Buzz User"})
	original_add_roles=user.add_roles
	def add_missing_roles(*roles):
		current={r.role for r in user.roles}
		missing=[role for role in roles if role not in current]
		if missing:return original_add_roles(*missing)
	user.add_roles=add_missing_roles
	for field in ("username", "mobile_no", "login_after", "login_before", "restrict_ip", "language", "time_zone"):
		if login.get(field) is not None and frappe.get_meta("User").has_field(field):
			user.set(field, login[field])
	# Use Frappe's supported bulk-import mode in this process only.
	# Ordinary signup throttling otherwise rejects every user after the first 61.
	previous_import = frappe.flags.in_import
	try:
		frappe.flags.in_import = True
		user.insert(ignore_permissions=True)
	finally:
		frappe.flags.in_import = previous_import
		user.add_roles = original_add_roles

	if login["password_hash"]:
		# A passlib hash, independent of either site's encryption key, so the
		# person keeps the password they already have.
		frappe.db.sql(
			"""insert into `__Auth` (doctype, name, fieldname, `password`, encrypted)
			values ('User', %s, 'password', %s, 0)
			on duplicate key update `password` = values(`password`), encrypted = 0""",
			(user.name, login["password_hash"]),
		)

	_ledger(ctx, "User", login["source_name"], "User", user.name)
	return user.name


# --- profile --------------------------------------------------------------------


def _clean_rows(rows):
	return [{k: v for k, v in row.items() if not k.startswith("_") and v not in (None, "")} for row in rows]


def _profile(ctx, user) -> str:
	bundle = ctx["bundle"]
	data = deepcopy(bundle["profile"])
	meta = frappe.get_meta("Red Profile")

	_check_links(ctx, data)
	_review_profile_phones(ctx, data)

	existing = frappe.db.get_value("Red Profile", {"email": data["email"]}) or (
		user and frappe.db.get_value("Red Profile", {"user": user})
	)

	if existing:
		doc = frappe.get_doc("Red Profile", existing)
		for field in PROFILE_SCALARS:
			if meta.has_field(field) and not doc.get(field) and data.get(field):
				doc.set(field, data[field])
		for field in PROFILE_TABLES:
			if meta.has_field(field) and not doc.get(field) and data.get(field):
				doc.set(field, _clean_rows(data[field]))
		if user and not doc.user and not frappe.db.exists("Red Profile", {"user": user}):
			doc.user = user
		doc.save(ignore_permissions=True)
		ctx["warnings"].append("a profile with this email already existed; only its empty fields were filled")
		_ledger(ctx, "Person", bundle["key"], "Red Profile", doc.name, "adopted")
		return doc.name

	missing = [f for f in (*PROFILE_SCALARS, *PROFILE_TABLES) if data.get(f) and not meta.has_field(f)]
	if missing:
		ctx["warnings"].append("this site has no field for: " + ", ".join(missing))

	values = {"doctype": "Red Profile", "email": data["email"], "user": user}
	for field in PROFILE_SCALARS:
		if meta.has_field(field) and data.get(field):
			values[field] = data[field]
	for field in PROFILE_TABLES:
		if meta.has_field(field) and data.get(field):
			values[field] = _clean_rows(data[field])

	doc = frappe.get_doc(values)
	doc.insert(ignore_permissions=True)
	_ledger(ctx, "Person", bundle["key"], "Red Profile", doc.name)
	return doc.name


def _check_links(ctx, data) -> None:
	"""Plain Link values that were not crosswalked: drop them if absent here."""
	for field, doctype in (("country_of_citizenship", "Country"),):
		if data.get(field) and not frappe.db.exists(doctype, data[field]):
			ctx["warnings"].append(f"{doctype} {data[field]!r} does not exist here; not carried")
			data[field] = None


def _check_phone(ctx, data, field="phone", label="phone", society=True, required=False) -> None:
	if not data.get(field):
		return
	from onerc_core.society.services import config

	try:
		# Red Profile also runs Frappe's Data/Phone validator, even when the
		# society has no custom pattern. Apply that same rule before insertion
		# so the explicit review setting can retain the original and omit it.
		frappe.utils.validate_phone_number(data[field], throw=True)
		if society:
			config.validate_phone_number(data[field])
	except frappe.ValidationError:
		frappe.clear_last_message()
		if not ctx["conf"].get("drop_invalid_phones"):
			raise
		action = "contact omitted" if required else "phone left blank"
		ctx["warnings"].append(f"{label}: {data[field]} fails destination phone validation; {action}; original retained in source capture/archive")
		ctx.setdefault("phone_review", []).append({"field": label, "original_value": data[field], "action": action})
		data[field] = None


def _review_profile_phones(ctx, data):
	_check_phone(ctx, data)
	for index, row in enumerate(data.get("vmms_references") or [], 1):
		_check_phone(ctx, row, label=f"vmms_references[{index}].phone", society=False)


def _review_emergency_contacts(ctx, rows):
	# A contact requires a primary phone. Keep its original in the source,
	# rather than inventing a replacement or relaxing ordinary signup rules.
	contacts = deepcopy(rows)
	retained = []
	for index, row in enumerate(contacts, 1):
		_check_phone(ctx, row, "primary_phone", f"emergency_contacts[{index}].primary_phone", society=False, required=True)
		if not row.get("primary_phone"):
			continue
		_check_phone(ctx, row, "alternative_phone", f"emergency_contacts[{index}].alternative_phone", society=False)
		retained.append(row)
	return retained


# --- files ----------------------------------------------------------------------


def _files(ctx, profile: str) -> None:
	files = ctx["bundle"]["files"]
	if not files:
		return

	doc = frappe.get_doc("Red Profile", profile)
	changed = False

	for item in files:
		url = _copy_file(ctx, item["url"], "Red Profile", profile, private=item["purpose"] != "photo")
		if not url:
			continue

		if item["purpose"] == "photo" and not doc.get("profile_photo"):
			doc.profile_photo = url
			changed = True
		elif item["purpose"] == "identification":
			rows = doc.get("identifications") or []
			if item["index"] < len(rows) and not rows[item["index"]].get("attachment"):
				rows[item["index"]].attachment = url
				changed = True
		elif item["purpose"] == "table":
			rows = doc.get(item["table"]) or []
			if item["index"] < len(rows) and not rows[item["index"]].get("attachment"):
				rows[item["index"]].attachment = url
				changed = True

	if changed:
		doc.save(ignore_permissions=True)


def _copy_file(ctx, url: str, doctype: str, name: str, private: bool) -> str | None:
	path = ctx["source"].file_path(url)
	if not path:
		if not ctx.get("dry_run") and getattr(ctx["source"], "data", {}).get("source", {}).get("protocol") in (1, 2) and not ctx.get("conf", {}).get("allow_missing_files"):
			raise ValueError("A required attachment is unavailable in the bulk capture.")
		ctx["warnings"].append(f"file {url} is not on the old site's disk; not carried")
		return None
	if hasattr(ctx["source"], "verify_file"):
		ctx["source"].verify_file(url, path)
	manifest = getattr(ctx["source"], "data", {}).get("captured_file_manifest", {}).get(url, {})
	# Source-private photos must not become public when assigned as a photo.
	private = private or bool(manifest.get("is_private"))

	if ctx.get("dry_run"):
		# A rollback undoes the File row but not the bytes written to disk, so a
		# rehearsal checks the file is there and writes nothing.
		return None

	from onerc_vmms_migrate.file_transfer import copy_verified
	return copy_verified(path, ctx["source"].file_name(url) if hasattr(ctx["source"], "file_name") else os.path.basename(path),
		doctype, name, private, manifest.get("sha256"))


# --- volunteer ------------------------------------------------------------------


def _volunteer(ctx, profile: str) -> str:
	from vmmsx.volunteer.services import volunteer as volunteer_service

	data = ctx["bundle"]["volunteer"]
	application = ctx["bundle"]["application"]

	existing = frappe.db.get_value("VMMS Volunteer", {"red_profile": profile})
	if existing:
		ctx["warnings"].append(f"already a volunteer here ({existing}); old record linked, nothing changed")
		_ledger(ctx, "Employee", data["source_name"], "VMMS Volunteer", existing, "adopted")
		return existing

	volunteer = volunteer_service.ensure(profile, data["home_geo_node"])
	volunteer.joined_on = data["joined_on"]
	volunteer.set("skills", [{"skill": s} for s in data["skills"]])
	volunteer.set("languages", [{"language": lang} for lang in data["languages"]])

	if data["status"] in volunteer_service.EXPLICIT_STATUSES:
		# Suspended and Exited are acts, and derive_status() keeps them.
		volunteer.status = data["status"]
		volunteer.exited_on = data["exited_on"]

	volunteer.save(ignore_permissions=True)
	_ledger(ctx, "Employee", data["source_name"], "VMMS Volunteer", volunteer.name)

	app = frappe.get_doc(
		{
			"doctype": "VMMS Volunteer Application",
			"red_profile": profile,
			"geo_node": application["geo_node"],
			"applied_on": application["applied_on"],
			"volunteer": volunteer.name,
			"skills": [{"skill": s} for s in application["skills"]],
			"languages": [{"language": lang} for lang in application["languages"]],
			"emergency_contacts": _review_emergency_contacts(ctx, application["emergency_contacts"]),
			"custom_answers": [_answer(row) for row in application["custom_answers"]],
			"approval_state": "Draft",
		}
	)
	app.insert(ignore_permissions=True)

	_mark_approved(app)
	_ledger(
		ctx, application["source_doctype"], application["source_name"], "VMMS Volunteer Application", app.name
	)
	return volunteer.name


def _answer(row: dict) -> dict:
	question = frappe.get_cached_doc("VMMS Application Question", row["question"])
	return {
		"question": question.name,
		"question_label": question.question_label,
		"question_group": question.question_group,
		"field_type": question.field_type,
		"answer_value": row["answer_value"],
	}


def _mark_approved(doc) -> None:
	"""Carry the source status without inventing an approver, decision date or audit row."""
	frappe.db.set_value(
		doc.doctype,
		doc.name,
		{"approval_state": "Approved", "approval_stage": "", "approval_stage_entered_on": None},
		update_modified=False,
	)


# --- member ---------------------------------------------------------------------


def _member(ctx, profile: str) -> str:
	from vmmsx.member.services import member as member_service

	bundle = ctx["bundle"]
	data = bundle["member"]

	member = member_service.ensure(profile)
	if data["joined_on"] and not member.joined_on:
		member.db_set("joined_on", data["joined_on"], update_modified=False)
	for source_name in data["source_names"]:
		_ledger(ctx, "VM Member", source_name, "VMMS Member", member.name)

	for row in bundle["memberships"]:
		membership_type = frappe.get_cached_doc("VMMS Membership Type", row["membership_type"])
		valid_to = None if membership_type.is_lifetime else row["valid_to"]

		doc = frappe.get_doc(
			{
				"doctype": "VMMS Membership",
				"member": member.name,
				"membership_type": row["membership_type"],
				"geo_node": row["geo_node"],
				"membership_source": "Gateway",
				"membership_status": row["membership_status"],
				"valid_from": row["valid_from"],
				"valid_to": valid_to,
				"paid_on": row["paid_on"],
				"payment_receipt": row["payment_receipt"],
				# Decided in the old system. See the module docstring.
				"approval_state": row.get("approval_state", "Approved"),
			}
		)
		# Preserve the recorded state without triggering modern payment/approval
		# automation on Draft or Pending historical memberships.
		from onerc_vmms_migrate.historical import insert
		values = {k:v for k,v in doc.as_dict().items() if k not in ("doctype", "name")}
		doc._action = "save"
		doc.validate()
		doc = insert("VMMS Membership", values)

		_ledger(ctx, "VM Membership", row["source_name"], "VMMS Membership", doc.name)

	return member.name
