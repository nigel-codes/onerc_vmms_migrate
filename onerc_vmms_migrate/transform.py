"""Snapshot + crosswalks -> one bundle per person. Pure: no database, no Frappe.

A **bundle** is everything one human being becomes on the new site: an optional
login, a Red Profile, and optionally a volunteer (with the one application that
makes it Active), a member and their memberships, and the files attached to
them. The loader writes a bundle in one transaction or not at all.

**Who is a person.** The old app spreads one human over User, Job Applicant,
Employee and VM Member. They are joined here on a lowercased email, which is
also core's key (`Red Profile.email` is unique). The login's own email is
preferred over the addresses typed into HR fields, because that is the one
the person signs in with.

**What stops a person from loading** is an entry in `bundle["errors"]`: no
email, no surname or an unreviewed crosswalk value. Conflicting ID claims
remain in a labelled review field without merging accounts. None of these is fixed by guessing. They go to rejects.csv, and a human
resolves them in the source data or in a crosswalk before the next run.

**Precedence for a fact held in several places:** User (the portal edits it,
so it is the latest) -> Employee -> the most recent Job Applicant. For a table
(education, references, ...) the first source that has any rows supplies all of
them. Tables are never merged across sources, because the old app copied them
applicant -> user and a merge would double every row.
"""

import datetime
import hashlib
import json
from collections import defaultdict

from onerc_vmms_migrate import normalise
from onerc_vmms_migrate.history_model import membership_state
from onerc_vmms_migrate.crosswalk import DROP, UNMAPPED, Crosswalks

# Deepest first. The first one the crosswalk maps is where the person lives.
RESIDENCE_FIELDS = ("sub_location", "administrative_location", "ward", "sub_county", "county")

MARITAL = {"Single", "Married", "Divorced", "Widowed", "Other"}
CITIZENSHIP = {"Citizen", "Non-citizen", "Refugee", "Migrant", "Other"}

# Old fields the new product deliberately treats as organization-specific
# questions rather than first-class data. The crosswalk `question` decides where
# each one goes, or that it stays in the archive.
QUESTION_FIELDS = (
	"blood_group",
	"allergies",
	"has_insurance",
	"access_to_internet",
	"number_of_dependants",
	"reason_to_join_krcs",
	"place_of_work",
	"mpesa_mobile_phone",
)

# Old roles that mean nothing more than "a volunteer or member with a login".
# Any other role on a migrated account is listed, so staff are re-provisioned
# deliberately on the new site instead of inheriting old grants.
PLAIN_ROLES = {
	"Volunteer",
	"Vmms Guest",
	"Employee",
	"Employee Self Service",
	"LMS Student",
	"Guest",
	"All",
	"Desk User",
	"Website User",
}

MIGRATED_MEMBERSHIP_STATUSES = {"Draft", "Pending", "Rejected", "Active", "Expired"}


def build(snapshot: dict, crosswalks: Crosswalks, conf: dict, today: datetime.date) -> dict:
	crosswalks.placeholders = {normalise.key(v) for v in conf.get("placeholder_values") or []}
	people, unplaced = _group(snapshot)
	bundles = [
		_bundle(key, person, snapshot, crosswalks, conf, today) for key, person in sorted(people.items())
	]
	_flag_shared_identifiers(bundles)

	return {
		"bundles": bundles,
		"not_migrated": unplaced + _not_migrated(snapshot, people),
	}


# --- who is a person ----------------------------------------------------------


def _user_email(snapshot: dict, user_name: str | None) -> str | None:
	user = snapshot["users"].get(user_name or "")
	if user:
		return normalise.email(user.get("email")) or normalise.email(user.get("name"))
	return normalise.email(user_name)


def employee_email(snapshot: dict, employee: dict) -> str | None:
	return (
		_user_email(snapshot, employee.get("user_id"))
		if employee.get("user_id") and snapshot["users"].get(employee["user_id"])
		else None
	) or next(
		filter(
			None,
			(
				normalise.email(employee.get(f))
				for f in ("personal_email", "prefered_email", "user_id", "company_email")
			),
		),
		None,
	)


def member_email(snapshot: dict, member: dict) -> str | None:
	if member.get("email_id"):
		found = _user_email(snapshot, member["email_id"])
		if found:
			return found
	employee = snapshot["employees"].get(member.get("volunteer") or "")
	return employee_email(snapshot, employee) if employee else None


def _group(snapshot: dict) -> tuple[dict, list]:
	people: dict[str, dict] = defaultdict(
		lambda: {"employees": [], "members": [], "applicants": [], "user": None}
	)
	unplaced = []

	for employee in snapshot["employees"].values():
		if not employee.get("is_volunteer"):
			continue
		email = employee_email(snapshot, employee)
		if not email:
			unplaced.append(_skip("Employee", employee["name"], "volunteer has no usable email"))
			continue
		people[email]["employees"].append(employee)

	for member in snapshot["members"].values():
		email = member_email(snapshot, member)
		if not email:
			unplaced.append(_skip("VM Member", member["name"], "member has no usable email"))
			continue
		people[email]["members"].append(member)

	# Preserve independent applications as people too. An application never
	# becomes an active volunteer merely because it was captured.
	for applicant in snapshot["applicants"].values():
		if applicant.get("is_volunteer"):
			email = normalise.email(applicant.get("email_id"))
			if email:
				people[email]  # Seed the identity; applications are attached below.
			else:
				unplaced.append(_skip("Job Applicant",applicant["name"],"application has no usable email"))

	users_by_email = {}
	for user in snapshot["users"].values():
		email = normalise.email(user.get("email")) or normalise.email(user.get("name"))
		if email:
			users_by_email[email] = user

	applicants_by_email = defaultdict(list)
	for applicant in snapshot["applicants"].values():
		email = normalise.email(applicant.get("email_id"))
		if email:
			applicants_by_email[email].append(applicant)

	for email, person in people.items():
		person["user"] = users_by_email.get(email)
		linked = {e.get("job_applicant") for e in person["employees"] if e.get("job_applicant")}
		chosen = {a["name"]: a for a in applicants_by_email.get(email, [])}
		for name in linked:
			if name in snapshot["applicants"]:
				chosen[name] = snapshot["applicants"][name]
		person["applicants"] = sorted(
			chosen.values(), key=lambda a: str(a.get("creation") or ""), reverse=True
		)

	return dict(people), unplaced


def _not_migrated(snapshot: dict, people: dict) -> list:
	"""Old records that are deliberately not loaded, so the counts add up."""
	skipped = []
	in_scope = {a["name"] for p in people.values() for a in p["applicants"]}

	for applicant in snapshot["applicants"].values():
		if applicant["name"] not in in_scope and applicant.get("is_volunteer"):
			skipped.append(
				_skip(
					"Job Applicant",
					applicant["name"],
					f"open application, never became a volunteer (status {applicant.get('status')}); decide it"
					" before the freeze",
				)
			)
	for membership in snapshot["memberships"].values():
		if membership.get("member") not in snapshot["members"]:
			skipped.append(_skip("VM Membership",membership["name"],
				"Source member record is absent; membership retained as unassigned history"))
	return skipped


def _skip(doctype: str, name: str, reason: str) -> dict:
	return {"source_doctype": doctype, "source_name": name, "reason": reason}


# --- one person -----------------------------------------------------------------


def _first(*values):
	for value in values:
		if value not in (None, "", [], 0):
			return value
	return None


def _first_table(field: str, *records) -> tuple[list, None]:
	"""All rows of `field` from the first record that has any. Never merged."""
	for record in records:
		if record and record.get(field):
			return record[field], None
	return [], None


def _bundle(email, person, snapshot, cw: Crosswalks, conf, today) -> dict:
	user = person["user"]
	employees = person["employees"]
	employee = employees[0] if employees else None
	applicant = person["applicants"][0] if person["applicants"] else None

	b = {
		"key": email,
		"errors": [],
		"warnings": [],
		"sources": [],
		"user": None,
		"profile": None,
		"volunteer": None,
		"application": None,
		"member": None,
		"memberships": [],
		"files": [],
	}

	if len(employees) > 1:
		b["volunteer_excluded"] = True
		b["warnings"].append("Multiple volunteer records share this account; volunteer identity requires review. Profile and member history remain eligible.")

	if user:
		b["sources"].append(("User", user["name"]))
	for record in employees:
		b["sources"].append(("Employee", record["name"]))
	for record in person["applicants"]:
		b["sources"].append(("Job Applicant", record["name"]))
	for record in person["members"]:
		b["sources"].append(("VM Member", record["name"]))

	b["profile"] = _profile(b, email, user, employee, applicant, person, snapshot, cw, conf, today)
	b["user"] = _login(b, email, user, snapshot)

	if employee and not b.get("volunteer_excluded"):
		_volunteer(b, employee, applicant, user, cw, conf, today)

	if person["members"]:
		_member(b, person["members"], snapshot, cw, conf, today)

	if not b["volunteer"] and not b["member"] and not person["applicants"] and not employees:
		b["errors"].append("nothing to migrate: no volunteer or member record")

	b["source_hash"] = _hash(user, employees, person["applicants"], person["members"], snapshot)
	return b


def _mapped(b, cw: Crosswalks, kind: str, value, required: bool = False):
	"""Look up one value; an unreviewed one is an error, a dropped one is None."""
	target = cw.lookup(kind, value)
	if required and target is None:
		b["errors"].append(f"missing required {kind} value")
		return None
	if target is UNMAPPED:
		b["errors"].append(f"unmapped {kind}: {normalise.text(value)!r} (fill in crosswalks/{kind}.csv)")
		return None
	if target == DROP:
		if required:
			b["errors"].append(f"{kind} {normalise.text(value)!r} is marked '-' (not migrated)")
		return None
	return target


def _profile(b, email, user, employee, applicant, person, snapshot, cw, conf, today) -> dict:
	u, e, a = user or {}, employee or {}, applicant or {}

	names = (
		normalise.names(u.get("first_name"), u.get("middle_name"), u.get("last_name"))
		or normalise.names(e.get("first_name"), e.get("middle_name"), e.get("last_name"))
		or normalise.names(a.get("first_name"), a.get("other_names"), a.get("surname"))
		or normalise.names(a.get("applicant_name"), None, None)
		or normalise.names(e.get("employee_name"), None, None)
		or next(
			(
				normalise.names(m.get("member_name"), None, None)
				for m in person["members"]
				if m.get("member_name")
			),
			None,
		)
	)

	if not names:
		b["errors"].append("no first and last name anywhere (core requires both)")
		names = ("", "", "")

	profile = {
		"email": email,
		"first_name": names[0],
		"middle_name": names[1],
		"last_name": names[2],
	}

	raw_phone = _first(u.get("mobile_no"), e.get("cell_number"), a.get("phone_number"), u.get("phone"))
	profile["phone"] = normalise.phone(raw_phone, conf.get("default_country_code"))
	profile["legacy_phone"] = str(raw_phone) if raw_phone else None

	gender = _first(u.get("gender"), e.get("gender"), a.get("gender"))
	if gender:
		profile["gender"] = _mapped(b, cw, "gender", gender)

	raw_dob = _first(
		u.get("birth_date"), u.get("date_of_birth"), e.get("date_of_birth"), a.get("date_of_birth")
	)
	dob = normalise.plausible_birth_date(raw_dob, today)
	if raw_dob and not dob:
		b["warnings"].append(f"implausible date of birth {raw_dob!r} not carried")
	profile["date_of_birth"] = dob

	marital = normalise.text(
		_first(u.get("marital_status"), e.get("marital_status"), a.get("marital_status"))
	)
	if marital in MARITAL:
		profile["marital_status"] = marital
	elif marital:
		b["warnings"].append(f"marital status {marital!r} is not one core knows; not carried")

	citizenship = normalise.text(_first(u.get("citizenship"), a.get("citizenship")))
	if citizenship in CITIZENSHIP:
		profile["citizenship_status"] = citizenship
	profile["country_of_citizenship"] = _first(
		u.get("country_of_citizenship"), a.get("country_of_citizenship")
	)

	# Residence: deepest mapped node, from the portal's copy first.
	for field in RESIDENCE_FIELDS:
		value = _first(u.get(field), a.get(field))
		if not value:
			continue
		node = cw.lookup("residence", f"{field}:{normalise.text(value)}")
		if node is UNMAPPED:
			b["warnings"].append(f"Unmapped {field} retained in source archive; using a recorded parent where available")
			continue
		if node == DROP:
			continue
		if node:
			profile["residency_type"] = "Local"
			profile["home_geo_node"] = node
			break

	profile["identifications"] = _identifications(b, u, a, cw)
	if b.get("untyped_id_number"):
		profile["legacy_untyped_id_number"] = b["untyped_id_number"]
	_supporting_documents(b, profile, u, a, cw)

	rows, _ = _first_table("disabilities", u, a)
	# A row saying "None" is somebody answering no, not a disability.
	disabilities = [
		r
		for r in rows
		if not cw.is_placeholder("disability", r.get("disability") or r.get("disability_name"))
	]
	chosen = []
	for row in disabilities:
		target = _mapped(b, cw, "disability", row.get("disability") or row.get("disability_name"))
		if target and target not in chosen:
			chosen.append(target)
	if disabilities:
		# They told us they have one, even if the kind is not carried. No rows is
		# not "No": nobody was asked the question the new product asks.
		profile["vmms_disability_status"] = "Yes"
		profile["vmms_disabilities"] = [{"disability": d} for d in chosen]

	profession = _first(a.get("profession"), u.get("profession"))
	if profession:
		target = _mapped(b, cw, "profession", profession)
		if target:
			profile["vmms_profession"] = target
			if target.casefold() == "other":
				profile["vmms_other_profession"] = normalise.text(profession)

	profile["vmms_education"] = _education(b, u, e, a, cw)
	profile["vmms_work_experience"] = _work_experience(u, e, a)
	profile["vmms_references"] = [
		{
			"reference_name": normalise.text(r.get("reference_name")),
			"position": normalise.text(r.get("position")),
			"organization": normalise.text(r.get("organization")),
			"email": normalise.email(r.get("email")),
			"legacy_email": str(r.get("email")) if r.get("email") and not normalise.email(r.get("email")) else None,
			"phone": normalise.phone(r.get("phone_number"), conf.get("default_country_code")),
			"notes": r.get("notes"),
		}
		for r in _first_table("work_references", u, a)[0]
		if normalise.text(r.get("reference_name"))
	]
	profile["vmms_training"] = _training(b, u, a)
	profile["vmms_licences"] = _licences(b, u, a, cw)
	profile["vmms_driving_licences"] = _driving(b, u, a, cw)

	photo = _first(a.get("profile_photo"), u.get("user_image"), e.get("image"))
	if photo:
		b["files"].append({"url": photo, "purpose": "photo"})

	return profile


def _identifications(b, u, a, cw) -> list[dict]:
	rows = []
	id_type = _first(u.get("identification_type"), a.get("identification_type"))
	id_number = normalise.text(_first(u.get("id_number"), a.get("id_number")))
	if cw.is_placeholder("id_number", id_number):
		id_number = ""

	if id_number:
		if not id_type:
			b["untyped_id_number"] = id_number
			b["warnings"].append("Identification number has no recorded type; retained as an untyped legacy identifier")
		else:
			target = _mapped(b, cw, "identification_type", id_type)
			if target:
				rows.append({"id_type": target, "id_number": id_number, "is_primary": 1})

	passport = normalise.text(_first(u.get("passport_number"), a.get("passport_number")))
	if cw.is_placeholder("passport_number", passport):
		passport = ""
	if passport:
		target = _mapped(b, cw, "identification_type", "__passport_number__")
		if target:
			rows.append({"id_type": target, "id_number": passport, "is_primary": 0 if rows else 1})

	return rows


def _supporting_documents(b, profile, u, a, cw) -> None:
	documents, _ = _first_table("supporting_documents", u, a)
	for row in documents:
		if not row.get("attachment"):
			continue
		target = _mapped(b, cw, "supporting_document", row.get("type"))
		if not target:
			continue
		if target == "file":
			b["files"].append({"url": row["attachment"], "purpose": "file"})
			continue
		match = next(
			(r for r in profile["identifications"] if r["id_type"] == target and not r.get("_scan")), None
		)
		if not match:
			match = {"id_type": target, "id_number": "", "is_primary": 0 if profile["identifications"] else 1}
			profile["identifications"].append(match)
		match["_scan"] = row["attachment"]
		b["files"].append(
			{
				"url": row["attachment"],
				"purpose": "identification",
				"index": profile["identifications"].index(match),
			}
		)


def _education(b, u, e, a, cw) -> list[dict]:
	# User.education is the LMS "Education Detail" shape; the others are HRMS.
	if u.get("education"):
		out = []
		for r in u["education"]:
			institution = normalise.text(r.get("institution_name"))
			if not institution:
				continue
			level = _mapped(b, cw, "education_level", r.get("degree_type")) if r.get("degree_type") else None
			out.append(
				{
					"institution": institution,
					"level": level,
					"qualification": normalise.text(
						" ".join(filter(None, [r.get("degree_type"), r.get("major")]))
					),
					"started_in": normalise.year(r.get("start_date")),
					"finished_in": normalise.year(r.get("end_date")),
				}
			)
		return [_education_limits(b,item) for item in out]

	rows, _ = _first_table("education", e, a)
	out = []
	for r in rows:
		institution = normalise.text(r.get("school_univ"))
		if not institution:
			continue
		level = _mapped(b, cw, "education_level", r.get("level")) if r.get("level") else None
		row = {
			"institution": institution,
			"level": level,
			"qualification": normalise.text(r.get("qualification")),
			"finished_in": normalise.year(r.get("year_of_passing")),
		}
		if r.get("attachment"):
			b["files"].append(
				{"url": r["attachment"], "purpose": "table", "table": "vmms_education", "index": len(out)}
			)
		out.append(row)
	return [_education_limits(b,item) for item in out]


def _education_limits(bundle,row):
	for field in ("institution","qualification"):
		value=row.get(field)
		if isinstance(value,str) and len(value)>140:
			row["legacy_"+field]=value
			row[field]=value[:105]+"… [see original text]"
			bundle["warnings"].append("Long education text preserved in a legacy detail field; the display uses an explicit excerpt")
	return row


def _work_experience(u, e, a) -> list[dict]:
	rows, _ = _first_table("work_experience", u, a)
	out = [
		{
			"organization": normalise.text(r.get("company")),
			"role": normalise.text(r.get("title")),
			"started_on": normalise.date(r.get("from_date")),
			"ended_on": normalise.date(r.get("to_date")),
			"is_current": 1 if r.get("current") else 0,
			"summary": r.get("description"),
		}
		for r in rows
		if normalise.text(r.get("company"))
	]
	for r in u.get("internship") or []:
		if normalise.text(r.get("company")):
			out.append(
				{
					"organization": normalise.text(r.get("company")),
					"role": normalise.text(r.get("title")) or "Volunteering / internship",
					"started_on": normalise.date(r.get("from_date")),
					"ended_on": normalise.date(r.get("to_date")),
					"is_current": 1 if r.get("current") else 0,
					"summary": r.get("description"),
				}
			)
	if not out:
		out = [
			{
				"organization": normalise.text(r.get("company_name")),
				"role": normalise.text(r.get("designation")),
			}
			for r in e.get("external_work_history") or []
			if normalise.text(r.get("company_name"))
		]
	return out


def _training(b, u, a) -> list[dict]:
	out = []
	courses, _ = _first_table("courses", u, a)
	for r in courses:
		if not normalise.text(r.get("course_name")):
			continue
		if r.get("attachment"):
			b["files"].append(
				{"url": r["attachment"], "purpose": "table", "table": "vmms_training", "index": len(out)}
			)
		out.append(
			{
				"course_name": normalise.text(r.get("course_name")),
				"institution": normalise.text(r.get("institution")),
				"started_on": normalise.date(r.get("start_date")),
				"completed_on": normalise.date(r.get("date_completed")),
				"remarks": r.get("remarks"),
			}
		)
	certifications, _ = _first_table("certification", u, a)
	for r in certifications:
		if normalise.text(r.get("certification_name")):
			out.append(
				{
					"course_name": normalise.text(r.get("certification_name")),
					"institution": normalise.text(r.get("organization")),
					"completed_on": normalise.date(r.get("issue_date")),
					"remarks": r.get("description"),
				}
			)
	return out


def _licences(b, u, a, cw) -> list[dict]:
	out = []
	rows, _ = _first_table("licences", u, a)
	for r in rows:
		licence_type = _mapped(b, cw, "licence_type", r.get("license_type"))
		if not licence_type:
			continue
		if not (
			normalise.text(r.get("institution"))
			and normalise.text(r.get("qualification"))
			and r.get("valid_from")
		):
			b["warnings"].append(f"licence {r.get('license_type')!r} is missing a required part; not carried")
			continue
		if r.get("attachment"):
			b["files"].append(
				{"url": r["attachment"], "purpose": "table", "table": "vmms_licences", "index": len(out)}
			)
		out.append(
			{
				"license_type": licence_type,
				"institution": normalise.text(r.get("institution")),
				"qualification": normalise.text(r.get("qualification")),
				"registration_no": normalise.text(r.get("registration_no")),
				"license_name": normalise.text(r.get("license_name")),
				"description": r.get("description"),
				"valid_from": normalise.date(r.get("valid_from")),
				"valid_to": normalise.date(r.get("valid_to")),
				"does_not_expire": 1 if r.get("does_not_expire") else 0,
			}
		)
	return out


def _driving(b, u, a, cw) -> list[dict]:
	out = []
	rows, _ = _first_table("driving_licence", u, a)
	for r in rows:
		licence_class = _mapped(b, cw, "driving_licence_class", r.get("class"))
		if not licence_class:
			continue
		if r.get("attachment"):
			b["files"].append(
				{
					"url": r["attachment"],
					"purpose": "table",
					"table": "vmms_driving_licences",
					"index": len(out),
				}
			)
		out.append({"licence_class": licence_class})
	return out


def _login(b, email, user, snapshot) -> dict | None:
	if not user:
		return None
	roles = sorted({r.get("role") for r in user.get("roles") or [] if r.get("role")})
	unusual = [r for r in roles if r not in PLAIN_ROLES]
	if unusual:
		b["warnings"].append(
			"old login held roles beyond a volunteer's: " + ", ".join(unusual) + ". Grant these deliberately"
		)
	return {
		"email": email,
		"source_name": user["name"],
		"enabled": 1 if user.get("enabled") else 0,
		"password_hash": snapshot["password_hashes"].get(user["name"]),
		"username": user.get("username"),
		"mobile_no": user.get("mobile_no"),
		"login_after": user.get("login_after"),
		"login_before": user.get("login_before"),
		"restrict_ip": user.get("restrict_ip"),
		"social_logins": [
			{"provider": r.get("provider"), "userid": r.get("userid"), "username": r.get("username")}
			for r in user.get("social_logins") or []
			if r.get("provider") and r.get("userid")
		],
		"old_roles": roles,
		"last_login": user.get("last_login"),
	}


def _volunteer(b, employee, applicant, user, cw, conf, today) -> None:
	u, a = user or {}, applicant or {}

	# Everything is looked up before anything can stop this volunteer, so a
	# crosswalk generation run sees every value in use, not only the values of
	# people whose branch happens to be mapped already.
	geo = None
	if employee.get("company"):
		geo = _mapped(b, cw, "branch", employee["company"], required=True)
	else:
		b["errors"].append(f"Employee {employee['name']} has no company (branch)")
	status = _mapped(b, cw, "volunteer_status", employee.get("status") or "Active", required=True)

	skills = []
	for row in u.get("skill") or []:
		target = _mapped(b, cw, "skill", row.get("skill_name") or row.get("skill"))
		if target and target not in skills:
			skills.append(target)
	additional, _ = _first_table("additional_skills", u, a)
	for row in additional:
		target = _mapped(b, cw, "skill", row.get("additional_skill"))
		if target and target not in skills:
			skills.append(target)

	languages = []
	language_rows, _ = _first_table("languages", u, a)
	for row in language_rows:
		target = _mapped(b, cw, "language", row.get("language"))
		if target and target not in languages:
			languages.append(target)

	joined_on = normalise.date(employee.get("date_of_joining"))
	contacts, answers = _contacts_and_answers(b, employee, u, a, cw, conf)

	if not geo or not status:
		return

	b["volunteer"] = {
		"source_name": employee["name"],
		"home_geo_node": geo,
		"status": status,
		"joined_on": joined_on,
		"exited_on": normalise.date(employee.get("relieving_date")) if status == "Exited" else None,
		"skills": skills,
		"languages": languages,
	}

	b["application"] = {
		"source_name": a["name"] if a.get("name") and a.get("status") == "Accepted" else employee["name"],
		"source_doctype": "Job Applicant" if a.get("name") and a.get("status") == "Accepted" else "Employee",
		"applied_on": normalise.date(a.get("creation"))
		or joined_on
		or normalise.date(employee.get("creation")),
		"geo_node": geo,
		"skills": skills,
		"languages": languages,
		"emergency_contacts": contacts,
		"custom_answers": answers,
	}


def _contacts_and_answers(b, employee, u, a, cw, conf) -> tuple[list, list]:
	contacts = []
	contact_name = normalise.text(employee.get("person_to_be_contacted"))
	contact_phone = normalise.phone(employee.get("emergency_phone_number"), conf.get("default_country_code"))
	if contact_name and contact_phone:
		contacts.append(
			{
				"contact_name": contact_name,
				"relationship": normalise.text(employee.get("relation")) or "Not recorded",
				"primary_phone": contact_phone,
				"may_contact_in_emergency": 1 if conf.get("emergency_contact_may_call") else 0,
			}
		)
	elif contact_name or contact_phone:
		b["warnings"].append("emergency contact is missing a name or a phone; not carried")
	else:
		b["warnings"].append(
			"no emergency contact on record (the new site requires one for future approvals)"
		)

	answers = []
	for field in QUESTION_FIELDS:
		if field == "allergies":
			rows, _ = _first_table("allergies", u, a)
			value = ", ".join(
				normalise.text(r.get("allergy")) for r in rows if normalise.text(r.get("allergy"))
			)
		else:
			value = normalise.text(_first(u.get(field), a.get(field)))
		if not value:
			continue
		question = _mapped(b, cw, "question", field)
		if question:
			answers.append({"question": question, "answer_value": value})

	return contacts, answers


def _member(b, members, snapshot, cw, conf, today) -> None:
	names = {m["name"] for m in members}
	if len(members) > 1:
		b["warnings"].append("several old member records merged into one: " + ", ".join(sorted(names)))

	joined = []
	for membership in sorted(snapshot["memberships"].values(), key=lambda m: m["name"]):
		if membership.get("member") not in names:
			continue

		status = membership.get("status")

		if status not in MIGRATED_MEMBERSHIP_STATUSES:
			b["errors"].append(f"unsupported membership status: {status}")
			continue

		if not membership.get("company"):
			b["errors"].append(f"membership {membership['name']} has no company (branch)")
			continue

		geo = _mapped(b, cw, "branch", membership["company"], required=True)
		membership_type = _mapped(b, cw, "membership_type", membership.get("membership_type"), required=True)
		if not geo or not membership_type:
			continue

		valid_from = normalise.date(membership.get("from_date"))
		valid_to = normalise.date(membership.get("to_date"))

		invoice = snapshot["invoices"].get(membership["name"]) or {}
		paid = invoice.get("status") == "Paid" or bool(membership.get("paid"))
		new_status, approval_state = membership_state(membership, paid)
		if new_status == "Active" and valid_to and valid_to < today:
			new_status = "Expired"
			b["warnings"].append(
				f"{membership['name']} was still Active on the old site but ended {valid_to}"
			)

		if not valid_from and status in ("Active", "Expired"):
			b["errors"].append(f"membership {membership['name']} has no start date")
			continue

		invoice = snapshot["invoices"].get(membership["name"]) or {}
		paid = invoice.get("status") == "Paid" or bool(membership.get("paid"))
		if new_status == "Active" and not paid:
			b["warnings"].append(f"{membership['name']} is Active but no payment is recorded")

		if status in ("Active", "Expired"):
			joined.extend(d for d in (normalise.date(membership.get("member_since_date")), valid_from) if d)
		b["memberships"].append(
			{
				"source_name": membership["name"],
				"membership_type": membership_type,
				"geo_node": geo,
				"membership_status": new_status,
				"approval_state": approval_state,
				"legacy_status": status,
				"valid_from": valid_from,
				"valid_to": valid_to,
				"paid_on": invoice.get("posting_date") if paid else None,
				"payment_receipt": invoice.get("name") if paid else None,
			}
		)

	# A member is a person even when no historical membership is Active/Expired.
	# Preserve their identity without inventing payment, approval or joining dates.
	b["member"] = {"source_names": sorted(names), "joined_on": min(joined) if joined else None}


def _flag_shared_identifiers(bundles: list[dict]) -> None:
	"""Keep accounts separate and move conflicting claims out of primary IDs."""
	holders = defaultdict(set)
	phones = defaultdict(set)
	for b in bundles:
		for row in (b["profile"] or {}).get("identifications", []):
			if row.get("id_number"):
				holders[(row["id_type"], normalise.key(row["id_number"]))].add(b["key"])
		if (b["profile"] or {}).get("phone"):
			phones[b["profile"]["phone"]].add(b["key"])

	by_key = {b["key"]: b for b in bundles}
	for (id_type, number), keys in holders.items():
		if len(keys) > 1:
			for k in keys:
				bundle=by_key[k]
				bundle.setdefault("conflicting_identifiers",set()).add((id_type,number))
				bundle["warnings"].append("Identification is claimed by multiple source accounts; retained for identity review, with accounts kept separate")
	for bundle in bundles:
		conflicts=bundle.pop("conflicting_identifiers",set())
		if not conflicts:continue
		profile=bundle["profile"]
		original=profile.get("identifications",[])
		retained=[];review=[];indices={}
		for index,row in enumerate(original):
			if (row["id_type"],normalise.key(row["id_number"])) in conflicts:
				review.append(row)
			else:
				indices[index]=len(retained);retained.append(row)
		profile["identifications"]=retained
		profile["legacy_identity_claims"]=json.dumps(review,ensure_ascii=False,sort_keys=True)
		for attachment in bundle["files"]:
			if attachment.get("purpose")=="identification":
				index=attachment["index"]
				if index in indices:attachment["index"]=indices[index]
				else:
					attachment["purpose"]="file"
					attachment.pop("index",None)
	for phone, keys in phones.items():
		if len(keys) > 1:
			for k in keys:
				by_key[k]["warnings"].append(f"phone {phone} is shared with " + ", ".join(sorted(keys - {k})))


def _hash(user, employees, applicants, members, snapshot) -> str:
	names = {m["name"] for m in members}
	# Signing in is not a change to the person.
	user = {k: v for k, v in (user or {}).items() if k != "last_login"}
	payload = {
		"user": user,
		"employees": employees,
		"applicants": applicants,
		"members": members,
		"memberships": [m for m in snapshot["memberships"].values() if m.get("member") in names],
	}
	blob = json.dumps(payload, sort_keys=True, default=str)
	return hashlib.sha256(blob.encode()).hexdigest()
