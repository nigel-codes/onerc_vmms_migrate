"""Entry points. Run each on the TARGET site with `bench execute`.

    bench --site T execute onerc_vmms_migrate.run.check
    bench --site T execute onerc_vmms_migrate.run.selftest                   # loader vs this site, rolled back
    bench --site T execute onerc_vmms_migrate.run.profile
    bench --site T execute onerc_vmms_migrate.run.crosswalks
    bench --site T execute onerc_vmms_migrate.run.plan
    bench --site T execute onerc_vmms_migrate.run.load                       # rehearsal: writes, then rolls back
    bench --site T execute onerc_vmms_migrate.run.load --kwargs "{'confirm': 'LOAD'}"
    bench --site T execute onerc_vmms_migrate.run.reconcile

Each one prints a short summary and writes the full picture under
`sites/T/private/onerc_vmms_migrate/reports/<run id>/`.

`load` rehearses unless it is given `confirm="LOAD"`. A rehearsal writes every
person through vmmsx's real validation, then rolls them back one by one, so it
finds every refusal the real run would meet. It copies no files.
"""

import csv
import datetime
import os
import traceback
from collections import Counter

import frappe
from frappe.utils import now_datetime, today

from onerc_vmms_migrate import load as loader
from onerc_vmms_migrate import transform, workspace
from onerc_vmms_migrate.crosswalk import Crosswalks
from onerc_vmms_migrate.extract import extract
from onerc_vmms_migrate.silence import SilenceBreached, silenced
from onerc_vmms_migrate.source import Source


def _run_id(kind: str) -> str:
	return f"{now_datetime().strftime('%Y%m%d-%H%M%S')}-{kind}"


def _say(*lines):
	for line in lines:
		print(line)


def _write_csv(directory: str, name: str, rows: list[dict]) -> str | None:
	if not rows:
		return None
	path = os.path.join(directory, name)
	columns = list(dict.fromkeys(k for row in rows for k in row))
	with open(path, "w", newline="") as handle:
		os.chmod(path,0o600)
		writer = csv.DictWriter(handle, fieldnames=columns)
		writer.writeheader()
		writer.writerows(rows)
	return path


def _build(source: Source):
	snapshot = extract(source)
	crosswalks = Crosswalks.read()
	result = transform.build(snapshot, crosswalks, workspace.config(), datetime.date.fromisoformat(today()))
	return snapshot, crosswalks, result


# --- check ------------------------------------------------------------------


def check():
	"""Can we read the old site, and is this site ready to receive it?"""
	conf = workspace.config()
	problems, notes = [], []

	with Source(conf) as source:
		notes.append(f"source database: {source.db_name}")
		notes.append(f"source apps: {source.frappe_version()}")
		if hasattr(source, "data"):
			notes.extend(
				f"  {key}: {len(source.data[key])}"
				for key in ("users", "employees", "applicants", "members", "memberships")
			)
			if source.data.get("source", {}).get("logins", {}).get("included"):
				notes.append(f"Protected login capture: {len(source.data.get('password_hashes', {}))} existing password hashes preserved.")
			else:
				notes.append("This capture does not carry password hashes; new users would need password resets.")
		else:
			for doctype in ("User", "Employee", "Job Applicant", "VM Member", "VM Membership"):
				count = (
					source.sql(f"select count(*) as n from `tab{doctype}`")[0]["n"]
					if source.has(doctype)
					else "missing"
				)
				notes.append(f"  {doctype}: {count}")

	notes.append(f"target site: {frappe.local.site}")
	notes.append(f"target apps: {', '.join(frappe.get_installed_apps())}")

	if not conf.get("default_country_code"):
		problems.append("default_country_code is not set; local phone numbers will not be normalised")

	from frappe.utils.scheduler import is_scheduler_disabled

	if not is_scheduler_disabled():
		problems.append(
			"the scheduler is enabled on this site (a real load refuses to run until it is disabled)"
		)
	if not frappe.conf.get("mute_emails"):
		problems.append(
			"mute_emails is not set in site_config (the loader mutes in-process anyway; set it for the cutover)"
		)

	from vmmsx.member.services import society as member_society
	from vmmsx.volunteer.services import society as volunteer_society

	if not volunteer_society.anchor_level_is_configured():
		problems.append("no volunteer anchor level configured (National Society Settings)")
	if not member_society.anchor_level_is_configured():
		problems.append("no membership anchor level configured (National Society Settings)")

	_say(*notes, "", *(f"! {p}" for p in problems) or ["ready"])
	return {"notes": notes, "problems": problems}


# --- profile ----------------------------------------------------------------


def profile():
	"""What is in the old site: counts, gaps and data quality. Writes nothing here."""
	run_id = _run_id("profile")
	out = workspace.report_dir(run_id)

	with Source() as source:
		snapshot = extract(source)
		custom_fields = source.custom_fields()

	employees = [e for e in snapshot["employees"].values() if e.get("is_volunteer")]
	users = snapshot["users"].values()
	emails = Counter()
	no_email = []

	for employee in employees:
		email = transform.employee_email(snapshot, employee)
		if email:
			emails[email] += 1
		else:
			no_email.append(
				{"doctype": "Employee", "name": employee["name"], "who": employee.get("employee_name")}
			)
	for member in snapshot["members"].values():
		if not transform.member_email(snapshot, member):
			no_email.append(
				{"doctype": "VM Member", "name": member["name"], "who": member.get("member_name")}
			)

	summary = {
		"source": snapshot["source"],
		"counts": {
			"users": len(snapshot["users"]),
			"users_by_type": dict(Counter(u.get("user_type") for u in users)),
			"volunteer_employees": len(employees),
			"staff_employees": len(snapshot["employees"]) - len(employees),
			"volunteers_by_status": dict(Counter(e.get("status") for e in employees)),
			"volunteers_by_branch": dict(Counter(e.get("company") for e in employees)),
			"job_applicants_by_status": dict(
				Counter(a.get("status") for a in snapshot["applicants"].values())
			),
			"members": len(snapshot["members"]),
			"memberships_by_status": dict(Counter(m.get("status") for m in snapshot["memberships"].values())),
			"memberships_by_type": dict(
				Counter(m.get("membership_type") for m in snapshot["memberships"].values())
			),
			"memberships_by_branch": dict(
				Counter(m.get("company") for m in snapshot["memberships"].values())
			),
			"password_hashes": len(snapshot["password_hashes"]),
			"files_attached_to_people": len(snapshot["files"]),
		},
		"quality": {
			"volunteers_sharing_an_email": {k: v for k, v in emails.items() if v > 1},
			"people_without_email": len(no_email),
			"volunteers_with_emergency_contact": sum(
				1 for e in employees if e.get("person_to_be_contacted") and e.get("emergency_phone_number")
			),
			"volunteers_without_date_of_joining": sum(1 for e in employees if not e.get("date_of_joining")),
			"memberships_active_but_ended": sum(
				1
				for m in snapshot["memberships"].values()
				if m.get("status") == "Active" and m.get("to_date") and str(m["to_date"]) < today()
			),
		},
		"missing_columns": snapshot["missing_columns"],
	}

	workspace.write_json(os.path.join(out, "profile.json"), summary)
	_write_csv(out, "custom_fields.csv", custom_fields)
	_write_csv(out, "people_without_email.csv", no_email)

	_say(
		f"report: {out}",
		*(f"  {k}: {v}" for k, v in summary["counts"].items() if not isinstance(v, dict)),
		f"  volunteers by status: {summary['counts']['volunteers_by_status']}",
		f"  memberships by status: {summary['counts']['memberships_by_status']}",
		*(f"  {k}: {v}" for k, v in summary["quality"].items() if not isinstance(v, dict)),
		f"  missing columns: {summary['missing_columns'] or 'none'}",
	)
	return summary


# --- crosswalks -------------------------------------------------------------


def crosswalks():
	"""Write or refresh the crosswalk CSVs from what the old site actually uses."""
	with Source() as source:
		_snapshot, cw, _result = _build(source)

	written = cw.write()
	problems = cw.problems()
	_say(
		f"crosswalks: {workspace.crosswalk_dir()}",
		*(f"  {os.path.basename(p)}" for p in written),
		f"{len(problems)} value(s) still need a target",
	)
	return {"written": written, "problems": len(problems)}


# --- plan -------------------------------------------------------------------


def plan():
	"""Transform only. Who would load, who would not and why. No writes here."""
	run_id = _run_id("plan")
	with Source() as source:
		_snapshot, cw, result = _build(source)
	return _report_plan(run_id, cw, result)


def _rejection_category(error):
	# Console summaries contain categories, never personal ID numbers or emails.
	if error.startswith("ID number"):
		return "ID number lacks a recorded type"
	if " is also held by " in error:
		return "Identification shared across accounts"
	if error.startswith("more than one volunteer Employee"):
		return "Multiple volunteer records share an account"
	if error.startswith("unmapped "):
		return error.split(":",1)[0]
	return "Missing or conflicting source information"


def _report_plan(run_id, cw, result) -> dict:
	out = workspace.report_dir(run_id)
	bundles = result["bundles"]
	ready = [b for b in bundles if not b["errors"]]
	rejects = [{"person": b["key"], "error": e} for b in bundles for e in b["errors"]]
	warnings = [{"person": b["key"], "warning": w} for b in bundles for w in b["warnings"]]
	skipped_memberships = [
		{"person": b["key"], **m} for b in bundles for m in b.get("skipped_memberships", [])
	]

	_write_csv(out, "rejects.csv", rejects)
	_write_csv(out, "warnings.csv", warnings)
	_write_csv(out, "not_migrated.csv", result["not_migrated"])
	_write_csv(out, "memberships_not_migrated.csv", skipped_memberships)
	_write_csv(out, "crosswalk_problems.csv", cw.problems())

	summary = {
		"people": len(bundles),
		"ready": len(ready),
		"rejected": len(bundles) - len(ready),
		"volunteers_ready": sum(1 for b in ready if b["volunteer"]),
		"members_ready": sum(1 for b in ready if b["member"]),
		"memberships_ready": sum(len(b["memberships"]) for b in ready),
		"not_migrated_records": len(result["not_migrated"]),
		"memberships_not_migrated": dict(Counter(m["status"] for m in skipped_memberships)),
		"reject_reasons": dict(Counter(_rejection_category(e["error"]) for e in rejects).most_common(15)),
		"placeholders_treated_as_blank": dict(cw.placeholder_hits),
	}
	workspace.write_json(os.path.join(out, "plan.json"), summary)
	_say(f"report: {out}", *(f"  {k}: {v}" for k, v in summary.items()))
	return summary


# --- load -------------------------------------------------------------------


def load(confirm: str | None = None, limit: int | None = None, only: str | None = None):
	"""Load every ready person. Rehearses unless confirm == "LOAD"."""
	dry_run = confirm != "LOAD"
	run_id = _run_id("rehearsal" if dry_run else "load")
	conf = workspace.config()

	if not dry_run:
		from frappe.utils.scheduler import is_scheduler_disabled

		if not is_scheduler_disabled():
			frappe.throw(
				"Disable the scheduler on this site before a real load (bench --site T disable-scheduler)."
			)

	frappe.set_user("Administrator")
	frappe.db.commit()  # nothing of ours is pending; start each person on a clean transaction

	with Source(conf) as source:
		_snapshot, cw, result = _build(source)
		if _snapshot.get("capture_complete") is False:
			frappe.throw("This bulk capture has missing source files. Review missing-files.json and capture a corrected source snapshot before rehearsing or importing.")
		plan_summary = _report_plan(run_id, cw, result)
		out = workspace.report_dir(run_id)

		bundles = [b for b in result["bundles"] if not b["errors"]]
		if only:
			bundles = [b for b in bundles if b["key"] == only.strip().lower()]
		if limit:
			bundles = bundles[: int(limit)]

		loaded, failed, skipped = [], [], []

		with silenced():
			for bundle in bundles:
				done = loader.already_migrated(bundle["key"])
				if done:
					changed = done.source_hash != bundle["source_hash"]
					skipped.append(
						{
							"person": bundle["key"],
							"migrated_in": done.run_id,
							"changed_since": "yes" if changed else "",
						}
					)
					continue

				try:
					written = loader.load_bundle(bundle, source, conf, run_id, dry_run=dry_run)
				except SilenceBreached as exc:
					frappe.db.rollback()
					failed.append({"person": bundle["key"], "error": f"STOPPED: {exc}"})
					_say(f"STOPPED on {bundle['key']}: {exc}")
					break
				except Exception as exc:
					frappe.db.rollback()
					frappe.clear_messages()
					failed.append(
						{
							"person": bundle["key"],
							"error": f"{type(exc).__name__}: {frappe.utils.strip_html(str(exc))}",
							"where": traceback.format_exc(limit=-3).strip().splitlines()[-3]
							if exc.__traceback__
							else "",
						}
					)
					continue

				if dry_run:
					frappe.db.rollback()
				else:
					frappe.db.commit()

				loaded.append(
					{
						"person": bundle["key"],
						"profile": written["profile"],
						"volunteer": written["volunteer"] or "",
						"member": written["member"] or "",
						"memberships": len(bundle["memberships"]),
						"warnings": " | ".join(written["warnings"]),
					}
				)

	_write_csv(out, "loaded.csv", loaded)
	_write_csv(out, "load_failures.csv", failed)
	_write_csv(out, "already_migrated.csv", skipped)

	summary = {
		"mode": "REHEARSAL (rolled back)" if dry_run else "LOAD (committed)",
		"attempted": len(bundles),
		"loaded": len(loaded),
		"failed": len(failed),
		"already_migrated": len(skipped),
		"changed_since_migrated": sum(1 for s in skipped if s["changed_since"]),
		"rejected_before_load": plan_summary["rejected"],
	}
	workspace.write_json(os.path.join(out, "load.json"), summary)
	_say("", *(f"  {k}: {v}" for k, v in summary.items()), f"report: {out}")
	return summary


# --- reconcile ----------------------------------------------------------------


def reconcile(bundles=None):
	"""Compare every migrated person with what the old site says today."""
	run_id = _run_id("reconcile")
	out = workspace.report_dir(run_id)

	if bundles is None:
		with Source() as source:
			_snapshot, _cw, result = _build(source)
		bundles = result["bundles"]

	mismatches, checked = [], 0
	for b in bundles:
		rows = frappe.get_all(
			loader.LEDGER,
			filters={"person_key": b["key"]},
			fields=["target_doctype", "target_name", "source_name", "outcome"],
		)
		if not rows:
			continue
		checked += 1
		by_target = {(r.target_doctype, r.source_name): r for r in rows}
		profile_row = next((r for r in rows if r.target_doctype == "Red Profile"), None)

		def compare(what, expected, actual):
			if str(expected or "") != str(actual or ""):
				mismatches.append({"person": b["key"], "field": what, "expected": expected, "actual": actual})

		if profile_row and profile_row.outcome == "created" and b["profile"]:
			actual = frappe.db.get_value(
				"Red Profile",
				profile_row.target_name,
				["email", "first_name", "last_name", "date_of_birth", "gender", "home_geo_node"],
				as_dict=True,
			)
			for field in ("email", "first_name", "last_name", "date_of_birth", "gender", "home_geo_node"):
				compare(
					f"Red Profile.{field}", b["profile"].get(field), actual.get(field) if actual else None
				)

		if b["volunteer"]:
			row = by_target.get(("VMMS Volunteer", b["volunteer"]["source_name"]))
			if not row:
				mismatches.append(
					{
						"person": b["key"],
						"field": "VMMS Volunteer",
						"expected": "present",
						"actual": "missing",
					}
				)
			elif row.outcome == "created":
				actual = frappe.db.get_value(
					"VMMS Volunteer", row.target_name, ["status", "home_geo_node", "joined_on"], as_dict=True
				)
				compare("Volunteer.status", b["volunteer"]["status"], actual.status)
				compare("Volunteer.home_geo_node", b["volunteer"]["home_geo_node"], actual.home_geo_node)
				compare("Volunteer.joined_on", b["volunteer"]["joined_on"], actual.joined_on)

		for m in b["memberships"]:
			row = by_target.get(("VMMS Membership", m["source_name"]))
			if not row:
				mismatches.append(
					{
						"person": b["key"],
						"field": f"Membership {m['source_name']}",
						"expected": "present",
						"actual": "missing",
					}
				)
				continue
			actual = frappe.db.get_value(
				"VMMS Membership",
				row.target_name,
				["membership_status", "membership_type", "geo_node", "valid_from", "valid_to"],
				as_dict=True,
			)
			lifetime = frappe.db.get_value("VMMS Membership Type", m["membership_type"], "is_lifetime")
			compare(f"{m['source_name']}.status", m["membership_status"], actual.membership_status)
			compare(f"{m['source_name']}.type", m["membership_type"], actual.membership_type)
			compare(f"{m['source_name']}.geo_node", m["geo_node"], actual.geo_node)
			compare(f"{m['source_name']}.valid_from", m["valid_from"], actual.valid_from)
			compare(f"{m['source_name']}.valid_to", None if lifetime else m["valid_to"], actual.valid_to)

	counts = {
		"people_checked": checked,
		"mismatches": len(mismatches),
		"target_volunteers_by_status": dict(
			frappe.db.sql("select status, count(*) from `tabVMMS Volunteer` group by status")
		),
		"target_memberships_by_status": dict(
			frappe.db.sql(
				"select membership_status, count(*) from `tabVMMS Membership` group by membership_status"
			)
		),
		"email_queue_rows": frappe.db.count("Email Queue"),
	}
	_write_csv(out, "mismatches.csv", mismatches)
	workspace.write_json(os.path.join(out, "reconcile.json"), counts)
	_say(f"report: {out}", *(f"  {k}: {v}" for k, v in counts.items()))
	return counts


# --- selftest -------------------------------------------------------------------


def selftest():
	"""Load three synthetic people through the real loader on this site, check them, roll back."""
	from onerc_vmms_migrate import selftest as _selftest

	return _selftest.run()
