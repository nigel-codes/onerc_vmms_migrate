"""Old fields the history import does not carry, reported (history-fields-not-imported.csv).

The archive keeps every old row whole, so nothing here is lost. But a field the
new doctype lacks, or one an adapter leaves out, would otherwise vanish from
the converted record without a word, and every drop is reported.

Each adapter is declared once here:
- `COPIED`: adapters that copy old fields into a doctype of the same shape by
  field name (`Loader.copy_values`). A field the destination lacks is dropped.
- `OMIT`: fields those adapters leave out of the by-name copy. The adapters use
  these tuples themselves, so the report cannot drift from what they do.
- `CARRIED`: fields an adapter stores another way (a different field, a place,
  a person, an attachment). For the adapters that are not in `COPIED`, this is
  the whole list of what they read; every other field is dropped.
- `CONSULTED`: fields read to decide something whose own value is not stored.
- `TABLES`: child tables an adapter carries, and which of their fields.

Counts are of captured rows with a value, so a preview, an import and a re-run
all report the same thing.
"""

SYSTEM = frozenset((
	"name", "owner", "creation", "modified", "modified_by", "docstatus", "idx", "doctype",
	"parent", "parenttype", "parentfield", "naming_series",
	"_user_tags", "_comments", "_liked_by", "_seen",
))

OMIT = {
	"Project": (
		"company", "cost_center", "customer", "sales_order", "project_template", "users", "department",
		"expected_start_date", "expected_end_date", "actual_start_date", "actual_end_date",
	),
	"Task": ("company", "department", "issue", "depends_on", "type", "parent_task", "lft", "rgt", "old_parent"),
	"Buzz Event": ("ticket_email_template", "ticket_print_format", "default_ticket_type"),
	"Event Booking": ("amended_from", "coupon_code", "payment_status", "status", "user"),
	"Event Ticket": ("qr_code", "amended_from"),
}

COPIED = {
	"Project": "Project",
	"Task": "Task",
	"Event Category": "Event Category",
	"Event Host": "Event Host",
	"Event Venue": "Event Venue",
	"Buzz Event": "Buzz Event",
	"Event Ticket Type": "Event Ticket Type",
	"Event Booking": "Event Booking",
	"Event Ticket": "Event Ticket",
	"Event Check In": "Event Check In",
	"Event": "Event",
}

_DATES = ("expected_start_date", "expected_end_date", "actual_start_date", "actual_end_date")

CARRIED = {
	"Project": ("company", "department", *_DATES),
	"Task": ("department", "parent_task", "lft", "rgt", "old_parent"),
	"Buzz Event": ("company",),
	"Event Booking": ("payment_status", "status", "user", "primary_contact"),
	"Event Ticket": ("qr_code", "booking", "attendee_name"),
	"Personnel Terms of Reference": (
		"title_of_mission", "company", "project", "expected_start_date", "expected_end_date",
		"mission_background", "workflow_state", "notes",
	),
	"Deployment Request Tool": (
		"title", "project", "company", "location", "expected_start_date", "expected_end_date",
		"number_of_volunteers_required", "require_contract_before_deployment", "terms_of_reference",
		"tor_attachment", "tor_url", "notes",
	),
	"Personnel Deployment Request": (
		"employee", "employee_name", "user", "deployment", "project", "terms_of_reference",
		"expected_start_date", "expected_end_date", "deployment_status", "notes", "tor_attachment",
	),
	"Event Registration Question": ("question_type", "question_id", "is_required", "question", "options"),
	"Personnel Availability Schedule": ("employee", "employee_name", "start_date", "end_date", "available_on_holidays"),
	"Timesheet": ("employee", "employee_name"),
	"Timesheet Detail": ("from_time", "hours", "description", "activity_type"),
	"Shift Type": ("start_time", "end_time"),
}

CONSULTED = {
	"Buzz Event": {"event_access": "decides publication: a restricted event comes in unpublished"},
}

# Parent doctype -> table field -> the child fields carried, or None for "every
# field the destination's child table has" (named in TARGET_TABLES).
TABLES = {
	"Personnel Terms of Reference": {
		field: None for field in ("stakeholders", "objectives", "expected_outputs", "approach_methods", "itinerary", "resources")
	},
	"Event Booking": {
		"attendees": ("full_name", "email", "ticket_type", "currency", "amount", "add_on_total", "number_of_add_ons"),
		"responses": ("question_id", "question", "response"),
	},
	"Personnel Availability Schedule": {"schedules": ("day", "shift_type")},
	"Buzz Event": {"event_registration_questions": ()},  # each row is its own record (Event Registration Question)
	"Timesheet": {"time_logs": ()},  # each row is its own record (Timesheet Detail)
}
# Where the generic child tables land, with the old names each one renames.
TARGET_TABLES = {"Personnel Terms of Reference": "VMMS Terms of Reference"}
TABLE_RENAMES = {
	"stakeholder_name", "objectives", "outputs", "approach_methods", "date", "time", "activity_description",
	"resource_name", "qty", "rate", "methodology", "cost_per_day",
}
# Not converted as records of their own, so they have no fields to drop: their
# rows stay in the archive whole.
NOT_REPORTED = {
	"Job Applicant": "the people session converts applications and reports their fields",
	"Shift Type": "read to shape availability slots; never converted itself",
}

ABSENT = "the destination has no such field"
LEFT_OUT = "left out by the adapter"
NOT_READ = "not read by the adapter"
TABLE_NOT_CARRIED = "table not carried (rows counted)"


def field_drops(history, schemas, doctypes, has_field) -> list[dict]:
	"""Old fields with values that the converted records do not carry.

	`history` is a History over the capture, `schemas` the capture's schemas,
	`doctypes` the converted source doctypes and `has_field(doctype, field)`
	asks the destination. One row per (doctype, field) with at least one value.
	"""
	drops = []
	for dt in doctypes:
		schema = schemas.get(dt)
		if not schema or dt in NOT_REPORTED:
			continue
		rows = history.rows(dt)
		if not rows:
			continue
		target = COPIED.get(dt)
		omitted = set(OMIT.get(dt, ()))
		carried = set(CARRIED.get(dt, ()))
		consulted = CONSULTED.get(dt, {})
		tables = {f["fieldname"]: f.get("options") for f in schema.get("fields", [])
		          if f.get("fieldtype") in ("Table", "Table MultiSelect")}
		counts = {}
		for row in rows:
			for field, value in row.items():
				if field in SYSTEM or field in tables or field in carried or not _has_value(value):
					continue
				counts[field] = counts.get(field, 0) + 1
		for field, count in sorted(counts.items()):
			if field in consulted:
				reason = "read, not stored: " + consulted[field]
			elif target is None:
				reason = NOT_READ
			elif field in omitted:
				reason = LEFT_OUT
			elif not has_field(target, field):
				reason = ABSENT
			else:
				continue
			drops.append({"source_doctype": dt, "field": field, "rows_with_a_value": count, "reason": reason})
		drops.extend(_table_drops(history, dt, rows, tables, has_field))
	return drops


def _table_drops(history, dt, rows, tables, has_field):
	drops = []
	carried_tables = TABLES.get(dt, {})
	target_parent = TARGET_TABLES.get(dt)
	for field, child_dt in sorted(tables.items()):
		children = [child for row in rows for child in history.children(dt, row["name"], field)]
		if not children:
			continue
		if field not in carried_tables:
			drops.append({"source_doctype": dt, "field": field, "rows_with_a_value": len(children),
			              "reason": TABLE_NOT_CARRIED})
			continue
		keep = carried_tables[field]
		if keep == ():
			continue
		counts = {}
		for child in children:
			for name, value in child.items():
				if name in SYSTEM or not _has_value(value):
					continue
				if keep is not None and name in keep:
					continue
				if keep is None and (name in TABLE_RENAMES or has_field(("table", target_parent, field), name)):
					continue
				counts[name] = counts.get(name, 0) + 1
		for name, count in sorted(counts.items()):
			drops.append({"source_doctype": f"{child_dt} ({dt}.{field})", "field": name,
			              "rows_with_a_value": count, "reason": NOT_READ if keep is not None else ABSENT})
	return drops


def _has_value(value) -> bool:
	if value is None:
		return False
	if isinstance(value, str):
		return bool(value.strip()) and value.strip() not in ("0", "0.0")
	if isinstance(value, (int, float)):
		return value != 0
	return bool(value)


def destination_fields():
	"""`has_field` against this site's doctypes, remembered per doctype."""
	import frappe

	cache = {}

	def fields(doctype):
		if doctype not in cache:
			if isinstance(doctype, tuple):
				_, parent, table = doctype
				child = frappe.get_meta(parent).get_field(table) if parent and frappe.db.exists("DocType", parent) else None
				cache[doctype] = fields(child.options) if child and child.options else set()
			else:
				meta = frappe.get_meta(doctype) if frappe.db.exists("DocType", doctype) else None
				cache[doctype] = {f.fieldname for f in meta.fields} if meta else set()
		return cache[doctype]

	return lambda doctype, field: field in fields(doctype)
