"""Helpdesk tickets in the history import (0.10.0): the rules that need no
database. The import itself is rehearsed on a clone and rolled back."""

import json
import os
import sqlite3
import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate import helpdesk_load, historical, history_import
from onerc_vmms_migrate.history_model import History
from onerc_vmms_migrate.silence import Guard, SilenceBreached


def capture(rows, schemas=None):
	history = {}
	for dt, records in rows.items():
		for row in records:
			history[f"{dt}\0{row['name']}"] = {"doctype": dt, "record": row}
	return {"source": {"source_id": "old"}, "schemas": schemas or {dt: {"fields": []} for dt in rows}, "history": history}


THREADS = {
	"HD Ticket": [{"name": "5"}, {"name": "16856"}],
	"Communication": [
		{"name": "c1", "reference_doctype": "HD Ticket", "reference_name": "5"},
		{"name": "c2", "reference_doctype": "Job Applicant", "reference_name": "a"},
	],
	"Communication Link": [
		{"name": "l1", "parent": "c1", "parenttype": "Communication", "parentfield": "timeline_links"},
		{"name": "l2", "parent": "c2", "parenttype": "Communication", "parentfield": "timeline_links"},
	],
	"Comment": [
		{"name": "m1", "reference_doctype": "HD Ticket", "reference_name": "5", "comment_type": "Comment"},
		{"name": "m2", "reference_doctype": "HD Ticket Comment", "comment_type": "Deleted"},
		{"name": "m3", "reference_doctype": "Employee", "reference_name": "e", "comment_type": "Comment"},
	],
	"HD Ticket Comment": [{"name": "m1", "reference_ticket": "5", "reference_ticket_name": "x"}],
	"HD Comment Reaction": [
		{"name": "r1", "parent": "m1", "parenttype": "Comment", "parentfield": "reactions"},
		{"name": "r2", "parent": "m3", "parenttype": "Comment", "parentfield": "reactions"},
		{"name": "r3", "parent": "m1", "parenttype": "HD Ticket Comment", "parentfield": "reactions"},
	],
	"ToDo": [{"name": "t1", "reference_type": "HD Ticket"}, {"name": "t2", "reference_type": "Employee"}],
	"Version": [{"name": "v1", "ref_doctype": "HD Ticket"}, {"name": "v2", "ref_doctype": "User"}],
}
SCHEMAS = {
	**{dt: {"fields": []} for dt in THREADS},
	"Communication": {"fields": [{"fieldname": "timeline_links", "fieldtype": "Table", "options": "Communication Link"}]},
	"Comment": {"fields": [{"fieldname": "reactions", "fieldtype": "Table", "options": "HD Comment Reaction"}]},
	"HD Ticket Comment": {"fields": [{"fieldname": "reactions", "fieldtype": "Table", "options": "HD Comment Reaction"}]},
	"HD Comment Reaction": {"fields": [], "istable": True},
}


def names(rows):
	return sorted(r["name"] for r in rows)


class TestScope(TestCase):
	"""Comment, Communication, ToDo and Version are whole tables in the
	capture; only the rows that belong to tickets are read."""

	def setUp(self):
		self.history = History(capture(THREADS, SCHEMAS))

	def test_whole_tables_keep_only_their_ticket_rows(self):
		self.assertEqual(names(helpdesk_load.scoped_rows(self.history, "Communication")), ["c1"])
		self.assertEqual(names(helpdesk_load.scoped_rows(self.history, "Comment")), ["m1", "m2"])
		self.assertEqual(names(helpdesk_load.scoped_rows(self.history, "ToDo")), ["t1"])
		self.assertEqual(names(helpdesk_load.scoped_rows(self.history, "Version")), ["v1"])
		self.assertEqual(names(helpdesk_load.scoped_rows(self.history, "HD Ticket")), ["16856", "5"])

	def test_child_rows_follow_their_scoped_parents(self):
		self.assertEqual(names(helpdesk_load.scoped_rows(self.history, "Communication Link")), ["l1"])
		# A reaction on an out-of-scope Comment stays out; HD Ticket Comment is converted whole.
		self.assertEqual(names(helpdesk_load.scoped_rows(self.history, "HD Comment Reaction")), ["r1", "r3"])

	def test_the_sqlite_store_selects_without_loading_the_table(self):
		from onerc_vmms_migrate.history_store import SQLiteHistory, index

		with tempfile.TemporaryDirectory() as directory:
			path = os.path.join(directory, "t.sqlite")
			db = sqlite3.connect(path)
			db.execute("CREATE TABLE records (dataset TEXT, record_key TEXT, payload TEXT)")
			for item in capture(THREADS)["history"].values():
				db.execute("INSERT INTO records VALUES ('history',?,?)",
					(item["doctype"] + item["record"]["name"], json.dumps(item)))
			db.commit()
			index(db)
			db.close()
			store = SQLiteHistory(path, 1)
			self.assertEqual(names(store.rows_where("Comment", "reference_doctype", ("HD Ticket",))), ["m1"])
			self.assertEqual(names(store.rows_where("Communication Link", "parent", ("c1", "c9"))), ["l1"])
			self.assertEqual(store.rows_where("Comment", "reference_doctype", ()), [])
			with self.assertRaises(ValueError):
				store.rows_where("Comment", "x') OR 1=1 --", ("a",))
			history = History({"history": store, "schemas": SCHEMAS})
			self.assertEqual(names(helpdesk_load.scoped_rows(history, "Communication")), ["c1"])
			self.assertNotIn("Communication", history.tables.cache)

	def test_every_old_helpdesk_doctype_is_archived_or_converted(self):
		schemas = {
			"HD Ticket": {"module": "Helpdesk"}, "HD Ticket Activity": {"module": "Helpdesk"},
			"HD View": {"module": "Helpdesk"}, "HD Ticket Template Field": {"module": "Helpdesk", "istable": 1},
			"HD Settings": {"module": "Helpdesk"}, "Buzz Event": {"module": "Events"},
		}
		self.assertEqual(helpdesk_load.archive_only(schemas), ["HD Ticket Activity", "HD Settings", "HD View"])

	def test_the_archive_takes_ticket_rows_and_the_archive_only_doctypes(self):
		snapshot = capture(THREADS, {**SCHEMAS, "HD Ticket Activity": {"fields": [], "module": "Helpdesk"}})
		doctypes = history_import.archived_doctypes(snapshot)
		for dt in ("HD Ticket", "Communication", "Communication Link", "Comment", "ToDo", "Version", "HD Ticket Activity"):
			self.assertIn(dt, doctypes)

	def test_files_of_other_communications_stay_out(self):
		snapshot = {**capture(THREADS, SCHEMAS), "all_file_manifest": {
			"/files/ticket.png": {"references": [{"attached_to_doctype": "Communication", "attached_to_name": "c1"}]},
			"/files/cv.pdf": {"references": [{"attached_to_doctype": "Communication", "attached_to_name": "c2"}]},
			"/files/shot.png": {"references": [{"attached_to_doctype": "HD Ticket", "attached_to_name": "5"}]},
		}}
		with patch("onerc_vmms_migrate.full_run.attachments", return_value={}) as attachments:
			history_import.files(snapshot, MagicMock(), "run", live=True)
		part = attachments.call_args.args[0]
		self.assertEqual(sorted(part["all_file_manifest"]), ["/files/shot.png", "/files/ticket.png"])
		self.assertNotIn("Communication\0c2", part["history"])

	def test_reconcile_reports_archived_only_doctypes(self):
		snapshot = capture({"HD Ticket Activity": [{"name": "a1"}, {"name": "a2"}]},
			{"HD Ticket Activity": {"fields": [], "module": "Helpdesk"}})
		loader = MagicMock(excluded=[], excluded_identities=set(), failures=[])
		with patch.object(history_import, "converted_index", return_value={}):
			records = history_import.reconcile(snapshot, loader)
		self.assertEqual(records, {"HD Ticket Activity": {"captured": 2, "archived_only": 2, "unaccounted": 0}})


class TestGuard(TestCase):
	"""The import writes the old tickets' emails and assignments on purpose;
	the guard allows exactly those rows and nothing else."""

	def setUp(self):
		self.counts = {"Email Queue": 0, "Notification Log": 0, "SMS Log": 0, "Communication": 10, "ToDo": 3}
		self.rows = {"Communication": {}, "ToDo": {}}
		db = MagicMock()
		db.table_exists.return_value = True
		db.count.side_effect = lambda table: self.counts[table]
		self.enterContext(patch.object(frappe, "db", db))

		def get_all(table, filters, fields):
			wanted = filters["name"][1]
			return [{"name": n, **row} for n, row in self.rows[table].items() if n in wanted]

		self.enterContext(patch.object(frappe, "get_all", side_effect=get_all))
		self.guard = Guard()

	def write(self, table, name, reference="HD Ticket"):
		self.counts[table] += 1
		self.rows[table][name] = {Guard.EXPECTABLE[table]: reference}

	def test_expected_ticket_rows_pass(self):
		self.write("Communication", "c1")
		self.write("ToDo", "t1")
		self.guard.expect("Communication", "c1")
		self.guard.expect("ToDo", "t1")
		self.guard.check()

	def test_an_unexpected_row_still_trips(self):
		self.write("Communication", "c1")
		self.write("Communication", "hook-made")
		self.guard.expect("Communication", "c1")
		with self.assertRaisesRegex(SilenceBreached, "'Communication': 1"):
			self.guard.check()

	def test_mail_queues_can_never_be_allowed(self):
		with self.assertRaises(SilenceBreached):
			self.guard.expect("Email Queue", "q1")
		self.counts["Email Queue"] += 1
		with self.assertRaises(SilenceBreached):
			self.guard.check()

	def test_a_rolled_back_insert_is_not_counted(self):
		self.guard.expect("Communication", "gone")  # allowed, then rolled back
		self.counts["Communication"] += 1  # someone else's row
		with self.assertRaises(SilenceBreached):
			self.guard.check()

	def test_an_allowed_row_that_is_not_ticket_history_trips(self):
		self.write("Communication", "c1", reference="User")
		self.guard.expect("Communication", "c1")
		with self.assertRaisesRegex(SilenceBreached, "not ticket history"):
			self.guard.check()


class Stub:
	"""Just enough Loader for the helpdesk adapter."""

	FIELDS = {
		"HD Ticket": {"subject", "status", "raised_by", "priority", "agent_group", "opening_date", "description"},
		"Communication": {"subject", "sender", "recipients", "cc", "content", "sent_or_received", "communication_date",
			"communication_type", "status", "user", "in_reply_to"},
		"Comment": {"comment_type", "comment_email", "comment_by", "content", "reference_doctype", "reference_name", "is_pinned"},
		"HD Ticket Comment": {"content", "reference_ticket", "commented_by", "is_pinned"},
		"ToDo": {"status", "description", "allocated_to", "reference_type", "reference_name", "assigned_by"},
		"HD Agent": {"user", "agent_name", "is_active"},
		"HD Team": {"team_name", "disabled"},
		"HD Service Level Agreement": {"service_level", "enabled", "default_sla", "holiday_list"},
		"HD Ticket Status": {"label_agent", "category", "color"},
	}

	def __init__(self, rows, users=(), schemas=None):
		self.snapshot = capture(rows, schemas)
		self.history = History(self.snapshot)
		self.results, self.failures, self.warnings, self.excluded = [], [], [], []
		self.excluded_identities, self.targets, self.inserted = set(), {}, []
		self.naming, self.guard, self.run_id = MagicMock(), MagicMock(), "run"
		self.users = set(users)

	def one(self, source_dt, row, target_dt, build, name=None):
		from onerc_vmms_migrate.history_load import SourceIdentityExcluded

		if (source_dt, str(row["name"])) in self.targets:
			self.results.append({"outcome": "existing"})
			return self.targets[(source_dt, str(row["name"]))]
		try:
			values = build(row)
		except SourceIdentityExcluded as exc:
			self.excluded.append({"source_doctype": source_dt, "source_name": row["name"], "reason": str(exc)})
			return None
		except Exception as exc:
			self.failures.append({"source_doctype": source_dt, "source_name": row["name"], "error": str(exc)})
			return None
		name = name or "new-" + str(row["name"])
		self.inserted.append((target_dt, name, values))
		self.targets[(source_dt, str(row["name"]))] = name
		self.results.append({"source_doctype": source_dt, "target_name": name, "outcome": "created"})
		return name

	def copy_values(self, doctype, row, omit=()):
		return {k: v for k, v in row.items() if k in self.FIELDS.get(doctype, ()) and k not in omit}

	def target(self, doctype, name, target_dt=None, required=True):
		found = self.targets.get((doctype, str(name))) if doctype != "User" else (name if name in self.users else None)
		if not found and required:
			raise ValueError(f"Unresolved {doctype} reference: {name}")
		return found

	def warn(self, doctype, row, message):
		self.warnings.append({"source_doctype": doctype, "source_name": row["name"], "warning": message})

	def values(self, target_dt):
		return {name: values for dt, name, values in self.inserted if dt == target_dt}


class HelpdeskCase(TestCase):
	def setUp(self):
		self.db = MagicMock()
		self.db.get_value.return_value = None
		self.db.exists.return_value = None
		self.enterContext(patch.object(frappe, "db", self.db))
		self.enterContext(patch.object(frappe, "get_all", return_value=["0001"]))
		self.enterContext(patch.object(historical, "mark"))
		self.enterContext(patch.object(historical, "exclude"))
		self.enterContext(patch.object(historical, "converted", return_value=None))
		self.meta = MagicMock()
		self.meta.has_field.return_value = True
		self.enterContext(patch.object(frappe, "get_meta", return_value=self.meta))


class TestTickets(HelpdeskCase):
	ROWS = {
		"HD Ticket": [
			{"name": "16856", "subject": "Login", "status": "To Test", "raised_by": "a@x.org", "via_customer_portal": 1,
			 "custom_mode_of_support_request": "Portal", "contact": "Amina", "_assign": '["agent@x.org", "gone@x.org"]'},
			{"name": "5", "subject": "Old", "status": "Closed", "raised_by": "b@x.org", "via_customer_portal": 0,
			 "merged_with": "16856"},
		],
	}

	def run_tickets(self, stub):
		return helpdesk_load.tickets_phase(stub, helpdesk_load.scoped_rows(stub.history, "HD Ticket"), stub.__dict__.setdefault("helpdesk_drops", __import__("collections").Counter()))

	def test_old_numbers_kept_source_mapped_and_custom_field_not_carried(self):
		stub = Stub(self.ROWS, users={"agent@x.org"})
		stub.helpdesk_collisions = {}
		self.run_tickets(stub)
		tickets = stub.values("HD Ticket")
		self.assertEqual(list(tickets), ["5", "16856"])  # numeric order
		self.assertEqual(tickets["16856"]["source"], "Portal")
		self.assertEqual(tickets["5"]["source"], "Email")
		self.assertNotIn("custom_mode_of_support_request", tickets["16856"])
		self.assertNotIn("merged_with", tickets["5"])  # set once both are in
		self.assertEqual(stub.helpdesk_drops[("HD Ticket", "contact: no Contact with the raiser's email on this site")], 1)

	def test_the_whole_range_is_reserved_before_the_first_ticket(self):
		stub = Stub(self.ROWS)
		stub.helpdesk_collisions = {}
		self.run_tickets(stub)
		stub.naming.reserve.assert_called_once_with("HD Ticket", "HD Ticket", ["5", "16856", "0001"])

	def test_a_taken_number_is_reported_and_nothing_is_written(self):
		stub = Stub(self.ROWS, users={"agent@x.org"})
		stub.helpdesk_collisions = {}
		taken = frappe._dict(name="5", subject="Welcome to Helpdesk", creation="2026-11-16", raised_by="x")
		self.db.get_value.side_effect = lambda dt, name, *a, **k: taken if dt == "HD Ticket" and name == "5" else None
		self.run_tickets(stub)
		self.assertEqual(list(stub.values("HD Ticket")), ["16856"])
		self.assertIn("nothing was overwritten", stub.failures[0]["error"])
		self.assertEqual(stub.helpdesk_collisions["5"]["existing_subject"], "Welcome to Helpdesk")
		self.db.set_value.assert_called_once()  # only the assignment list of 16856, never ticket 5

	def test_assignment_list_keeps_accounts_that_are_here(self):
		stub = Stub(self.ROWS, users={"agent@x.org"})
		stub.helpdesk_collisions = {}
		self.run_tickets(stub)
		self.db.set_value.assert_called_once_with("HD Ticket", "16856", "_assign", '["agent@x.org"]', update_modified=False)
		self.assertIn("gone@x.org", stub.warnings[-1]["warning"])

	def test_merge_links_are_set_once_both_tickets_are_in(self):
		stub = Stub(self.ROWS)
		stub.targets = {("HD Ticket", "5"): "5", ("HD Ticket", "16856"): "16856"}
		helpdesk_load.merges(stub, self.ROWS["HD Ticket"])
		self.db.set_value.assert_called_once_with("HD Ticket", "5", "merged_with", "16856", update_modified=False)


class TestThreads(HelpdeskCase):
	ROWS = {
		"HD Ticket": [{"name": "5"}],
		"Communication": [
			{"name": "c2", "reference_doctype": "HD Ticket", "reference_name": "5", "sender": "agent@x.org",
			 "recipients": "a@x.org", "sent_or_received": "Sent", "communication_date": "2026-01-02 10:00:00",
			 "email_account": "Support", "content": "Hello"},
			{"name": "c1", "reference_doctype": "HD Ticket", "reference_name": "5", "sender": "a@x.org",
			 "sent_or_received": "Received", "communication_date": "2026-01-01 09:00:00"},
			{"name": "c3", "reference_doctype": "HD Ticket", "reference_name": "404", "sent_or_received": "Received"},
		],
		"Communication Link": [{"name": "l1", "parent": "c2", "parenttype": "Communication", "parentfield": "timeline_links"}],
		"HD Ticket Comment": [{"name": "k1", "reference_ticket": "5", "commented_by": "agent@x.org", "content": "note", "is_pinned": 1}],
		"Comment": [
			{"name": "k1", "reference_doctype": "HD Ticket", "reference_name": "5", "comment_type": "Comment",
			 "comment_email": "agent@x.org", "comment_by": "Agent One", "content": "note", "is_pinned": 1},
			{"name": "a1", "reference_doctype": "HD Ticket", "reference_name": "5", "comment_type": "Attachment", "content": "<a>x</a>"},
			{"name": "d1", "reference_doctype": "HD Ticket Comment", "reference_name": None, "comment_type": "Deleted"},
		],
		"ToDo": [
			{"name": "t1", "reference_type": "HD Ticket", "reference_name": "5", "allocated_to": "agent@x.org", "status": "Open", "description": "x"},
			{"name": "t2", "reference_type": "HD Ticket", "reference_name": "5", "allocated_to": "vendor@x.org", "status": "Cancelled", "description": "x"},
		],
		"Version": [{"name": "v1", "ref_doctype": "HD Ticket", "docname": "5", "data": "{}", "owner": "agent@x.org"}],
	}

	def stub(self):
		stub = Stub(self.ROWS, users={"agent@x.org"}, schemas={**{dt: {"fields": []} for dt in self.ROWS}, **SCHEMAS})
		stub.targets[("HD Ticket", "5")] = "5"
		stub.helpdesk_drops = __import__("collections").Counter()
		return stub

	def test_emails_keep_direction_people_and_dates_in_order(self):
		stub = self.stub()
		helpdesk_load.communications(stub, {"5"}, stub.helpdesk_drops)
		self.assertEqual(list(stub.values("Communication")), ["c1", "c2"])
		sent = stub.values("Communication")["c2"]
		self.assertEqual((sent["sender"], sent["recipients"], sent["sent_or_received"], sent["communication_date"]),
			("agent@x.org", "a@x.org", "Sent", "2026-01-02 10:00:00"))
		self.assertEqual((sent["reference_doctype"], sent["reference_name"]), ("HD Ticket", "5"))
		self.assertNotIn("email_account", sent)
		self.assertEqual(stub.helpdesk_drops[("Communication", "timeline links to contacts and customers (Communication Link rows)")], 1)
		self.assertEqual([c.args for c in stub.guard.expect.call_args_list], [("Communication", "c1"), ("Communication", "c2")])
		self.assertIn("deleted on the old site", stub.excluded[0]["reason"])  # c3: its ticket is gone

	def test_core_comments_take_helpdesks_copy_and_are_not_doubled(self):
		stub = self.stub()
		helpdesk_load.comments(stub, {"5"})
		comments = stub.values("Comment")
		self.assertEqual(sorted(comments), ["a1", "k1"])
		self.assertEqual(comments["k1"]["comment_by"], "Agent One")
		self.assertEqual(comments["k1"]["is_pinned"], 1)
		historical.mark.assert_called_once_with(stub.snapshot, "Comment", "k1", "Comment", "k1", "run")
		self.assertEqual([e["source_name"] for e in stub.excluded], ["d1"])
		stub.guard.expect.assert_not_called()  # Comment is not an outbox table

	def test_an_older_helpdesk_keeps_hd_ticket_comment(self):
		self.meta.has_field.return_value = False
		stub = self.stub()
		helpdesk_load.comments(stub, {"5"})
		self.assertEqual(stub.values("HD Ticket Comment")["k1"]["reference_ticket"], "5")
		historical.mark.assert_called_once_with(stub.snapshot, "Comment", "k1", "HD Ticket Comment", "k1", "run")

	def test_without_helpdesks_copy_the_comment_is_built_as_helpdesk_would(self):
		rows = {**self.ROWS, "Comment": []}
		stub = Stub(rows, users={"agent@x.org"})
		stub.targets[("HD Ticket", "5")] = "5"
		self.db.get_value.return_value = "Agent One"
		helpdesk_load.comments(stub, {"5"})
		self.assertEqual(stub.values("Comment")["k1"], {"comment_type": "Comment", "comment_email": "agent@x.org",
			"comment_by": "Agent One", "content": "note", "is_pinned": 1, "docstatus": 0,
			"reference_doctype": "HD Ticket", "reference_name": "5"})

	def test_an_assignment_to_an_account_not_here_is_reported(self):
		stub = self.stub()
		helpdesk_load.todos(stub, {"5"}, stub.helpdesk_drops)
		self.assertEqual(list(stub.values("ToDo")), ["t1"])
		self.assertIn("vendor@x.org", stub.failures[0]["error"])
		stub.guard.expect.assert_called_once_with("ToDo", "t1")

	def test_versions_follow_their_ticket_with_the_old_author(self):
		stub = self.stub()
		helpdesk_load.versions(stub, {"5"})
		[(name, values)] = stub.values("Version").items()
		self.assertEqual((values["ref_doctype"], values["docname"], values["data"]), ("HD Ticket", "5", "{}"))
		self.db.set_value.assert_called_once_with("Version", name, "owner", "agent@x.org", update_modified=False)


class TestMasters(HelpdeskCase):
	def test_a_seeded_record_is_linked_unchanged_and_differences_reported(self):
		stub = Stub({"HD Ticket Status": [{"name": "Open", "label_agent": "Open", "color": "Red"}]})
		self.db.exists.return_value = "Open"
		with patch.object(frappe, "get_doc", return_value=frappe._dict(label_agent="Open", color="Blue")):
			helpdesk_load.masters(stub)
		self.assertEqual(stub.inserted, [])
		historical.mark.assert_called_once_with(stub.snapshot, "HD Ticket Status", "Open", "HD Ticket Status", "Open", "run")
		self.assertIn("differs in color", stub.warnings[0]["warning"])

	def test_numbers_captured_as_text_are_not_differences(self):
		self.assertTrue(helpdesk_load._same("0.40", 0.4))
		self.assertTrue(helpdesk_load._same(None, 0))
		self.assertFalse(helpdesk_load._same("Red", "Blue"))

	def test_the_old_sla_comes_in_switched_off(self):
		stub = Stub({"HD Service Level Agreement": [{"name": "Default", "service_level": "Default", "enabled": 1, "default_sla": 1}]})
		self.meta.fields = []
		helpdesk_load.masters(stub)
		sla = stub.values("HD Service Level Agreement")["Default"]
		self.assertEqual((sla["enabled"], sla["default_sla"]), (0, 0))

	def test_agents_come_in_switched_off_and_need_their_account(self):
		stub = Stub({"HD Agent": [{"name": "agent@x.org", "user": "agent@x.org", "is_active": 1},
			{"name": "vendor@x.org", "user": "vendor@x.org", "is_active": 1}]}, users={"agent@x.org"})
		helpdesk_load.masters(stub)
		self.assertEqual(stub.values("HD Agent")["agent@x.org"]["is_active"], 0)
		self.assertIn("vendor@x.org", stub.failures[0]["error"])

	def test_teams_keep_members_who_are_here_and_no_assignment_rule(self):
		rows = {"HD Team": [{"name": "KRCS-DT", "team_name": "KRCS-DT", "assignment_rule": "KRCS-DT - Support Rotation"},
			{"name": "NSD", "team_name": "NSD"}],
			"HD Team Member": [{"name": 1, "parent": "KRCS-DT", "parenttype": "HD Team", "parentfield": "users", "user": "agent@x.org"},
				{"name": 2, "parent": "KRCS-DT", "parenttype": "HD Team", "parentfield": "users", "user": "gone@x.org"},
				{"name": 3, "parent": "NSD", "parenttype": "HD Team", "parentfield": "users", "user": "gone@x.org"}]}
		stub = Stub(rows, users={"agent@x.org"})
		helpdesk_load.masters(stub)
		team = stub.values("HD Team")["KRCS-DT"]
		self.assertEqual(team["users"], [{"user": "agent@x.org"}])
		self.assertNotIn("assignment_rule", team)
		self.assertIn("No member of HD Team NSD", stub.failures[0]["error"])


class TestPhase(HelpdeskCase):
	def test_a_capture_taken_before_exporter_0_5_0_is_named(self):
		stub = Stub({})
		stub.snapshot["source"]["continuity"] = {"tables": {"tabHD Ticket": {"disposition": "source_backup_required"}}}
		with self.assertRaisesRegex(ValueError, "exporter on the old site to 0.5.0"):
			helpdesk_load.run(stub)

	def test_no_tickets_and_no_helpdesk_is_nothing_to_do(self):
		helpdesk_load.run(Stub({}))

	def test_helpdesk_must_be_installed(self):
		with patch.object(frappe, "get_installed_apps", return_value=["frappe"]):
			with self.assertRaisesRegex(ValueError, "Install Helpdesk"):
				helpdesk_load.run(Stub({"HD Ticket": [{"name": "5"}]}))


class TestFilesAndInsert(TestCase):
	def test_restored_ticket_files_add_no_new_attachment_comment(self):
		from frappe.core.doctype.file.file import File

		calls = []
		with patch.object(File, "create_attachment_record", lambda self: calls.append(self.attached_to_doctype)):
			with helpdesk_load.quiet_attachment_comments():
				for dt in ("HD Ticket", "Communication", "Buzz Event"):
					File.create_attachment_record(SimpleNamespace(attached_to_doctype=dt))
		self.assertEqual(calls, ["Buzz Event"])

	def test_ticket_files_are_private(self):
		from onerc_vmms_migrate.file_transfer import required_privacy

		self.assertTrue(required_privacy("HD Ticket", False))
		self.assertTrue(required_privacy("Communication", False))

	def test_an_integer_keyed_child_row_takes_its_sequence(self):
		child = SimpleNamespace(name=None, meta=SimpleNamespace(autoname="autoincrement"), doctype="HD Team Member",
			db_insert=MagicMock(), get_invalid_links=lambda: ([], []))
		doc = MagicMock(name="team")
		doc.get_all_children.return_value = [child]
		doc.get_invalid_links.return_value = ([], [])
		with (
			patch.object(frappe, "new_doc", return_value=doc),
			patch.object(frappe, "db", MagicMock(**{"exists.return_value": False, "get_next_sequence_val.return_value": 41})),
		):
			historical.insert("HD Team", {"team_name": "NSD", "creation": "2026-01-01"}, name="NSD")
		self.assertEqual(child.name, 41)
