"""Every old field with a value that a converted record does not carry is reported."""

from unittest import TestCase

from onerc_vmms_migrate import history_fields, history_load


class FakeHistory:
	def __init__(self, rows, children=None):
		self._rows, self._children = rows, children or {}

	def rows(self, doctype):
		return self._rows.get(doctype, [])

	def children(self, doctype, name, field):
		return self._children.get((doctype, name, field), [])


def destination(fields):
	return lambda doctype, field: field in fields.get(doctype, ())


def schema(*fields, tables=()):
	return {"fields": [{"fieldname": f, "fieldtype": "Data"} for f in fields]
		+ [{"fieldname": f, "fieldtype": "Table", "options": child} for f, child in tables]}


class TestFieldDrops(TestCase):
	def drops(self, rows, schemas, fields, children=None, doctypes=None):
		found = history_fields.field_drops(FakeHistory(rows, children), schemas, doctypes or list(schemas), destination(fields))
		return {(row["source_doctype"], row["field"]): (row["rows_with_a_value"], row["reason"]) for row in found}

	def test_a_field_the_destination_lacks_is_reported_with_its_count(self):
		rows = {"Event Booking": [
			{"name": "B1", "company": "Kisumu", "customer": "C1", "event": "1", "owner": "x@y", "net_amount": 0},
			{"name": "B2", "company": "Kisumu", "customer": "", "event": "1", "user": "a@b", "primary_contact": "a@b"},
		]}
		found = self.drops(rows, {"Event Booking": schema("company", "customer", "event", "user", "net_amount")},
			{"Event Booking": {"event", "user", "net_amount"}})
		self.assertEqual(found, {
			("Event Booking", "company"): (2, history_fields.ABSENT),
			("Event Booking", "customer"): (1, history_fields.ABSENT),
		})

	def test_a_field_the_adapter_leaves_out_is_reported_even_where_the_destination_has_it(self):
		rows = {"Buzz Event": [{"name": 1, "ticket_email_template": "T", "company": "Kisumu", "event_access": "Public", "title": "Day"}]}
		found = self.drops(rows, {"Buzz Event": schema("ticket_email_template", "company", "event_access", "title")},
			{"Buzz Event": {"ticket_email_template", "title"}})
		self.assertEqual(found[("Buzz Event", "ticket_email_template")], (1, history_fields.LEFT_OUT))
		self.assertTrue(found[("Buzz Event", "event_access")][1].startswith("read, not stored"))
		self.assertNotIn(("Buzz Event", "company"), found)  # carried as the event's place
		self.assertNotIn(("Buzz Event", "title"), found)

	def test_a_bespoke_adapter_reports_everything_it_does_not_read(self):
		rows = {"Deployment Request Tool": [{"name": "PDR-1", "task": "TASK-1", "location": "Lodwar", "future_deployment_date": "2026-12-01", "title": "Flood"}]}
		found = self.drops(rows, {"Deployment Request Tool": schema("task", "location", "future_deployment_date", "title")}, {})
		self.assertEqual(set(found), {("Deployment Request Tool", "task"), ("Deployment Request Tool", "future_deployment_date")})
		self.assertEqual(found[("Deployment Request Tool", "task")][1], history_fields.NOT_READ)

	def test_tables_are_counted_by_their_rows_and_carried_tables_by_their_fields(self):
		rows = {"Deployment Request Tool": [{"name": "PDR-1"}], "Event Booking": [{"name": "B1"}]}
		children = {
			("Deployment Request Tool", "PDR-1", "skills"): [{"name": "s1", "skill": "First aid"}, {"name": "s2", "skill": "Driving"}],
			("Event Booking", "B1", "attendees"): [{"name": "a1", "full_name": "A B", "email": "a@b", "company": "X"}],
		}
		schemas = {
			"Deployment Request Tool": schema(tables=[("skills", "Designation Skill")]),
			"Event Booking": schema(tables=[("attendees", "Event Booking Attendee")]),
		}
		found = self.drops(rows, schemas, {}, children)
		self.assertEqual(found[("Deployment Request Tool", "skills")], (2, history_fields.TABLE_NOT_CARRIED))
		self.assertEqual(found[("Event Booking Attendee (Event Booking.attendees)", "company")], (1, history_fields.NOT_READ))
		self.assertNotIn(("Event Booking Attendee (Event Booking.attendees)", "email"), found)

	def test_terms_tables_keep_what_the_new_table_has_and_the_renamed_fields(self):
		rows = {"Personnel Terms of Reference": [{"name": "TOR-1"}]}
		children = {("Personnel Terms of Reference", "TOR-1", "resources"): [
			{"name": "r1", "resource_name": "Tent", "qty": 2, "unit": "Nos", "supplier": "Acme"}]}
		fields = {("table", "VMMS Terms of Reference", "resources"): {"unit", "resource", "quantity"}}
		found = self.drops(rows, {"Personnel Terms of Reference": schema(tables=[("resources", "TOR Resources")])}, fields, children)
		self.assertEqual(found, {("TOR Resources (Personnel Terms of Reference.resources)", "supplier"): (1, history_fields.ABSENT)})

	def test_people_session_and_vocabulary_doctypes_are_not_reported(self):
		rows = {"Job Applicant": [{"name": "J1", "anything": "x"}], "Shift Type": [{"name": "Day", "color": "Blue"}]}
		self.assertEqual(self.drops(rows, {"Job Applicant": schema("anything"), "Shift Type": schema("color")}, {}), {})

	def test_empty_values_are_not_drops(self):
		for value in (None, "", " ", 0, 0.0, "0"):
			with self.subTest(value=value):
				self.assertFalse(history_fields._has_value(value))
		for value in ("x", 1, 2.5):
			self.assertTrue(history_fields._has_value(value))


class TestAdaptersShareTheDeclaredOmissions(TestCase):
	def test_the_adapters_use_the_declared_tuples(self):
		import inspect

		source = inspect.getsource(history_load)
		for doctype in history_fields.OMIT:
			with self.subTest(doctype=doctype):
				self.assertIn(f"omit=OMIT['{doctype}']", source)
		self.assertEqual(source.count("omit=('"), 0)

	def test_omitted_and_carried_fields_belong_to_declared_adapters(self):
		for doctype in history_fields.OMIT:
			self.assertIn(doctype, history_fields.COPIED)
