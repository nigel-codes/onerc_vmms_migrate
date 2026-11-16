"""Post-import repairs: the rules that need no database. Behaviour on records is
rehearsed against a migrated clone, inside a transaction that is rolled back."""

import os
import re
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate import post_import_repairs as repairs


class TestIdentityPatterns(TestCase):
	def test_only_seven_or_eight_digits_are_national_ids(self):
		self.assertEqual(repairs.id_pattern("1234567"), repairs.NATIONAL_ID_PATTERN)
		self.assertEqual(repairs.id_pattern("30960975"), repairs.NATIONAL_ID_PATTERN)
		for number, kind in (
			("829944", "6 digits"),
			("123456789", "9+ digits"),
			("35581360.0", "7-8 digits ending in .0 (spreadsheet)"),
			("3558 1360", "7-8 digits with spaces or dashes"),
			("AK0156779", "letters then digits (passport-like)"),
			("CK40099", "other letters and digits"),
			("n/a?", "other"),
		):
			with self.subTest(number=number):
				self.assertEqual(repairs.id_pattern(number), kind)


class TestApplyRequest(TestCase):
	def setUp(self):
		patches = (
			patch.object(repairs, "_assert_people_finished"),
			patch.object(repairs, "_previewed", return_value=True),
			patch.object(frappe, "throw", side_effect=frappe.ValidationError),
		)
		for item in patches:
			item.start()
			self.addCleanup(item.stop)

	def test_runs_in_the_agreed_order_whatever_was_ticked_first(self):
		chosen = repairs.validate_request(["life_memberships", "system_manager"], "REPAIR", True)
		self.assertEqual(chosen, ["system_manager", "life_memberships"])

	def test_refuses_without_confirmation_backup_or_a_choice(self):
		for args in (
			(["system_manager"], "repair", True),
			(["system_manager"], "REPAIR", False),
			([], "REPAIR", True),
			(["drop_everything"], "REPAIR", True),
		):
			with self.subTest(args=args), self.assertRaises(frappe.ValidationError):
				repairs.validate_request(*args)

	def test_refuses_before_a_preview(self):
		with patch.object(repairs, "_previewed", return_value=False), self.assertRaises(frappe.ValidationError):
			repairs.validate_request(["system_manager"], "REPAIR", True)


class TestRepairList(TestCase):
	def test_every_repair_plans_and_applies(self):
		self.assertEqual(tuple(repairs.PLANS), repairs.ORDER)
		self.assertEqual(tuple(repairs.APPLY), repairs.ORDER)

	def test_the_settings_dialog_offers_the_same_repairs_in_the_same_order(self):
		form = os.path.join(os.path.dirname(repairs.__file__), "onerc_vmms_migrate", "doctype",
			"vmms_migration_settings", "vmms_migration_settings.js")
		with open(form) as handle:
			block = re.search(r"const repair_choices = \[(.*?)\n\];", handle.read(), re.S).group(1)
		offered = re.findall(r"\['(\w+)', '([^']+)'\]", block)
		self.assertEqual(offered, list(repairs.REPAIRS.items()))


class TestImportedDecisionsMayMove(TestCase):
	"""Routing re-saves applications that carry an old decision with no approver,
	stage or date. The import's own exception must let that save through, and
	only for a row that is marked imported and has a migration record."""

	def row(self, marked=1, recorded=True, flag=False):
		from onerc_vmms_migrate.import_review import ImportedDecisionMixin

		class Base:
			def _get_missing_mandatory_fields(self):
				return [("stage", ""), ("approver", ""), ("decided_on", ""), ("decision", "")]

		class Row(ImportedDecisionMixin, Base):
			doctype, name, parenttype, parent = "VMMS Approval Decision", "row-1", "VMMS Volunteer Application", "VAPP-1"

			def get(self, field):
				return marked if field == "legacy_source_decision" else None

			def is_new(self):
				return False

		patches = (
			patch.object(frappe, "db", MagicMock(get_value=MagicMock(return_value=marked), exists=MagicMock(return_value=recorded))),
			patch.object(frappe, "flags", frappe._dict(vmms_migration_import=flag)),
		)
		for item in patches:
			item.start()
			self.addCleanup(item.stop)
		return Row()

	def missing(self, **kwargs):
		return [field for field, _ in self.row(**kwargs)._get_missing_mandatory_fields()]

	def test_a_recorded_imported_decision_saves_without_approver_stage_or_date(self):
		self.assertEqual(self.missing(), ["decision"])

	def test_an_unmarked_or_unrecorded_decision_is_still_held_to_the_rules(self):
		self.assertEqual(self.missing(marked=0), ["stage", "approver", "decided_on", "decision"])
		self.assertEqual(self.missing(recorded=False), ["stage", "approver", "decided_on", "decision"])


class TestEventBookings(TestCase):
	"""A booking imported before its person's account existed is linked once
	the account is there, by the old booking's user or its primary contact."""

	def plan(self, unclaimed, payloads, imported=(), existing=()):
		import json

		meta = MagicMock()
		meta.has_field.return_value = True

		def sql(query, values=None, *args, **kwargs):
			if "Legacy VMMS Document" in query:
				return [(name, json.dumps({"doctype": "Event Booking", "record": record})) for name, record in payloads.items()]
			return list(imported)

		db = MagicMock(sql=MagicMock(side_effect=sql))
		db.exists.side_effect = lambda doctype, name=None: doctype == "DocType" or name in existing
		with (
			patch.object(frappe, "db", db),
			patch.object(frappe, "get_meta", return_value=meta),
			patch.object(frappe, "get_all", return_value=list(unclaimed)),
		):
			return repairs.plan_event_bookings()

	def test_links_by_user_then_by_primary_contact_and_leaves_the_rest(self):
		plan = self.plan(
			["B1", "B2", "B3", "B4", "B5"],
			{
				"B1": {"user": "old@x", "primary_contact": "other@x"},
				"B2": {"user": None, "primary_contact": " Here@Example.org "},
				"B3": {"user": "Administrator", "primary_contact": "missingemail7@vmms.com"},
				"B4": {"user": None, "primary_contact": "gone@example.org"},
			},
			imported=[("old@x", "new@x")],
			existing={"here@example.org"},
		)
		self.assertEqual(plan["todo"], [{"booking": "B1", "user": "new@x"}, {"booking": "B2", "user": "here@example.org"}])
		self.assertEqual(plan["summary"]["placeholder address"], 1)
		self.assertEqual(plan["summary"]["no account yet"], 1)
		self.assertEqual(plan["summary"]["no archived old booking"], 1)

	def test_apply_writes_the_account_without_hooks_and_skips_a_claimed_booking(self):
		db = MagicMock()
		db.get_value.side_effect = lambda doctype, name, field: "someone@x" if name == "B2" else None
		with patch.object(frappe, "db", db):
			done = repairs.apply_event_bookings(
				{"todo": [{"booking": "B1", "user": "new@x"}, {"booking": "B2", "user": "here@x"}]}, lambda *a: None, MagicMock())
		self.assertEqual(done, {"bookings_linked": 1})
		db.set_value.assert_called_once_with("Event Booking", "B1", {"user": "new@x", "legacy_unclaimed_booking": 0}, update_modified=False)


class TestHeldMail(TestCase):
	def test_plan_lists_every_waiting_email_with_its_recipients(self):
		rows = [
			frappe._dict(name="Q1", creation="2026-11-14 10:00:00", status="Not Sent", reference_doctype="VMMS Volunteer Application", reference_name="A1", sender=None),
			frappe._dict(name="Q2", creation="2026-11-15 10:00:00", status="Partially Sent", reference_doctype=None, reference_name=None, sender=None),
		]
		db = MagicMock(sql=MagicMock(return_value=[("Q1", "cc@redcross.or.ke"), ("Q2", "a@gmail.com"), ("Q2", "b@gmail.com")]))
		with patch.object(frappe, "get_all", return_value=rows) as get_all, patch.object(frappe, "db", db):
			plan = repairs.plan_held_mail()
		self.assertEqual(get_all.call_args.kwargs["filters"], {"status": ["in", repairs.HELD_STATUSES]})
		self.assertEqual(plan["todo"], ["Q1", "Q2"])
		self.assertEqual(plan["summary"]["recipients"], 3)
		self.assertEqual(plan["summary"]["recipient_domains"], {"gmail.com": 2, "redcross.or.ke": 1})

	def test_apply_sets_the_mail_aside_without_deleting_it(self):
		db = MagicMock()
		with patch.object(frappe, "db", db):
			done = repairs.apply_held_mail({"todo": ["Q1", "Q2"]}, lambda *a: None, MagicMock())
		self.assertEqual(done, {"set_aside": 2})
		query, values = db.sql.call_args.args
		self.assertIn("set status = 'Error'", query)
		self.assertIn("status in %s", query)  # one sent since the plan stays sent
		self.assertEqual(values, (repairs.HELD_NOTE, ("Q1", "Q2"), repairs.HELD_STATUSES))
		db.delete.assert_not_called()


class TestApprovalRouting(TestCase):
	def test_only_imported_records_whose_queue_differs_are_moved(self):
		from vmmsx.approvals.services import assignment, config, engine

		with (
			patch.object(config, "governed_doctypes", return_value=["VMMS Volunteer Application"]),
			patch.object(frappe, "db", MagicMock(exists=MagicMock(return_value=True),
				sql=MagicMock(return_value=[("A1",), ("A2",), ("A3",)]))),
			patch.object(frappe, "get_all", return_value=["A1", "A2", "A3", "NEW-1"]),
			patch.object(frappe, "get_doc", side_effect=lambda doctype, name: name),
			patch.object(engine, "authorised", side_effect=lambda doc: {"approvers": [] if doc == "A3" else ["cc@x"]}),
			patch.object(assignment, "assignees", side_effect=lambda doctype, name: {"cc@x"} if name == "A2" else {"old@x"}),
			patch.object(repairs, "SlaReminders"),
		):
			plan = repairs.plan_approval_routing()
		self.assertEqual(plan["todo"], [{"doctype": "VMMS Volunteer Application", "record": "A1"}])
		self.assertEqual(plan["summary"]["VMMS Volunteer Application: already queued right"], 1)
		self.assertEqual(plan["summary"]["VMMS Volunteer Application: nobody may decide (left as it is)"], 1)

	def test_overdue_reminders_are_counted_for_notify_only_stages(self):
		from vmmsx.approvals.services import config, contract, routing, sla

		stages = {"review": frappe._dict(on_sla_breach=sla.BREACH_NOTIFY_ONLY), "regional": frappe._dict(on_sla_breach=sla.BREACH_WEEKLY_DIGEST)}
		due = {"A1": "now", "A2": "week", "A3": "later", "A4": "now"}
		with (
			patch.object(config, "for_doctype"),
			patch.object(config, "stage_anchor"),
			patch.object(contract, "stage", side_effect=lambda doc: "regional" if doc.name == "A4" else "review"),
			patch.object(config, "stage_by_name", side_effect=lambda workflow, name: stages[name]),
			patch.object(routing, "routed", return_value=["cc1@x", "cc2@x"]),
			patch.object(frappe.utils.data, "get_system_timezone", return_value="Africa/Nairobi"),
		):
			counter = repairs.SlaReminders()
			with patch.object(sla, "is_breached", side_effect=lambda doc, stage, when: due[doc.name] == "now" or (due[doc.name] == "week" and when == counter.week)):
				for name in due:
					counter.count(frappe._dict(doctype="VMMS Volunteer Application", name=name))
		self.assertEqual(counter.summary(), {"overdue_now": 1, "bells_a_day_now": 2, "overdue_within_a_week": 1, "reviewers_reminded": 2})

	def test_apply_moves_them_with_the_assignment_notice_off(self):
		from frappe.desk.form import assign_to
		from vmmsx.approvals.services import repair

		seen = []

		def resync(doctype, name):
			seen.append(assign_to.notify_assignment("a", "b"))
			return True

		original = assign_to.notify_assignment
		with patch.object(repair, "_resync_one", side_effect=resync), patch.object(frappe, "db", MagicMock()):
			done = repairs.apply_approval_routing({"todo": [{"doctype": "D", "record": "R"}]}, lambda *a: None, MagicMock())
		self.assertEqual(done, {"moved": 1})
		self.assertEqual(seen, [None])
		self.assertIs(assign_to.notify_assignment, original)


class TestReviewerNotes(TestCase):
	"""The old reviewers' comments come across as the same comments, on the
	record each application or membership became, and only once."""

	def notes(self):
		return [
			{"name": "c1", "comment_type": "Comment", "reference_doctype": "Job Applicant", "reference_name": "a@x.org",
			 "content": "<div><p>Kindly attach your documents</p></div>", "owner": "old@x.org", "comment_email": "old@x.org",
			 "comment_by": "Faith K", "creation": "2026-07-02 14:25:06", "modified": "2026-07-02 14:25:06"},
			{"name": "c2", "comment_type": "Comment", "reference_doctype": "VM Membership", "reference_name": "VM-MSH-1",
			 "content": "Kindly renew", "owner": "gone@x.org", "creation": "2026-07-28 20:42:08"},
			{"name": "c3", "comment_type": "Comment", "reference_doctype": "Job Applicant", "reference_name": "job@x.org", "content": "x"},
			{"name": "c4", "comment_type": "Info", "reference_doctype": "Job Applicant", "reference_name": "a@x.org", "content": "auto"},
			{"name": "c5", "comment_type": "Comment", "reference_doctype": "Job Applicant", "reference_name": "a@x.org", "content": "again"},
		]

	def test_plan_matches_each_comment_to_its_new_record(self):
		from contextlib import contextmanager

		@contextmanager
		def capture():
			yield lambda doctype: iter(self.notes()) if doctype == "Comment" else iter(())

		def sql(query, values=None, *args, **kwargs):
			if "Legacy VMMS Document" in query and values == ("Job Applicant", repairs.APPLICATION):
				return [("a@x.org", "VAPP-1")]
			if "Legacy Migration Record" in query and values == ("VM Membership", "VMMS Membership"):
				return [("VM-MSH-1", "MSH-1")]
			if "from tabComment" in query:
				return [("c5",)]
			return []

		db = MagicMock(sql=MagicMock(side_effect=sql), exists=MagicMock(return_value=True), get_value=MagicMock(return_value="In Review"))
		with patch.object(repairs, "_capture", capture), patch.object(frappe, "db", db):
			plan = repairs.plan_reviewer_notes()
		self.assertEqual([(item["note"]["name"], item["target"]) for item in plan["todo"]],
			[("c1", [repairs.APPLICATION, "VAPP-1"]), ("c2", ["VMMS Membership", "MSH-1"])])
		self.assertEqual(plan["summary"]["old_comments"], 4)  # c4 is not a person's comment
		self.assertEqual(plan["summary"]["already here"], 1)
		self.assertEqual(plan["summary"]["Job Applicant not imported"], 1)
		self.assertEqual(plan["details"]["examples"][0]["text"], "Kindly attach your documents")

	def test_apply_writes_each_comment_as_it_was_without_hooks(self):
		note = self.notes()[0]
		inserted = []

		class Comment(dict):
			def db_insert(self):
				inserted.append(dict(self))

		db = MagicMock(sql=MagicMock(return_value=[("old@x.org", "old@x.org")]))
		db.exists.side_effect = lambda doctype, name=None: doctype == "User"
		with patch.object(frappe, "db", db), patch.object(frappe, "get_doc", side_effect=lambda values: Comment(values)):
			done = repairs.apply_reviewer_notes({"todo": [{"note": note, "target": [repairs.APPLICATION, "VAPP-1"]}]},
				lambda *a: None, MagicMock())
		self.assertEqual(done, {"comments_added": 1})
		row = inserted[0]
		self.assertEqual((row["name"], row["comment_type"], row["reference_doctype"], row["reference_name"]),
			("c1", "Comment", repairs.APPLICATION, "VAPP-1"))
		self.assertEqual((row["owner"], row["creation"], row["content"]), ("old@x.org", note["creation"], note["content"]))
