"""Helpdesk tickets from the old site, with their threads (0.10.0).

Part of the history import (`Loader.helpdesk`). The old site ran helpdesk
1.29-1.31 on Frappe v15; the destination runs helpdesk develop. Each record
goes through `historical.insert`, so no ticket hook, SLA job, assignment rule,
acknowledgement or feedback email runs, and nothing is sent.

Order:
1. masters the tickets link to (statuses, priorities, types, feedback options,
   the holiday list, the SLA, teams, agents). A record the destination already
   has under the same name (helpdesk seeds Open, Billing, Default...) is linked
   to as it is, never updated; any difference is reported;
2. tickets, under their old numbers. A number the destination already uses is
   reported and that old ticket is not imported: nothing is overwritten;
3. merge links between tickets, once both ends are in;
4. the thread: emails (Communication), comments, assignments (ToDo) and the
   change history (Version) the status timeline reads.

Comment, Communication, ToDo and Version are captured as whole tables. `SCOPE`
keeps the rows that belong to tickets; nothing else of those tables is
archived, converted or reported here.
"""

import json
from collections import Counter
from contextlib import contextmanager

import frappe

from onerc_vmms_migrate import historical

TICKET = "HD Ticket"
MASTERS = (
	"HD Ticket Status",
	"HD Ticket Priority",
	"HD Ticket Type",
	"HD Ticket Feedback Option",
	"HD Service Holiday List",
	"HD Service Level Agreement",
	"HD Team",
	"HD Agent",
)
THREAD = ("Communication", "HD Ticket Comment", "Comment", "ToDo", "Version")
# Source doctypes this module converts, in dependency order.
CONVERTED = (*MASTERS, TICKET, *THREAD)
# Whole tables in the capture: only the rows that belong to a ticket.
SCOPE = {
	"Communication": ("reference_doctype", (TICKET,)),
	"Comment": ("reference_doctype", (TICKET, "HD Ticket Comment")),
	"ToDo": ("reference_type", (TICKET,)),
	"Version": ("ref_doctype", (TICKET,)),
}
# Kept whole in the archive, never converted: develop no longer reads ticket
# activity (its timeline reads Version), the three articles are drafts and the
# old in-app notifications must not reappear. Every other captured Helpdesk
# doctype that is not converted is archived the same way (see archive_only).
ARCHIVE_ONLY = ("HD Ticket Activity", "HD Article", "HD Article Category", "HD Notification")
# Imported rows the outbox guard may allow, and the ticket link each must carry.
OUTBOX = {"Communication": "reference_doctype", "ToDo": "reference_type"}
# Restoring a file onto these must not add a new "Attachment" comment dated
# today: the old site's own attachment comments come across with the thread.
QUIET_ATTACHMENTS = (TICKET, "Communication", "Comment", "HD Ticket Comment")
# Targets of these keep their old creator (history_load.OWNER_DOCTYPES).
OWNED = (TICKET, "Communication", "Comment", "HD Ticket Comment", "ToDo")

TICKET_OMIT = ("merged_with", "ticket_split_from", "contact", "customer", "email_account")
OMIT = {
	TICKET: TICKET_OMIT,
	"HD Team": ("assignment_rule",),
	"Communication": ("email_account",),
	"ToDo": ("assignment_rule",),
}


class TicketCollision(ValueError):
	pass


# Scope ------------------------------------------------------------------------


def scoped_rows(history, doctype):
	"""Captured rows of a doctype the helpdesk import reads. For a SCOPE table,
	only the ticket rows; for a child table of one, only its scoped parents'
	rows. Anything else is every captured row (`History.rows`)."""
	cache = getattr(history, "_helpdesk_scope", None)
	if not isinstance(cache, dict):
		cache = {}
		try:
			history._helpdesk_scope = cache
		except AttributeError:
			pass
	if doctype in cache:
		return cache[doctype]
	if doctype in SCOPE:
		field, values = SCOPE[doctype]
		rows = list(history.rows_where(doctype, field, values))
	else:
		parents = _parents(history, doctype)
		scoped = [parent for parent in parents if parent in SCOPE]
		if not scoped:
			rows = list(history.rows(doctype))
		else:
			keep = {parent: {str(r["name"]) for r in scoped_rows(history, parent)} for parent in scoped}
			names = sorted(set().union(*keep.values()))
			rows = [
				r for r in history.rows_where(doctype, "parent", names)
				if str(r.get("parent")) in keep.get(r.get("parenttype"), ())
			]
			others = [parent for parent in parents if parent not in SCOPE]
			if others:
				rows += list(history.rows_where(doctype, "parenttype", others))
	cache[doctype] = rows
	return rows


def _parents(history, doctype):
	schemas = getattr(history, "schemas", None)
	if not isinstance(schemas, dict):
		return []
	return sorted(
		parent for parent, schema in schemas.items()
		if any(f.get("fieldtype") in ("Table", "Table MultiSelect") and f.get("options") == doctype
		       for f in (schema or {}).get("fields", []))
	)


def archive_only(schemas):
	"""ARCHIVE_ONLY, and every other captured Helpdesk doctype this module does
	not convert, so no old helpdesk record is left out of the archive."""
	found = [dt for dt in ARCHIVE_ONLY if dt in schemas]
	for dt, schema in sorted(schemas.items()):
		if (schema or {}).get("module") == "Helpdesk" and not schema.get("istable") \
				and dt not in CONVERTED and dt not in found:
			found.append(dt)
	return found


def captured_without_tickets(snapshot):
	"""The old database had tickets, but this capture did not take them (an
	exporter before 0.5.0)."""
	tables = snapshot.get("source", {}).get("continuity", {}).get("tables") or {}
	return "tabHD Ticket" in tables and TICKET not in snapshot.get("schemas", {})


# Outbox and files ---------------------------------------------------------------


def allow_outbox(loader, target_dt, name):
	"""Tell the run's outbox guard about a Communication or ToDo this import
	wrote on purpose. Only after historical.insert succeeded, which refuses an
	existing name, so an allowance never covers a row anyone else wrote."""
	guard = getattr(loader, "guard", None)
	if guard is not None and target_dt in OUTBOX:
		guard.expect(target_dt, name)


@contextmanager
def quiet_attachment_comments():
	from frappe.core.doctype.file.file import File

	original = File.create_attachment_record

	def create_attachment_record(self):
		if self.attached_to_doctype in QUIET_ATTACHMENTS:
			return None
		return original(self)

	File.create_attachment_record = create_attachment_record
	try:
		yield
	finally:
		File.create_attachment_record = original


# Ledger helpers -------------------------------------------------------------------


def _batched_index(loader):
	return getattr(loader, "converted_index", None) if getattr(loader, "batch_limit", None) is not None else None


def link_existing(loader, source_dt, row, target_dt, target_name, warning=None):
	"""Record that an old row is an existing destination record, unchanged."""
	identity = (source_dt, str(row["name"]))
	if identity in loader.excluded_identities:
		return None
	index = _batched_index(loader)
	done = index.get(identity) if index is not None else historical.converted(loader.snapshot, source_dt, row["name"])
	if not done:
		historical.mark(loader.snapshot, source_dt, row["name"], target_dt, target_name, loader.run_id)
		if index is not None:
			index[identity] = target_name
		loader.results.append({"source_doctype": source_dt, "source_name": row["name"],
			"target_doctype": target_dt, "target_name": target_name, "outcome": "existing"})
		if warning:
			loader.warnings.append({"source_doctype": source_dt, "source_name": row["name"],
				"target_name": target_name, "warning": warning})
		done = target_name
	loader.targets[(source_dt, row["name"])] = done
	return done


def exclude_row(loader, source_dt, row, reason):
	identity = (source_dt, str(row["name"]))
	if identity in loader.excluded_identities:
		return
	historical.exclude(loader.snapshot, source_dt, row["name"], reason, loader.run_id)
	loader.excluded_identities.add(identity)
	loader.__dict__.setdefault("excluded_reasons", {})[identity] = reason
	loader.excluded.append({"source_doctype": source_dt, "source_name": row["name"], "reason": reason})


def fail_row(loader, source_dt, row, target_dt, error):
	identity = (source_dt, str(row["name"]))
	if identity in getattr(loader, "skip_identities", ()):
		return
	loader.failures.append({"source_doctype": source_dt, "source_name": row["name"],
		"target_doctype": target_dt, "error": error})


def convert(loader, source_dt, row, target_dt, build, name=None):
	"""Loader.one, then the outbox allowance and the steps that need the new
	record's name. Returns (target name, created)."""
	before = len(loader.results)
	result = loader.one(source_dt, row, target_dt, build, name=name)
	created = bool(result) and len(loader.results) > before and loader.results[-1].get("outcome") == "created"
	if created:
		allow_outbox(loader, target_dt, result)
	return result, created


def warn_once(loader, source_dt, name, message):
	seen = loader.__dict__.setdefault("_helpdesk_warned", set())
	if (source_dt, str(name), message) in seen:
		return
	seen.add((source_dt, str(name), message))
	loader.warnings.append({"source_doctype": source_dt, "source_name": name, "warning": message})


# The adapter ---------------------------------------------------------------------


def run(loader):
	history = loader.history
	tickets = scoped_rows(history, TICKET)
	if not tickets:
		if captured_without_tickets(loader.snapshot):
			raise ValueError("The old site has helpdesk tickets, but this capture does not. "
				"Update the exporter on the old site to 0.5.0, capture again and select the new capture.")
		return
	if "helpdesk" not in frappe.get_installed_apps():
		raise ValueError("Install Helpdesk on this site before importing the old tickets.")
	drops = loader.__dict__.setdefault("helpdesk_drops", Counter())
	loader.__dict__.setdefault("helpdesk_collisions", {})
	masters(loader)
	ticket_names = tickets_phase(loader, tickets, drops)
	merges(loader, tickets)
	communications(loader, ticket_names, drops)
	comments(loader, ticket_names)
	todos(loader, ticket_names, drops)
	versions(loader, ticket_names)


def masters(loader):
	for dt in MASTERS[:5]:
		for row in scoped_rows(loader.history, dt):
			master(loader, dt, row, lambda r, dt=dt: copy_with_tables(loader, dt, r))
	for row in scoped_rows(loader.history, "HD Service Level Agreement"):
		master(loader, "HD Service Level Agreement", row, lambda r: sla(loader, r))
	for row in scoped_rows(loader.history, "HD Team"):
		master(loader, "HD Team", row, lambda r: team(loader, r))
	for row in scoped_rows(loader.history, "HD Agent"):
		user = loader.target("User", row.get("user"), required=False) if row.get("user") else None
		master(loader, "HD Agent", row, lambda r: agent(loader, r), name=user or row["name"])


def master(loader, dt, row, build, name=None):
	name = name or row["name"]
	identity = (dt, str(row["name"]))
	index = _batched_index(loader)
	done = index.get(identity) if index is not None else historical.converted(loader.snapshot, dt, row["name"])
	existing = None if done or identity in loader.excluded_identities else frappe.db.exists(dt, name)
	if existing:
		# Seeded by helpdesk (Open, Billing, Default...): link, never update.
		return link_existing(loader, dt, row, dt, existing, existing_note(loader, dt, row, existing))
	return convert(loader, dt, row, dt, build, name=name)[0]


def existing_note(loader, dt, row, existing):
	try:
		values = loader.copy_values(dt, row, omit=OMIT.get(dt, ()))
		doc = frappe.get_doc(dt, existing)
		different = sorted(
			field for field, value in values.items()
			if field not in ("creation", "modified", "docstatus") and not _same(value, doc.get(field))
		)
	except Exception as exc:
		different = ["(not compared: " + frappe.utils.strip_html(str(exc)) + ")"]
	note = "Linked to the " + dt + " already on this site; its values are unchanged"
	return note + ("; the old record differs in " + ", ".join(different) if different else "")


def _same(old, new):
	"""Captured values arrive as text ("0.40", "1"): compare numbers as numbers."""
	old, new = ("" if old is None else old), ("" if new is None else new)
	if not _has_value(old) and not _has_value(new):
		return True
	try:
		return float(old) == float(new)
	except (TypeError, ValueError):
		return str(old) == str(new)


def copy_with_tables(loader, dt, row, omit=()):
	"""Same-doctype copy, with each child table copied field by field."""
	values = loader.copy_values(dt, row, omit=omit)
	meta = frappe.get_meta(dt)
	for field in meta.fields:
		if field.fieldtype != "Table" or field.fieldname in omit:
			continue
		child_meta = frappe.get_meta(field.options)
		names = {f.fieldname for f in child_meta.fields if f.fieldtype not in ("Section Break", "Column Break", "Tab Break", "HTML", "Button")}
		values[field.fieldname] = [
			{key: value for key, value in child.items() if key in names}
			for child in loader.history.children(dt, row["name"], field.fieldname)
		]
	return values


def sla(loader, row):
	"""The old agreement comes in switched off and not the default (as the old
	terms of reference did): open tickets keep their recorded SLA dates, and no
	new ticket is measured against it unless staff switch it on."""
	values = copy_with_tables(loader, "HD Service Level Agreement", row)
	values.update(enabled=0, default_sla=0)
	return values


def team(loader, row):
	"""Members whose accounts are on this site; no assignment rule, so nothing
	is handed out automatically (helpdesk makes one when staff next save it)."""
	values = loader.copy_values("HD Team", row, omit=OMIT["HD Team"])
	members, missing = [], []
	for child in loader.history.children("HD Team", row["name"], "users"):
		user = loader.target("User", child.get("user"), required=False) if child.get("user") else None
		if user:
			members.append({"user": user})
		elif child.get("user"):
			missing.append(str(child["user"]))
	if not members:
		raise ValueError("No member of HD Team " + str(row["name"]) + " has an account on this site ("
			+ (", ".join(missing) or "no members") + "); the team needs at least one")
	if missing:
		warn_once(loader, "HD Team", row["name"], "Members without an account on this site left out: " + ", ".join(missing))
	values["users"] = members
	return values


def agent(loader, row):
	"""Imported switched off: no ticket is routed to an old agent until staff
	switch them on (helpdesk then gives the account its Agent role)."""
	user = loader.target("User", row.get("user"))
	values = loader.copy_values("HD Agent", row)
	values.update(user=user, is_active=0)
	return values


def ticket_key(row):
	name = str(row["name"])
	return (0, int(name), name) if name.isdigit() else (1, 0, name)


def tickets_phase(loader, tickets, drops):
	tickets = sorted(tickets, key=ticket_key)
	# The old numbers, the old counter and anything already here: new tickets
	# made after the import start above all of them.
	loader.naming.reserve(TICKET, TICKET, [r["name"] for r in tickets] + frappe.get_all(TICKET, pluck="name"))
	contacts = loader.__dict__.setdefault("_helpdesk_contacts", {})
	for row in tickets:
		def build(r):
			clash = frappe.db.get_value(TICKET, r["name"], ["name", "subject", "creation", "raised_by"], as_dict=True)
			if clash:
				loader.helpdesk_collisions[str(r["name"])] = {
					"ticket": r["name"], "old_subject": r.get("subject"), "old_created": r.get("creation"),
					"old_raised_by": r.get("raised_by"), "existing_subject": clash.subject,
					"existing_created": clash.creation, "existing_raised_by": clash.raised_by}
				raise TicketCollision(
					f"HD Ticket {r['name']} already exists on this site ('{clash.subject}', created {clash.creation}). "
					f"The old ticket ('{r.get('subject')}', created {r.get('creation')}) was not imported and nothing "
					"was overwritten; decide which keeps the number.")
			values = loader.copy_values(TICKET, r, omit=TICKET_OMIT)
			values["source"] = "Portal" if int(r.get("via_customer_portal") or 0) else "Email"
			raised_by = r.get("raised_by")
			if raised_by and raised_by not in contacts:
				contacts[raised_by] = frappe.db.get_value("Contact", {"email_id": raised_by}, "name")
			values["contact"] = contacts.get(raised_by) if raised_by else None
			if r.get("contact") and not values["contact"]:
				drops[(TICKET, "contact: no Contact with the raiser's email on this site")] += 1
			for field in ("customer", "email_account"):
				if r.get(field):
					loader.warn(TICKET, r, f"Old {field} {r[field]} not carried; kept in the archive")
			return values

		name, created = convert(loader, TICKET, row, TICKET, build, name=row["name"])
		if created:
			assignees(loader, row, name)
	return {str(r["name"]) for r in tickets}


def assignees(loader, row, name):
	"""`_assign` as the old ticket had it, for the accounts that are here.
	Insertion clears it (Document._validate), so it is set afterwards."""
	try:
		assigned = json.loads(row.get("_assign") or "[]")
	except (TypeError, ValueError):
		assigned = []
	kept, missing = [], []
	for user in assigned if isinstance(assigned, list) else []:
		target = loader.target("User", user, required=False)
		(kept.append(target) if target else missing.append(str(user)))
	if kept:
		frappe.db.set_value(TICKET, name, "_assign", json.dumps(kept), update_modified=False)
	if missing:
		warn_once(loader, TICKET, row["name"], "Assigned to accounts not on this site, left out of the assignment list: "
			+ ", ".join(missing))


def merges(loader, tickets):
	for row in tickets:
		name = loader.targets.get((TICKET, row["name"]))
		if not name:
			continue
		for field in ("merged_with", "ticket_split_from"):
			if not row.get(field):
				continue
			other = loader.targets.get((TICKET, str(row[field]))) or loader.target(TICKET, row[field], required=False)
			if not other:
				warn_once(loader, TICKET, row["name"], f"{field} {row[field]}: that ticket was not imported; link left empty")
			elif frappe.db.get_value(TICKET, name, field) != other:
				frappe.db.set_value(TICKET, name, field, other, update_modified=False)


def ticket_of(loader, source_dt, row, field, ticket_names):
	"""The imported ticket a thread row belongs to. A row whose ticket was
	deleted on the old site stays in the archive."""
	ticket = str(row.get(field) or "")
	if ticket not in ticket_names:
		from onerc_vmms_migrate.history_load import SourceIdentityExcluded

		if ticket:
			reason = f"Its ticket {ticket} was deleted on the old site"
		elif row.get("comment_type") == "Deleted":
			reason = "The old site's note of a deleted ticket; it names no ticket"
		else:
			reason = "It names no ticket"
		raise SourceIdentityExcluded(reason + "; kept in the archive")
	return loader.target(TICKET, ticket)


def communications(loader, ticket_names, drops):
	rows = scoped_rows(loader.history, "Communication")
	links = Counter(str(r.get("parent")) for r in scoped_rows(loader.history, "Communication Link"))
	for row in sorted(rows, key=lambda r: (str(r.get("communication_date") or r.get("creation") or ""), str(r["name"]))):
		def build(r):
			values = loader.copy_values("Communication", r, omit=OMIT["Communication"])
			values["reference_doctype"] = TICKET
			values["reference_name"] = ticket_of(loader, "Communication", r, "reference_name", ticket_names)
			if r.get("email_account"):
				drops[("Communication", "email_account: the old outgoing mail account is not on this site")] += 1
			if links.get(str(r["name"])):
				drops[("Communication", "timeline links to contacts and customers (Communication Link rows)")] += links[str(r["name"])]
			return values

		convert(loader, "Communication", row, "Communication", build, name=row["name"])


def comments(loader, ticket_names):
	"""Ticket comments. helpdesk develop keeps them in core Comment (with
	is_pinned) under their old names; an older destination keeps HD Ticket
	Comment. helpdesk 1.31 had already copied them into core Comment on the old
	site; that copy is the same comment and is linked, not imported twice."""
	core = frappe.get_meta("Comment").has_field("is_pinned")
	target_dt = "Comment" if core else "HD Ticket Comment"
	captured = scoped_rows(loader.history, "Comment")
	mirrored = {str(r["name"]): r for r in captured
		if r.get("reference_doctype") == TICKET and r.get("comment_type") == "Comment"}
	hd_comments = scoped_rows(loader.history, "HD Ticket Comment")
	hd_names = {str(r["name"]) for r in hd_comments}
	for row in hd_comments:
		def build(r):
			ticket = ticket_of(loader, "HD Ticket Comment", r, "reference_ticket", ticket_names)
			if not core:
				values = loader.copy_values("HD Ticket Comment", r)
				values["reference_ticket"] = ticket
				return values
			copy = mirrored.get(str(r["name"]))
			if copy:
				values = loader.copy_values("Comment", copy)
			else:
				# What helpdesk's own upgrade writes (migrate_hd_ticket_comment_to_comment).
				author = r.get("commented_by") or r.get("owner")
				user = loader.target("User", author, required=False) if author else None
				values = {"comment_type": "Comment", "comment_email": author,
					"comment_by": (frappe.db.get_value("User", user, "full_name") if user else None) or author,
					"content": r.get("content"), "is_pinned": int(r.get("is_pinned") or 0), "docstatus": 0}
			values.update(reference_doctype=TICKET, reference_name=ticket)
			return values

		convert(loader, "HD Ticket Comment", row, target_dt, build, name=row["name"])
	for row in captured:
		name = str(row["name"])
		if row.get("reference_doctype") == TICKET and row.get("comment_type") == "Comment" and name in hd_names:
			target = loader.targets.get(("HD Ticket Comment", name))
			if target:
				link_existing(loader, "Comment", row, target_dt, target)
			elif ("HD Ticket Comment", name) in loader.excluded_identities:
				exclude_row(loader, "Comment", row, "The same comment as HD Ticket Comment " + name + ", which stays in the archive")
			else:
				fail_row(loader, "Comment", row, target_dt, "The same comment as HD Ticket Comment " + name + ", which was not imported")
			continue
		if row.get("reference_doctype") == "HD Ticket Comment":
			if row.get("comment_type") not in ("Attachment", "Attachment Removed"):
				exclude_row(loader, "Comment", row, f"A '{row.get('comment_type')}' note on a ticket comment "
					f"({row.get('reference_name') or 'name not recorded'}) that no longer existed on the old site; kept in the archive")
				continue

		def build(r):
			values = loader.copy_values("Comment", r)
			if r.get("reference_doctype") == "HD Ticket Comment":
				values.update(reference_doctype=target_dt,
					reference_name=loader.target("HD Ticket Comment", r.get("reference_name")))
			else:
				values.update(reference_doctype=TICKET,
					reference_name=ticket_of(loader, "Comment", r, "reference_name", ticket_names))
			return values

		convert(loader, "Comment", row, "Comment", build, name=row["name"])


def todos(loader, ticket_names, drops):
	"""Assignments as recorded, open and cancelled. The assignee must have an
	account here; an imported open ToDo shows in their list, unannounced."""
	for row in scoped_rows(loader.history, "ToDo"):
		def build(r):
			ticket = ticket_of(loader, "ToDo", r, "reference_name", ticket_names)
			assignee = loader.target("User", r.get("allocated_to")) if r.get("allocated_to") else None
			values = loader.copy_values("ToDo", r, omit=OMIT["ToDo"])
			values.update(reference_type=TICKET, reference_name=ticket, allocated_to=assignee)
			if r.get("assignment_rule"):
				drops[("ToDo", "assignment_rule: the old assignment rules are not imported")] += 1
			return values

		convert(loader, "ToDo", row, "ToDo", build, name=row["name"])


def versions(loader, ticket_names):
	"""The change history the develop status timeline reads, named as the
	people session names old Versions, with the old author as written."""
	from onerc_vmms_migrate import people_versions

	source_id = str(loader.snapshot["source"].get("source_id") or loader.snapshot["source"]["db_name"])
	for row in scoped_rows(loader.history, "Version"):
		ticket = str(row.get("docname") or "")
		target = loader.targets.get((TICKET, ticket)) if ticket in ticket_names else None
		name = people_versions.name(source_id, row["name"], {"target_doctype": TICKET, "target_name": target}) if target else None

		def build(r):
			docname = ticket_of(loader, "Version", r, "docname", ticket_names)
			return {"ref_doctype": TICKET, "docname": docname, "data": r.get("data"),
				"modified_by": r.get("modified_by"), "docstatus": 0}

		result, created = convert(loader, "Version", row, "Version", build, name=name)
		if created and row.get("owner") and row["owner"] != "Administrator":
			frappe.db.set_value("Version", result, "owner", row["owner"], update_modified=False)


# Reports -------------------------------------------------------------------------


def statuses(targets):
	"""What the imported tickets look like. `targets(source_doctype)` gives
	the converted target names."""
	tickets = targets(TICKET)
	if not tickets:
		return {}
	rows = frappe.get_all(TICKET, filters={"name": ["in", tickets]}, fields=["status", "agent_group", "_assign"])
	return {
		"tickets": dict(Counter(str(r.status) for r in rows)),
		"tickets_without_team": sum(not r.agent_group for r in rows),
		"tickets_assigned": sum(bool(r._assign and r._assign != "[]") for r in rows),
		"thread": {dt: len(targets(dt)) for dt in THREAD},
	}


def write_reports(directory, loader):
	"""history-helpdesk-collisions.csv: old tickets whose number is taken here.
	history-helpdesk-drops.csv: values not carried, counted per kind."""
	from onerc_vmms_migrate import run

	clashes = list(getattr(loader, "helpdesk_collisions", {}).values())
	drops = [{"source_doctype": dt, "not_carried": what, "records": count}
		for (dt, what), count in sorted(getattr(loader, "helpdesk_drops", {}).items())]
	if clashes:
		run._write_csv(directory, "history-helpdesk-collisions.csv", [
			{key: "" if value is None else str(value) for key, value in row.items()} for row in clashes])
	if drops:
		run._write_csv(directory, "history-helpdesk-drops.csv", drops)


SYSTEM = frozenset(("name", "owner", "creation", "modified", "modified_by", "docstatus", "idx", "doctype",
	"parent", "parenttype", "parentfield", "_user_tags", "_comments", "_liked_by", "_seen", "_assign"))
CARRIED = {
	TICKET: ("via_customer_portal", "contact", "merged_with", "ticket_split_from"),
	"HD Ticket Comment": ("content", "reference_ticket", "commented_by", "is_pinned"),
	"Version": ("ref_doctype", "docname", "data"),
}
# Adapters that build their record from CARRIED alone (not a by-name copy).
READ_ONLY_CARRIED = ("HD Ticket Comment", "Version")
SWITCHED_OFF = {
	"HD Service Level Agreement": ("enabled", "default_sla"),
	"HD Agent": ("is_active",),
}


def field_drops(snapshot):
	"""Old helpdesk fields with values the converted records do not carry, in
	the rows of history-fields-not-imported.csv. The archive keeps them all."""
	from onerc_vmms_migrate.history_model import History

	schemas = snapshot.get("schemas", {})
	if TICKET not in schemas or "helpdesk" not in frappe.get_installed_apps():
		return []
	history = History(snapshot)
	core = frappe.get_meta("Comment").has_field("is_pinned")
	drops = []
	for dt in CONVERTED:
		schema = schemas.get(dt)
		rows = scoped_rows(history, dt) if schema else []
		if not rows:
			continue
		target = {"HD Ticket Comment": "Comment" if core else "HD Ticket Comment"}.get(dt, dt)
		fields = {f.fieldname for f in frappe.get_meta(target).fields}
		tables = {f["fieldname"]: f.get("options") for f in schema.get("fields", [])
			if f.get("fieldtype") in ("Table", "Table MultiSelect")}
		counts = Counter()
		for row in rows:
			for field, value in row.items():
				if field in SYSTEM or field in tables or field in CARRIED.get(dt, ()) or not _has_value(value):
					continue
				counts[field] += 1
		for field, count in sorted(counts.items()):
			if field in OMIT.get(dt, ()):
				reason = "left out by the adapter"
			elif field in SWITCHED_OFF.get(dt, ()):
				reason = "imported switched off (decision)"
			elif dt in READ_ONLY_CARRIED and not (dt == "HD Ticket Comment" and not core):
				reason = "not read by the adapter"
			elif field not in fields:
				reason = "the destination has no such field"
			else:
				continue
			drops.append({"source_doctype": dt, "field": field, "rows_with_a_value": count, "reason": reason})
		for field, child in sorted(tables.items()):
			if dt in ("HD Team", "HD Service Level Agreement", "HD Service Holiday List") and field in fields:
				continue
			children = [c for c in scoped_rows(history, child) if c.get("parenttype") == dt and c.get("parentfield") == field]
			if children:
				drops.append({"source_doctype": dt, "field": field, "rows_with_a_value": len(children),
					"reason": "table not carried (rows counted)"})
	return drops


def _has_value(value):
	if value is None:
		return False
	if isinstance(value, str):
		return bool(value.strip()) and value.strip() not in ("0", "0.0")
	if isinstance(value, (int, float)):
		return value != 0
	return bool(value)
