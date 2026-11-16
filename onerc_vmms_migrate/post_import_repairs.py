"""Repairs to people records the import has already written.

Agreed with KRCS on 15 November 2026, after the production people import:

1. System Manager comes off the Navari accounts and the placeholder account;
   the accounts stay enabled.
2. Volunteers and members lose the six old roles that gave them the desk, and
   Raven User stops granting the desk, so they keep Raven, LMS and Helpdesk.
3. County Coordinators get the branch they had on the old site; the national
   companies become the national level.
4. Applications the old site had on Hold or had replied to go back into their
   county's first review step.
5. Approved volunteer applications that never produced a volunteer do now;
   job and opportunity applications are left alone.
6. ID numbers imported without a type are filed as National ID when they are
   7 or 8 digits; the rest are reported.
7. Active life memberships carry no end date.

Added on 16 November 2026, for the history import and the end of the cutover:

8. An old event booking whose account was not there when the history came
   across is linked to that account once it exists, so the person sees it.
9. Imported applications and memberships still waiting are put in the queue
   of whoever reviews them now, without the assignment email the first
   scheduled run after the cutover would otherwise send.
10. The comments reviewers left on old applications and memberships ("Kindly
   attach your documents") come across as the same comments on the records
   they became, with their author and date. Desk shows them on the record.
11. Mail queued while the cutover held email is set aside (status Error, with
   the reason), so Finish cutover does not send it. Retry Sending in the Email
   Queue list sends any one of them deliberately.

Each repair finds what still needs repairing, so it is safe to run again: after
a later people capture it repairs only the newcomers, and a stopped run is
simply started again. Preview writes a report and changes nothing. Apply runs
the repairs the operator ticked, in this order, sends nobody email, SMS or an
alert, and commits every PAGE records so a stop keeps what was done.
"""

import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from pathlib import Path

import frappe
from frappe.utils import getdate, now_datetime, today

from onerc_vmms_migrate import silence, workspace

SETTINGS = "VMMS Migration Settings"
CONFIRMATION = "REPAIR"
PAGE = 50

REPAIRS = {
	"system_manager": "Remove System Manager from the Navari and placeholder accounts",
	"desk_access": "Take the desk away from volunteers and members",
	"coordinator_areas": "Give County Coordinators their old branch",
	"waiting_applications": "Put Hold and Replied applications back in review",
	"approved_without_volunteer": "Make volunteers from approved applications that have none",
	"identity_numbers": "File untyped 7–8 digit ID numbers as National ID",
	"life_memberships": "Clear the end date on active life memberships",
	"event_bookings": "Link old event bookings to accounts that now exist",
	"approval_routing": "Queue imported waiting records with their current reviewers, without emailing them",
	"reviewer_notes": "Copy the comments reviewers left on old applications and memberships",
	"held_mail": "Set aside mail queued during the cutover so it is not sent",
}
ORDER = tuple(REPAIRS)

SYSTEM_ACCOUNTS = ("Administrator", "Guest")
ADMIN_ROLE = "System Manager"
ADMIN_REMOVED_DOMAINS = ("@navari.co.ke",)
ADMIN_REMOVED_USERS = ("missingemail19@vmms.com",)
OLD_DESK_ROLES = ("Inbox User", "Analytics", "Dashboard Manager", "VM Member", "Gameplan Member", "Gameplan Guest")
RAVEN_ROLE = "Raven User"
COORDINATOR = "County Coordinator"
NATIONAL_COMPANIES = ("Kenya Red Cross Society", "KRCS - HQ")
APPLICATION = "VMMS Volunteer Application"
NATIONAL_ID = "national_id"
NATIONAL_ID_PATTERN = "7-8 digits (National ID)"
# Messages leave a row in these on their way out. ToDo is not here: putting an
# application in a coordinator's queue is an assignment, and that is the point.
QUIET = ("Email Queue", "Notification Log", "SMS Log", "Communication")


# --- the two actions ------------------------------------------------------


def preview(progress=None) -> dict:
	"""Work out every repair and write the report. Changes nothing."""
	_assert_people_finished()
	report = {"generated_at": str(now_datetime()), "scheduler_enabled": _scheduler_on(), "repairs": {}}
	summary = {}
	for key in ORDER:
		_report(progress, key, 0, 1)
		plan = PLANS[key]()
		report["repairs"][key] = {"label": REPAIRS[key], "summary": plan["summary"], "details": plan["details"]}
		summary[key] = plan["summary"]
	directory = workspace.report_dir(_run_id("repairs-preview"))
	workspace.write_json(os.path.join(directory, "repairs-preview.json"), report)
	return {
		"report": os.path.basename(directory) + "/repairs-preview.json",
		"scheduler_enabled": report["scheduler_enabled"],
		"repairs": summary,
	}


def apply(progress=None) -> dict:
	"""Run the repairs chosen in the Apply dialog, in the agreed order."""
	from onerc_vmms_migrate.browser import state

	chosen = [key for key in ORDER if key in set(state().get("repairs_selected") or [])]
	if not chosen:
		frappe.throw("Choose at least one repair.")
	_assert_people_finished()
	directory = workspace.report_dir(_run_id("repairs-apply"))
	results = {}
	with silence.silenced(trusted=False):
		for key in chosen:
			_report(progress, key, 0, None)
			plan = PLANS[key]()
			_report(progress, key, 0, len(plan["todo"]))
			quiet = Quiet()
			results[key] = {"planned": plan["summary"], "done": APPLY[key](plan, _phase(progress, key), quiet)}
			quiet.check()
			frappe.db.commit()
			workspace.write_json(os.path.join(directory, "repairs-applied.json"), results)
	return {"report": os.path.basename(directory) + "/repairs-applied.json", "repairs": {k: v["done"] for k, v in results.items()}}


def validate_request(repairs, confirmation, backup_confirmed) -> list[str]:
	"""What the Apply dialog asked for, refused unless it was deliberate."""
	if confirmation != CONFIRMATION:
		frappe.throw(f"Type {CONFIRMATION} to apply the repairs.")
	if not backup_confirmed:
		frappe.throw("Confirm you have a current database backup.")
	repairs = list(repairs or [])
	unknown = sorted(set(repairs) - set(ORDER))
	if unknown:
		frappe.throw(f"Unknown repair: {', '.join(unknown)}")
	chosen = [key for key in ORDER if key in repairs]
	if not chosen:
		frappe.throw("Choose at least one repair.")
	_assert_people_finished()
	if not _previewed():
		frappe.throw("Preview the repairs first and read repairs-preview.json.")
	return chosen


# --- 1. System Manager ----------------------------------------------------


def plan_system_manager() -> dict:
	holders = _column("select parent from `tabHas Role` where parenttype = 'User' and role = %s", (ADMIN_ROLE,))
	losing = sorted(user for user in holders if _loses_admin(user))
	from_profile = _profile_holders(losing, (ADMIN_ROLE,))
	return {
		"summary": {
			"accounts": len(losing),
			"keep_system_manager": len(holders) - len(losing),
			"get_it_back_from_a_role_profile": len(from_profile),
		},
		"details": {"accounts": losing, "role_profile_gives_it_back": sorted(from_profile)},
		"todo": losing,
	}


def apply_system_manager(plan, progress, quiet) -> dict:
	for user in plan["todo"]:
		frappe.get_doc("User", user).remove_roles(ADMIN_ROLE)
	return {"removed_from": len(plan["todo"])}


def _loses_admin(user: str) -> bool:
	user = user.lower()
	return user.endswith(ADMIN_REMOVED_DOMAINS) or user in ADMIN_REMOVED_USERS


# --- 2. desk access -------------------------------------------------------


def plan_desk_access() -> dict:
	"""Who loses the six old roles, and who stays a desk user and why.

	Staff are left alone: anybody who reaches the desk through a role other than
	the six and Raven User keeps every role they hold. A role profile that grants
	one of the six would give it back on the account's next save, so those
	accounts are reported and not touched.
	"""
	passive = (*OLD_DESK_ROLES, RAVEN_ROLE)
	holders = set(_column("select distinct parent from `tabHas Role` where parenttype = 'User' and role in %s", (OLD_DESK_ROLES,)))
	staff = set(_column("""select distinct h.parent from `tabHas Role` h join tabRole r on r.name = h.role
		where h.parenttype = 'User' and r.desk_access = 1 and h.role not in %s""", (passive,)))
	candidates = sorted(holders - staff - set(SYSTEM_ACCOUNTS))
	profiled = _profile_holders(candidates, OLD_DESK_ROLES)
	scope = [user for user in candidates if user not in profiled]

	kept = sorted(holders & staff)
	kept_people = _people(kept)
	kept_reasons = []
	if kept_people["volunteer_or_member"]:
		kept_reasons = [
			{"role": role, "accounts": count}
			for role, count in frappe.db.sql("""select h.role, count(distinct h.parent) from `tabHas Role` h
				join tabRole r on r.name = h.role where h.parenttype = 'User' and r.desk_access = 1
				and h.role not in %s and h.parent in %s group by 1 order by 2 desc""",
				(passive, tuple(kept_people["volunteer_or_member"])))
		]

	portal = _column("""select u.name from tabUser u where u.user_type = 'System User' and u.name not in %s
		and not exists(select 1 from `tabHas Role` h join tabRole r on r.name = h.role
			where h.parent = u.name and h.parenttype = 'User' and r.desk_access = 1
			and h.role != %s and not (h.role in %s and u.name in %s))""",
		(SYSTEM_ACCOUNTS, RAVEN_ROLE, OLD_DESK_ROLES, tuple(scope) or ("",)))
	raven = frappe.db.get_value("Role", RAVEN_ROLE, "desk_access")
	return {
		"summary": {
			"lose_the_old_roles": _people(scope)["counts"],
			"left_alone_as_staff": len(kept),
			"skipped_role_profile": len(profiled),
			"volunteers_or_members_who_stay_desk_users": len(kept_people["volunteer_or_member"]),
			"become_portal_users": _people(portal)["counts"],
			"raven_user_desk_access": "missing" if raven is None else raven,
			"desk_users_now": frappe.db.count("User", {"user_type": "System User"}),
			"desk_users_after": frappe.db.count("User", {"user_type": "System User"}) - len(portal),
		},
		"details": {
			"stay_desk_users_because_of": kept_reasons,
			"stay_desk_users": kept_people["volunteer_or_member"],
			"skipped_role_profile": sorted(profiled),
			"apps": {app: app in frappe.get_installed_apps() for app in ("raven", "lms", "helpdesk")},
		},
		"todo": scope,
	}


def apply_desk_access(plan, progress, quiet) -> dict:
	scope = plan["todo"]
	for done, part in enumerate(_chunks(scope), start=1):
		frappe.db.sql("delete from `tabHas Role` where parenttype = 'User' and parent in %s and role in %s", (part, OLD_DESK_ROLES))
		progress(min(done * 500, len(scope)), len(scope))
	if frappe.db.exists("Role", RAVEN_ROLE):
		# A plain write, not Role.save(): the save re-saves every holder's User, one
		# by one, and the account types are settled below in one statement instead.
		frappe.db.set_value("Role", RAVEN_ROLE, "desk_access", 0)
	portal = _column("""select u.name from tabUser u where u.user_type = 'System User' and u.name not in %s
		and not exists(select 1 from `tabHas Role` h join tabRole r on r.name = h.role
			where h.parent = u.name and h.parenttype = 'User' and r.desk_access = 1)""", (SYSTEM_ACCOUNTS,))
	for part in _chunks(portal):
		frappe.db.sql("update tabUser set user_type = 'Website User' where name in %s", (part,))
	frappe.clear_cache()
	return {"old_roles_removed_from": len(scope), "raven_user_desk_access": 0, "became_portal_users": len(portal)}


# --- 3. coordinator areas -------------------------------------------------


def plan_coordinator_areas() -> dict:
	from onerc_vmms_migrate.crosswalk import DROP, UNMAPPED, Crosswalks

	old = old_coordinator_companies()
	if old is None:
		return {"summary": {"error": "The selected capture has no role or permission history."}, "details": {}, "todo": []}
	roots = frappe.get_all("Geo Node", filters={"parent_geo_node": ["is", "not set"]}, pluck="name")
	if len(roots) != 1:
		frappe.throw(f"Expected one national Geo Node at the top of the tree, found {len(roots)}.")
	national = roots[0]
	mapping = Crosswalks.read()
	holders = set(_column("select distinct parent from `tabHas Role` where parenttype = 'User' and role = %s", (COORDINATOR,)))
	enabled = set(_column("select name from tabUser where enabled = 1"))
	places = {name: (place, level) for name, place, level in frappe.db.sql("select name, geo_node_name, geo_level from `tabGeo Node`")}

	todo, rows, no_branch, disabled, problems = [], [], [], [], []
	for user, companies in sorted(old.items()):
		if not frappe.db.exists("User", user):
			problems.append({"user": user, "reason": "no account on this site"})
		elif user not in holders:
			problems.append({"user": user, "reason": "does not hold County Coordinator here"})
		elif user not in enabled:
			disabled.append(user)
		elif not companies:
			no_branch.append(user)
		else:
			seen = set()
			for company in companies:
				node = national if company in NATIONAL_COMPANIES else mapping.lookup("branch", company)
				if not node or node in (UNMAPPED, DROP) or node not in places:
					problems.append({"user": user, "reason": f"old branch {company!r} has no place here"})
					continue
				if node in seen:
					continue
				seen.add(node)
				exists = bool(frappe.db.exists("Geo Assignment", {"user": user, "role": COORDINATOR, "geo_node": node, "is_active": 1}))
				rows.append({"user": user, "old_branch": company, "place": places[node][0], "level": places[node][1], "already_there": exists})
				if not exists:
					todo.append((user, node))
	return {
		"summary": {
			"coordinators_on_old_site": len(old),
			"areas_to_add": len(todo),
			"already_there": sum(row["already_there"] for row in rows),
			"no_branch_on_old_site": len(no_branch),
			"disabled_here": len(disabled),
			"need_a_look": len(problems),
			"coordinators_here_not_on_old_site": len(holders - set(old)),
		},
		"details": {
			"areas": rows,
			"no_branch_on_old_site": no_branch,
			"disabled_here": disabled,
			"need_a_look": problems,
			"coordinators_here_not_on_old_site": sorted(holders - set(old)),
		},
		"todo": todo,
	}


def apply_coordinator_areas(plan, progress, quiet) -> dict:
	for done, (user, node) in enumerate(plan["todo"], start=1):
		frappe.get_doc({
			"doctype": "Geo Assignment",
			"user": user,
			"role": COORDINATOR,
			"geo_node": node,
			"is_active": 1,
			"valid_from": today(),
		}).insert(ignore_permissions=True)
		progress(done, len(plan["todo"]))
	return {"areas_added": len(plan["todo"])}


def old_coordinator_companies() -> dict | None:
	"""Each old County Coordinator, with the companies their permissions named.

	Read from the selected capture's own copy of the old Has Role and User
	Permission rows, so nothing about the old site is written into this code.
	"""
	with _capture() as rows:
		if rows is None:
			return None
		coordinators = {
			row["parent"] for row in rows("Has Role")
			if row.get("role") == COORDINATOR and row.get("parenttype") == "User"
		}
		companies = defaultdict(set)
		for row in rows("User Permission"):
			if row.get("user") in coordinators and row.get("allow") == "Company":
				companies[row["user"]].add(row["for_value"])
	return {user: sorted(companies.get(user, ())) for user in sorted(coordinators)}


# --- 4. applications waiting with no review step ---------------------------


def plan_waiting_applications() -> dict:
	from vmmsx.approvals.services import config, routing

	workflow = config.for_doctype(APPLICATION)
	waiting = _waiting()
	places = dict(frappe.db.sql("select name, geo_node_name from `tabGeo Node`"))
	routed, escalated = {}, {}

	def where(doc):
		# The questions the engine's `_advance` asks, remembered per place.
		for stage in config.stages_after(workflow, None):
			node = config.stage_anchor(doc, workflow, stage)
			if (stage.name, node) not in routed:
				routed[(stage.name, node)] = routing.routed(stage, node)
			if routed[(stage.name, node)]:
				return stage.stage_label, routed[(stage.name, node)], "routed"
			if stage.is_optional:
				continue
			if (stage.name, node) not in escalated:
				escalated[(stage.name, node)] = routing.escalate(stage.required_role, node, exclude=set())["approvers"]
			people = escalated[(stage.name, node)]
			return stage.stage_label, people, "escalated" if people else "nobody yet"
		return None, [], "would be approved"

	todo, held = [], []
	outcomes = Counter()
	by_place = defaultdict(lambda: {"applications": 0, "approvers": set(), "outcomes": Counter()})
	for row in waiting:
		try:
			doc = frappe.get_doc(APPLICATION, row.name)
			missing = unsaveable(doc)
			if missing:
				stage, people, outcome = None, [], f"would not save: missing {', '.join(missing)}"
			else:
				stage, people, outcome = where(doc)
		except Exception as error:
			stage, people, outcome = None, [], f"cannot route: {str(error)[:100]}"
		outcomes[outcome.split(":")[0]] += 1
		entry = by_place[places.get(row.geo_node, row.geo_node)]
		entry["applications"] += 1
		entry["approvers"].update(people)
		entry["outcomes"][outcome] += 1
		if outcome in ("routed", "escalated", "nobody yet"):
			todo.append(row.name)
		else:
			held.append({"application": row.name, "why": outcome})
	stages = [(s.stage_label, s.sla_days, s.on_sla_breach) for s in config.stages_after(workflow, None)]
	return {
		"summary": {"waiting": len(waiting), "to_route": len(todo), "held": len(held), **dict(outcomes)},
		"details": {
			"by_place": [
				{"place": place, "applications": entry["applications"], "outcomes": dict(entry["outcomes"]), "approvers": sorted(entry["approvers"])}
				for place, entry in sorted(by_place.items(), key=lambda item: -item[1]["applications"])
			],
			"held": held,
			"stages_and_deadlines": [{"stage": label, "sla_days": days, "on_breach": breach} for label, days, breach in stages],
		},
		"todo": todo,
	}


def apply_waiting_applications(plan, progress, quiet) -> dict:
	from frappe.desk.form import assign_to
	from vmmsx.approvals.services import config, engine

	workflow = config.for_doctype(APPLICATION)
	routed, failed = 0, []
	# Frappe tells an assignee by bell and email; these coordinators are told by
	# their queue, so the notice is off for this process while the run lasts.
	with silence.replacing(assign_to, "notify_assignment", lambda *args, **kwargs: None):
		for done, name in enumerate(plan["todo"], start=1):
			frappe.db.savepoint("repair_route")
			try:
				doc = frappe.get_doc(APPLICATION, name)
				# Somebody may have acted on it since the plan was made.
				if doc.approval_state == "In Review" and not doc.approval_stage:
					engine._commit(doc, workflow, engine._advance(doc, workflow, after=None), frappe.session.user)
					routed += 1
			except Exception as error:
				frappe.db.rollback(save_point="repair_route")
				failed.append({"application": name, "why": str(error)[:200]})
			if done % PAGE == 0:
				_commit_page(quiet)
				progress(done, len(plan["todo"]))
	return {"routed": routed, "failed": failed}


def unsaveable(doc) -> list[str]:
	"""Required fields the save that routes this application would find empty.

	An old decision the import carried over has no approver, stage or date. Its
	provenance excuses that (import_review.ImportedDecisionMixin) only when the
	row is marked as imported and the application has a migration record; a
	row that lost either would stop the save, so it is held and reported here.
	"""
	missing = [field for field, _ in doc._get_missing_mandatory_fields()]
	for child in doc.get_all_children():
		missing += [f"{child.doctype} row {child.idx}: {field}" for field, _ in child._get_missing_mandatory_fields()]
	return missing


def _waiting():
	return frappe.get_all(
		APPLICATION,
		filters=[[APPLICATION, "approval_state", "=", "In Review"], [APPLICATION, "approval_stage", "is", "not set"]],
		fields=["name", "geo_node"],
		order_by="creation asc",
	)


# --- 5. approved applications without a volunteer -------------------------


def plan_approved_without_volunteer() -> dict:
	"""Sort each approved application with no volunteer by what the old site says.

	The old Job Applicant is the one the import archived for this application.
	A job or opportunity application is left alone even when its person is a
	volunteer. The new volunteer joins on the day the old application was last
	changed, which is when it was accepted: the old site kept no other date.
	"""
	rows = frappe.get_all(
		APPLICATION,
		filters=[[APPLICATION, "approval_state", "=", "Approved"], [APPLICATION, "volunteer", "is", "not set"]],
		fields=["name", "red_profile", "geo_node"],
		order_by="creation asc",
	)
	todo, groups, left = [], Counter(), []
	for row in rows:
		old = old_application(row.name)
		volunteer = frappe.db.get_value("VMMS Volunteer", {"red_profile": row.red_profile}) if row.red_profile else None
		if not row.red_profile:
			group = "left alone: no profile"
		elif old is None:
			group = "left alone: old record not found"
		elif not int(old.get("is_volunteer") or 0):
			group = "left alone: job or opportunity application" + (" (person is a volunteer)" if volunteer else "")
		elif volunteer:
			group = "link to their volunteer"
			todo.append({"application": row.name, "volunteer": volunteer})
		else:
			group = "make a volunteer"
			todo.append({"application": row.name, "red_profile": row.red_profile, "geo_node": row.geo_node,
				"joined_on": str(getdate(old.get("modified") or old.get("creation")))})
		groups[group] += 1
		if group.startswith("left alone"):
			left.append({"application": row.name, "why": group})
	joined = sorted(item["joined_on"] for item in todo if "joined_on" in item)
	from vmmsx.volunteer.services import society

	role = society.volunteer_member_role()
	return {
		"summary": {
			"approved_without_volunteer": len(rows),
			**dict(groups),
			"joining_dates": f"{joined[0]} to {joined[-1]}" if joined else None,
			"new_volunteers_get_role": role,
			"that_role_opens_the_desk": bool(frappe.db.get_value("Role", role, "desk_access")) if role else None,
			"new_volunteers_get_an_hr_record": society.provisions_employees(),
		},
		"details": {
			"joining_dates_by_year": dict(sorted(Counter(day[:4] for day in joined).items())),
			"left_alone": left,
		},
		"todo": todo,
	}


def apply_approved_without_volunteer(plan, progress, quiet) -> dict:
	from vmmsx.volunteer.services import application as application_service
	from vmmsx.volunteer.services import volunteer as volunteer_service

	linked, made, failed = 0, 0, []
	for done, item in enumerate(plan["todo"], start=1):
		frappe.db.savepoint("repair_volunteer")
		try:
			# A person with two such applications: the first makes the volunteer,
			# the later one is linked to it.
			existing = item.get("volunteer") or frappe.db.get_value("VMMS Volunteer", {"red_profile": item.get("red_profile")})
			if frappe.db.get_value(APPLICATION, item["application"], "volunteer"):
				pass  # linked since the plan was made
			elif existing:
				frappe.db.set_value(APPLICATION, item["application"], "volunteer", existing, update_modified=False)
				linked += 1
			else:
				# The volunteer exists, with its joining date, before acceptance runs:
				# the HR record and the affiliation take their start date from it.
				volunteer = volunteer_service.ensure(item["red_profile"], home_geo_node=item["geo_node"])
				volunteer.db_set("joined_on", item["joined_on"], update_modified=False)
				application_service.accept(frappe.get_doc(APPLICATION, item["application"]))
				made += 1
		except Exception as error:
			frappe.db.rollback(save_point="repair_volunteer")
			failed.append({"application": item["application"], "why": str(error)[:200]})
		if done % PAGE == 0:
			_commit_page(quiet)
			progress(done, len(plan["todo"]))
	return {"linked_to_their_volunteer": linked, "volunteers_made": made, "failed": failed}


def old_application(name: str) -> dict | None:
	"""The old Job Applicant this application came from, as the import archived it."""
	source = frappe.db.get_value("Legacy Migration Record",
		{"source_doctype": "Job Applicant", "target_doctype": APPLICATION, "target_name": name}, "source_name")
	if not source:
		return None
	payload = frappe.db.get_value("Legacy VMMS Document", {"source_doctype": "Job Applicant", "source_name": source},
		"source_payload", order_by="creation desc")
	if not payload:
		return None
	data = json.loads(payload)
	return data.get("record", data) if isinstance(data, dict) else None


# --- 6. identity numbers --------------------------------------------------


def id_pattern(number: str) -> str:
	if re.fullmatch(r"\d{7,8}", number):
		return NATIONAL_ID_PATTERN
	if re.fullmatch(r"\d+", number):
		return f"{len(number)} digits" if len(number) < 7 else "9+ digits"
	if re.fullmatch(r"\d{7,8}\.0", number):
		return "7-8 digits ending in .0 (spreadsheet)"
	compact = re.sub(r"[\s\-./]", "", number)
	if re.fullmatch(r"\d{7,8}", compact):
		return "7-8 digits with spaces or dashes"
	if re.fullmatch(r"[A-Za-z]{1,2}\d{6,9}", compact):
		return "letters then digits (passport-like)"
	if re.fullmatch(r"[A-Za-z0-9]+", compact):
		return "other letters and digits"
	return "other"


def plan_identity_numbers() -> dict:
	"""National ID rows for the 7-8 digit numbers, and a count of everything else.

	Skipped and listed: a number already on another profile, a number two of
	these profiles share, and a profile that already holds it. The imported
	value itself stays where it is. A row is primary only for a profile that has
	no primary document yet.
	"""
	if not frappe.db.exists("Identification Type", NATIONAL_ID):
		return {"summary": {"error": f"Identification Type {NATIONAL_ID} is missing."}, "details": {}, "todo": []}
	profiles = frappe.db.sql("""select name, trim(legacy_untyped_id_number) as number from `tabRed Profile`
		where ifnull(trim(legacy_untyped_id_number), '') != ''""", as_dict=True)
	patterns, examples = Counter(), defaultdict(list)
	for profile in profiles:
		kind = id_pattern(profile.number)
		patterns[kind] += 1
		if kind != NATIONAL_ID_PATTERN and len(examples[kind]) < 5:
			examples[kind].append(profile.number)
	candidates = [p for p in profiles if id_pattern(p.number) == NATIONAL_ID_PATTERN]
	held = defaultdict(set)
	for parent, number in frappe.db.sql("""select parent, trim(id_number) from `tabRed Profile Identification`
		where parenttype = 'Red Profile' and ifnull(trim(id_number), '') != ''"""):
		held[number].add(parent)
	shared = Counter(p.number for p in candidates)
	primary = set(_column("select distinct parent from `tabRed Profile Identification` where parenttype = 'Red Profile' and is_primary = 1"))

	todo, already, elsewhere, twice = [], [], [], defaultdict(list)
	for p in candidates:
		owners = held.get(p.number, set())
		if p.name in owners:
			already.append(p.name)
		elif owners:
			elsewhere.append({"profile": p.name, "number": p.number, "already_on": sorted(owners)})
		elif shared[p.number] > 1:
			twice[p.number].append(p.name)
		else:
			todo.append({"profile": p.name, "number": p.number, "primary": p.name not in primary})
	return {
		"summary": {
			"untyped_numbers": len(profiles),
			"by_pattern": dict(patterns.most_common()),
			"national_id_rows_to_add": len(todo),
			"marked_primary": sum(item["primary"] for item in todo),
			"already_on_the_profile": len(already),
			"skipped_on_another_profile": len(elsewhere),
			"skipped_shared_by_these_profiles": sum(len(v) for v in twice.values()),
		},
		"details": {
			"examples_by_pattern": dict(examples),
			"on_another_profile": elsewhere,
			"shared_by_these_profiles": [{"number": number, "profiles": names} for number, names in sorted(twice.items())],
		},
		"todo": todo,
	}


def apply_identity_numbers(plan, progress, quiet) -> dict:
	last = dict(frappe.db.sql("""select parent, max(idx) from `tabRed Profile Identification`
		where parenttype = 'Red Profile' and parentfield = 'identifications' group by parent"""))
	for done, item in enumerate(plan["todo"], start=1):
		# A row of its own, not a profile save: the profile's other validations
		# have nothing to say about a document number, and some old profiles
		# carry review exceptions a full save would raise again.
		row = frappe.new_doc("Red Profile Identification")
		row.update({
			"parent": item["profile"],
			"parenttype": "Red Profile",
			"parentfield": "identifications",
			"idx": (last.get(item["profile"]) or 0) + 1,
			"id_type": NATIONAL_ID,
			"id_number": item["number"],
			"is_primary": int(item["primary"]),
		})
		row.db_insert()
		frappe.clear_document_cache("Red Profile", item["profile"])
		if done % 500 == 0:
			_commit_page(quiet)
			progress(done, len(plan["todo"]))
	return {"national_id_rows_added": len(plan["todo"])}


# --- 7. life memberships --------------------------------------------------


def plan_life_memberships() -> dict:
	rows = frappe.db.sql("""select m.name, m.valid_to from `tabVMMS Membership` m
		join `tabVMMS Membership Type` t on t.name = m.membership_type
		where t.is_lifetime = 1 and m.membership_status = 'Active' and m.valid_to is not null""", as_dict=True)
	expired = frappe.db.count("VMMS Membership", {"membership_status": "Expired",
		"membership_type": ["in", frappe.get_all("VMMS Membership Type", {"is_lifetime": 1}, pluck="name") or [""]]})
	return {
		"summary": {
			"active_life_memberships_with_an_end_date": len(rows),
			"by_year": dict(sorted(Counter(str(row.valid_to.year) for row in rows).items())),
			"life_memberships_already_expired_not_changed": expired,
		},
		"details": {},
		"todo": [row.name for row in rows],
	}


def apply_life_memberships(plan, progress, quiet) -> dict:
	for name in plan["todo"]:
		frappe.db.set_value("VMMS Membership", name, "valid_to", None, update_modified=False)
		frappe.clear_document_cache("VMMS Membership", name)
	return {"end_dates_cleared": len(plan["todo"])}


# --- 8. event bookings ----------------------------------------------------

BOOKING = "Event Booking"
DETAIL_ROWS = 500


def plan_event_bookings() -> dict:
	"""Unclaimed imported bookings whose person has an account now.

	The history import links a booking to its account when the account is
	there. A person brought in by a later people catch-up never had theirs
	linked, and Buzz lists a person's bookings by account. The account comes
	from the old booking as archived: its user, or else its primary contact's
	email, through the people session's record of that account.
	"""
	from onerc_vmms_migrate.normalise import email as clean_email
	from onerc_vmms_migrate.placeholder_emails import PATTERN

	if not frappe.db.exists("DocType", BOOKING) or not frappe.get_meta(BOOKING).has_field("legacy_unclaimed_booking"):
		return {"summary": {"error": "Buzz or the legacy booking fields are not installed."}, "details": {}, "todo": []}
	unclaimed = frappe.get_all(BOOKING, filters={"legacy_unclaimed_booking": 1, "user": ["is", "not set"]}, pluck="name")
	sources = dict(frappe.db.sql("""select target_name, source_payload from `tabLegacy VMMS Document`
		where source_doctype = %s and target_doctype = %s and conversion_status = 'Converted'
		and target_name in %s""", (BOOKING, BOOKING, tuple(unclaimed) or ("",)))) if unclaimed else {}
	imported = dict(frappe.db.sql("""select source_name, target_name from `tabLegacy Migration Record`
		where source_doctype = 'User' and target_doctype = 'User'"""))

	def account(candidate):
		if not candidate or candidate in SYSTEM_ACCOUNTS or PATTERN.fullmatch(candidate):
			return None
		found = imported.get(candidate) or (candidate if frappe.db.exists("User", candidate) else None)
		return found if found and found not in SYSTEM_ACCOUNTS else None

	todo, outcomes, left = [], Counter(), []
	for name in unclaimed:
		payload = sources.get(name)
		if not payload:
			outcomes["no archived old booking"] += 1
			left.append({"booking": name, "why": "no archived old booking"})
			continue
		data = json.loads(payload)
		old = data.get("record", data) if isinstance(data, dict) else {}
		by_user = account(old.get("user"))
		contact = clean_email(old.get("primary_contact"))
		user = by_user or account(contact)
		if user:
			outcomes["link by its user" if by_user else "link by its primary contact"] += 1
			todo.append({"booking": name, "user": user})
		else:
			why = "placeholder address" if contact and PATTERN.fullmatch(contact) else "no account yet"
			outcomes[why] += 1
			left.append({"booking": name, "why": why})
	return {
		"summary": {"unclaimed": len(unclaimed), "to_link": len(todo), **dict(outcomes)},
		"details": {"to_link": todo[:DETAIL_ROWS], "left_unclaimed": left[:DETAIL_ROWS]},
		"todo": todo,
	}


def apply_event_bookings(plan, progress, quiet) -> dict:
	linked = 0
	for done, item in enumerate(plan["todo"], start=1):
		# A plain write: the booking is submitted and its hooks send tickets.
		if not frappe.db.get_value(BOOKING, item["booking"], "user"):
			frappe.db.set_value(BOOKING, item["booking"], {"user": item["user"], "legacy_unclaimed_booking": 0},
				update_modified=False)
			linked += 1
		if done % 500 == 0:
			_commit_page(quiet)
			progress(done, len(plan["todo"]))
	return {"bookings_linked": linked}


# --- 9. routing of imported waiting records -------------------------------


def plan_approval_routing() -> dict:
	"""Imported records in review whose queue differs from who may decide today.

	vmmsx's daily resync (repair.resync_pending) moves them at the first run
	after the cutover, and Frappe emails each new assignee. While the cutover
	lasted, a change of County Coordinator or area did not move them, so every
	such change made then would be emailed at once.
	"""
	from vmmsx.approvals.services import assignment, config, engine

	todo, outcomes, failed = [], Counter(), []
	reminders = SlaReminders()
	for doctype in config.governed_doctypes():
		if not frappe.db.exists("DocType", doctype):
			continue
		imported = set(_column("""select target_name from `tabLegacy Migration Record` where target_doctype = %s""", (doctype,)))
		waiting = frappe.get_all(doctype, filters={"approval_state": "In Review", "approval_stage": ["is", "set"]}, pluck="name")
		for name in waiting:
			if name not in imported:
				continue
			try:
				doc = frappe.get_doc(doctype, name)
				admitted = sorted(engine.authorised(doc)["approvers"])
				reminders.count(doc)
			except Exception as error:
				failed.append({"doctype": doctype, "record": name, "why": str(error)[:200]})
				continue
			if not admitted:
				outcomes[f"{doctype}: nobody may decide (left as it is)"] += 1
			elif sorted(assignment.assignees(doctype, name)) == admitted:
				outcomes[f"{doctype}: already queued right"] += 1
			else:
				outcomes[f"{doctype}: to move"] += 1
				todo.append({"doctype": doctype, "record": name})
	return {
		"summary": {"to_move": len(todo), "cannot_check": len(failed), **dict(outcomes),
			"overdue_reminders_after_the_cutover": reminders.summary()},
		"details": {"to_move": todo[:DETAIL_ROWS], "cannot_check": failed[:DETAIL_ROWS]},
		"todo": todo,
	}


class SlaReminders:
	"""What vmmsx's daily SLA sweep would do with the imported backlog once the
	scheduler runs: a bell, every day, to each routed reviewer of each record
	overdue at a notify-only stage. Counted for the operator; nothing changes
	(whether to keep them is the society's decision)."""

	def __init__(self):
		from frappe.utils import add_days

		self.now, self.week = now_datetime(), add_days(now_datetime(), 7)
		self.overdue = self.within_a_week = self.bells_a_day = 0
		self.reviewers = set()

	def count(self, doc) -> None:
		from vmmsx.approvals.services import config, contract, routing, sla

		workflow = config.for_doctype(doc.doctype)
		stage = config.stage_by_name(workflow, contract.stage(doc))
		if not stage or stage.on_sla_breach != sla.BREACH_NOTIFY_ONLY:
			return
		if sla.is_breached(doc, stage, self.week):
			late = routing.routed(stage, config.stage_anchor(doc, workflow, stage))
			self.reviewers.update(late)
			if sla.is_breached(doc, stage, self.now):
				self.overdue += 1
				self.bells_a_day += len(late)
			else:
				self.within_a_week += 1

	def summary(self) -> dict:
		return {"overdue_now": self.overdue, "bells_a_day_now": self.bells_a_day,
			"overdue_within_a_week": self.within_a_week, "reviewers_reminded": len(self.reviewers)}


def apply_approval_routing(plan, progress, quiet) -> dict:
	from frappe.desk.form import assign_to
	from vmmsx.approvals.services import repair

	moved = 0
	# The same move the daily resync makes, with Frappe's assignment notice off
	# for this process, as for the waiting applications (repair 4).
	with silence.replacing(assign_to, "notify_assignment", lambda *args, **kwargs: None):
		for done, item in enumerate(plan["todo"], start=1):
			if repair._resync_one(item["doctype"], item["record"]):
				moved += 1
			if done % PAGE == 0:
				_commit_page(quiet)
				progress(done, len(plan["todo"]))
	return {"moved": moved}


# --- 10. reviewer notes ---------------------------------------------------

NOTE_SOURCES = {"Job Applicant": APPLICATION, "VM Membership": "VMMS Membership"}


def plan_reviewer_notes() -> dict:
	"""The old reviewers' comments, each matched to the record its application
	or membership became.

	Read from the selected capture. Only human comments (type Comment) on old
	applications and memberships; an application left alone (a job or
	opportunity application) has no record here, so its comments are counted
	and stay in the capture. A comment keeps its old name, so one already
	brought across is found and not added twice.
	"""
	targets = {}
	for source, target in NOTE_SOURCES.items():
		for name, found in frappe.db.sql("""select source_name, target_name from `tabLegacy VMMS Document`
			where source_doctype = %s and target_doctype = %s and conversion_status = 'Converted'""", (source, target)):
			targets[(source, str(name))] = (target, found)
		for name, found in frappe.db.sql("""select source_name, target_name from `tabLegacy Migration Record`
			where source_doctype = %s and target_doctype = %s""", (source, target)):
			targets[(source, str(name))] = (target, found)
	with _capture() as rows:
		if rows is None:
			return {"summary": {"error": "The selected capture has no history."}, "details": {}, "todo": []}
		notes = [row for row in rows("Comment")
			if row.get("comment_type") == "Comment" and row.get("reference_doctype") in NOTE_SOURCES]
	present = set(_column("select name from tabComment where name in %s", (tuple(n["name"] for n in notes) or ("",),)))
	todo, outcomes, unmatched = [], Counter(), []
	for note in notes:
		target = targets.get((note["reference_doctype"], str(note.get("reference_name"))))
		if note["name"] in present:
			outcomes["already here"] += 1
		elif not target or not frappe.db.exists(*target):
			outcomes[f"{note['reference_doctype']} not imported"] += 1
			unmatched.append({"old": f"{note['reference_doctype']} {note.get('reference_name')}", "comment": note["name"]})
		else:
			outcomes[f"to add on {target[0]}"] += 1
			todo.append({"note": note, "target": list(target)})
	states = Counter()
	for item in todo:
		states[f"{item['target'][0]}: {frappe.db.get_value(*item['target'], 'approval_state') or 'no state'}"] += 1
	return {
		"summary": {"old_comments": len(notes), **dict(outcomes), "on_records_now": dict(states)},
		"details": {"examples": [
			{"on": " ".join(item["target"]), "by": item["note"].get("comment_email"), "on_date": str(item["note"].get("creation")),
			 "text": re.sub(r"<[^>]+>", " ", str(item["note"].get("content") or "")).strip()[:200]}
			for item in todo[:DETAIL_ROWS]
		], "not_imported_records": unmatched[:DETAIL_ROWS]},
		"todo": todo,
	}


def apply_reviewer_notes(plan, progress, quiet) -> dict:
	"""Each comment as it was: text, author, date. Written as a row of its own,
	so no mention or follow notice is sent."""
	imported = dict(frappe.db.sql("""select source_name, target_name from `tabLegacy Migration Record`
		where source_doctype = 'User' and target_doctype = 'User'"""))
	added = 0
	for done, item in enumerate(plan["todo"], start=1):
		note = item["note"]
		if frappe.db.exists("Comment", note["name"]):
			continue
		author = note.get("owner")
		author = imported.get(author) or (author if author and frappe.db.exists("User", author) else "Administrator")
		comment = frappe.get_doc({
			"doctype": "Comment",
			"name": note["name"],
			"comment_type": "Comment",
			"reference_doctype": item["target"][0],
			"reference_name": item["target"][1],
			"content": note.get("content"),
			"comment_email": note.get("comment_email"),
			"comment_by": note.get("comment_by"),
			"published": 0,
			"seen": int(note.get("seen") or 0),
			"owner": author,
			"modified_by": author,
			"creation": note.get("creation"),
			"modified": note.get("modified") or note.get("creation"),
		})
		comment.db_insert()
		added += 1
		if done % 500 == 0:
			_commit_page(quiet)
			progress(done, len(plan["todo"]))
	return {"comments_added": added}


# --- 11. mail held by the cutover ----------------------------------------

HELD_STATUSES = ("Not Sent", "Partially Sent")
HELD_NOTE = ("Held during the migration cutover and not sent (onerc_vmms_migrate). "
	"Use Retry Sending in the Email Queue list to send it.")


def held_mail() -> list:
	"""Mail the queue would send at its next run. With email muted for the
	cutover nothing leaves, but every row waits; Finish cutover unmutes and the
	first scheduler run sends them all, however old."""
	return frappe.get_all("Email Queue", filters={"status": ["in", HELD_STATUSES]},
		fields=["name", "creation", "status", "reference_doctype", "reference_name", "sender"], order_by="creation asc")


def plan_held_mail() -> dict:
	rows = held_mail()
	recipients = defaultdict(list)
	for parent, recipient in frappe.db.sql("""select parent, recipient from `tabEmail Queue Recipient`
		where parent in %s""", (tuple(row.name for row in rows) or ("",),)):
		recipients[parent].append(recipient or "")
	domains = Counter(address.rsplit("@", 1)[-1].lower() for people in recipients.values() for address in people if "@" in address)
	return {
		"summary": {
			"held": len(rows),
			"recipients": sum(len(people) for people in recipients.values()),
			"by_reference": dict(Counter(row.reference_doctype or "(none)" for row in rows).most_common()),
			"by_day": dict(sorted(Counter(str(row.creation)[:10] for row in rows).items())),
			"recipient_domains": dict(domains.most_common(20)),
		},
		"details": {"held": [
			{"email_queue": row.name, "queued_on": str(row.creation), "reference": f"{row.reference_doctype or ''} {row.reference_name or ''}".strip(),
			 "recipients": len(recipients.get(row.name, ())), "sender": row.sender}
			for row in rows[:DETAIL_ROWS]
		]},
		"todo": [row.name for row in rows],
	}


def apply_held_mail(plan, progress, quiet) -> dict:
	set_aside = 0
	for part in _chunks(plan["todo"]):
		# Only rows still waiting: one sent since the plan was made stays sent.
		frappe.db.sql("""update `tabEmail Queue` set status = 'Error', error = %s
			where name in %s and status in %s""", (HELD_NOTE, tuple(part), HELD_STATUSES))
		set_aside += len(part)
	return {"set_aside": set_aside}


PLANS = {
	"system_manager": plan_system_manager,
	"desk_access": plan_desk_access,
	"coordinator_areas": plan_coordinator_areas,
	"waiting_applications": plan_waiting_applications,
	"approved_without_volunteer": plan_approved_without_volunteer,
	"identity_numbers": plan_identity_numbers,
	"life_memberships": plan_life_memberships,
	"event_bookings": plan_event_bookings,
	"approval_routing": plan_approval_routing,
	"reviewer_notes": plan_reviewer_notes,
	"held_mail": plan_held_mail,
}
APPLY = {
	"system_manager": apply_system_manager,
	"desk_access": apply_desk_access,
	"coordinator_areas": apply_coordinator_areas,
	"waiting_applications": apply_waiting_applications,
	"approved_without_volunteer": apply_approved_without_volunteer,
	"identity_numbers": apply_identity_numbers,
	"life_memberships": apply_life_memberships,
	"event_bookings": apply_event_bookings,
	"approval_routing": apply_approval_routing,
	"reviewer_notes": apply_reviewer_notes,
	"held_mail": apply_held_mail,
}


# --- helpers --------------------------------------------------------------


class Quiet:
	"""Refuse the commit if this run left a message on its way out.

	Counted for rows this worker's user created since the repair began, so a
	person using the site at the same time cannot stop it.
	"""

	def __init__(self):
		self.since = now_datetime()
		self.user = frappe.session.user
		self.tables = [table for table in QUIET if frappe.db.table_exists(table)]

	def check(self) -> None:
		grew = {
			table: count for table in self.tables
			if (count := frappe.db.count(table, {"creation": [">=", self.since], "owner": self.user}))
		}
		if grew:
			raise silence.SilenceBreached(f"a message was about to leave: {grew}")


def _commit_page(quiet) -> None:
	quiet.check()
	frappe.db.commit()


@contextmanager
def _capture():
	"""Rows of one old doctype at a time, from the selected capture's history file."""
	capture = frappe.db.get_single_value(SETTINGS, "browser_capture")
	path = workspace.path("captures", capture, "transfer.sqlite") if capture else None
	if not path or not os.path.isfile(path):
		yield None
		return
	with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
		def rows(doctype):
			for (payload,) in db.execute("""select r.payload from history_index i join records r
				on r.record_key = i.record_key and r.dataset = 'history' where i.doctype = ?""", (doctype,)):
				yield json.loads(payload)["record"]

		try:
			db.execute("select 1 from history_index limit 1")
		except sqlite3.OperationalError:
			yield None
			return
		yield rows


def _assert_people_finished() -> None:
	from onerc_vmms_migrate.batches import checkpoint

	batch = checkpoint()
	if batch and batch.get("status") != "finished":
		frappe.throw("Finish the people import before repairing what it wrote.")


def _previewed() -> bool:
	reports = os.path.dirname(workspace.path("reports", "placeholder"))
	return os.path.isdir(reports) and any(name.endswith("-repairs-preview") for name in os.listdir(reports))


def _scheduler_on() -> bool:
	return bool(frappe.db.get_single_value("System Settings", "enable_scheduler"))


def _run_id(kind: str) -> str:
	return f"{now_datetime().strftime('%Y%m%d-%H%M%S')}-{kind}"


def _phase(progress, key):
	return lambda processed, total: _report(progress, key, processed, total)


def _report(progress, key, processed, total) -> None:
	if progress:
		progress({"phase": key, "processed": processed, "total": total})


def _column(sql, values=()):
	return [row[0] for row in frappe.db.sql(sql, values)]


def _chunks(items, size=500):
	items = list(items)
	for start in range(0, len(items), size):
		yield items[start : start + size]


def _profile_holders(users, roles) -> set[str]:
	found = set()
	for part in _chunks(users):
		found |= set(_column("""select distinct u.parent from `tabUser Role Profile` u
			join `tabHas Role` h on h.parent = u.role_profile and h.parenttype = 'Role Profile' and h.role in %s
			where u.parent in %s""", (tuple(roles), part)))
	return found


def _people(users) -> dict:
	"""How many of these accounts are volunteers, members, or neither."""
	users = list(users)
	volunteers, members = set(), set()
	for part in _chunks(users):
		volunteers |= set(_column("""select p.user from `tabRed Profile` p join `tabVMMS Volunteer` v on v.red_profile = p.name
			where p.user in %s""", (part,)))
		members |= set(_column("""select p.user from `tabRed Profile` p join `tabVMMS Member` m on m.red_profile = p.name
			where p.user in %s""", (part,)))
	return {
		"counts": {
			"volunteers": len(volunteers),
			"members_not_volunteers": len(members - volunteers),
			"neither": len(set(users) - volunteers - members),
		},
		"volunteer_or_member": sorted(volunteers | members),
	}
