"""History import after the people session: the rules that need no database.
The import itself is rehearsed against a migrated clone and rolled back."""

import json
import os
import re
import tempfile
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import add_days, getdate

from onerc_vmms_migrate import browser, history_import
from onerc_vmms_migrate.history_load import BatchBoundary, Loader


def bare_loader():
	loader = object.__new__(Loader)
	loader.conf, loader.warnings, loader.targets = {}, [], {}
	return loader


class TestDecidedStates(TestCase):
	"""Decisions of 15 November 2026: ended deployments are Completed, an ask
	left unanswered on one has Expired, and old terms come in switched off."""

	def setUp(self):
		zone = patch.object(frappe.utils.data, "get_system_timezone", return_value="Africa/Nairobi")
		zone.start()
		self.addCleanup(zone.stop)

	def test_a_deployment_stands_where_its_dates_put_it_today(self):
		today = getdate()
		status = Loader.deployment_status
		self.assertEqual(status(add_days(today, -30), add_days(today, -1)), "Completed")
		self.assertEqual(status(add_days(today, -3), add_days(today, 3)), "Active")
		self.assertEqual(status(f"{add_days(today, -3)} 09:00:00", f"{add_days(today, 3)} 17:00:00"), "Active")
		self.assertEqual(status(add_days(today, 2), add_days(today, 9)), "Planned")
		self.assertEqual(status(None, None), "Planned")

	def test_only_an_unanswered_ask_on_a_finished_deployment_expires(self):
		status = Loader.assignment_status
		self.assertEqual(status({"deployment_status": "Pending"}, "Completed"), "Expired")
		self.assertEqual(status({"deployment_status": "Pending"}, "Active"), "Pending")
		self.assertEqual(status({"deployment_status": "Pending"}, "Planned"), "Pending")
		self.assertEqual(status({"deployment_status": "Accepted"}, "Completed"), "Accepted")
		self.assertEqual(status({"deployment_status": "Rejected"}, "Completed"), "Declined")
		self.assertEqual(status({"deployment_status": "Pending", "docstatus": 2}, "Completed"), "Withdrawn")

	def test_old_terms_are_built_switched_off(self):
		source = open(os.path.join(os.path.dirname(history_import.__file__), "history_load.py")).read()
		self.assertIn("'is_active':0}", source)


class TestVocabularyKeptAsWritten(TestCase):
	def test_an_old_methodology_is_added_exactly_as_written_and_switched_off(self):
		loader = bare_loader()
		with patch.object(frappe, "db", MagicMock()) as db, patch.object(frappe, "get_doc") as get_doc:
			db.get_value.return_value = None
			db.exists.return_value = False
			name = loader.methodology(" Community engagement ", "TOR-1")
		self.assertEqual(name, "community_engagement")
		self.assertEqual(
			get_doc.call_args.args[0],
			{
				"doctype": "VMMS TOR Methodology",
				"methodology_key": "community_engagement",
				"methodology_name": "Community engagement",
				"is_active": 0,
			},
		)
		self.assertIn("Community engagement", loader.warnings[0]["warning"])

	def test_a_methodology_already_in_the_register_is_reused(self):
		with patch.object(frappe, "db", MagicMock()) as db, patch.object(frappe, "get_doc") as get_doc:
			db.get_value.return_value = "training"
			self.assertEqual(bare_loader().methodology("Training", "TOR-1"), "training")
			get_doc.assert_not_called()

	def test_a_key_taken_by_another_label_gets_a_distinct_key(self):
		with patch.object(frappe, "db", MagicMock()) as db, patch.object(frappe, "get_doc"):
			db.get_value.return_value = None
			db.exists.return_value = True
			name = bare_loader().methodology("Training!", "TOR-1")
		self.assertRegex(name, r"^training_[0-9a-f]{8}$")

	def test_missing_project_types_are_added_once_by_their_old_names(self):
		loader = bare_loader()
		with patch.object(frappe, "db", MagicMock()) as db, patch.object(frappe, "get_doc") as get_doc:
			db.exists.side_effect = lambda doctype, name: name == "Internal"
			loader.project_types([{"project_type": "health"}, {"project_type": "Internal"}, {"project_type": "health"}, {}])
		self.assertEqual([c.args[0] for c in get_doc.call_args_list], [{"doctype": "Project Type", "project_type": "health"}])

	def test_a_coordinator_who_was_not_imported_leaves_administrator_and_a_warning(self):
		loader = bare_loader()
		with patch.object(Loader, "target", return_value=None):
			self.assertEqual(loader.coordinator("Deployment Request Tool", {"name": "PDR-1", "owner": "gone@x"}), "Administrator")
		self.assertIn("gone@x", loader.warnings[0]["warning"])
		with patch.object(Loader, "target", return_value="here@x"):
			self.assertEqual(bare_loader().coordinator("Deployment Request Tool", {"name": "PDR-1", "owner": "here@x"}), "here@x")


class TestArchiveScope(TestCase):
	def test_the_converted_doctypes_bring_their_child_tables(self):
		snapshot = {
			"schemas": {
				"Personnel Terms of Reference": {"fields": [{"fieldtype": "Table", "options": "TOR Objectives"}]},
				"TOR Objectives": {"fields": []},
				"Timesheet": {"fields": [{"fieldtype": "Table", "options": "Timesheet Detail"}]},
				"Timesheet Detail": {"fields": []},
				"Employee": {"fields": [{"fieldtype": "Table", "options": "Employee Education"}]},
				"Employee Education": {"fields": []},
			}
		}
		doctypes = history_import.archived_doctypes(snapshot)
		self.assertIn("TOR Objectives", doctypes)
		self.assertEqual(doctypes.count("Timesheet Detail"), 1)
		self.assertNotIn("Employee", doctypes)
		self.assertNotIn("Employee Education", doctypes)


class FakeLoader:
	"""Two pages of projects, a terms adapter that is broken, deployments that fail once."""

	def __init__(self):
		self.naming = MagicMock()
		self.failures, self.calls = [], []
		self.projects_left = 2

	def projects(self):
		self.calls.append("projects")
		if self.projects_left:
			self.projects_left -= 1
			raise BatchBoundary()

	def terms(self):
		self.calls.append("terms")
		raise ValueError("Set the destination legal Company")

	def deployments(self):
		self.calls.append("deployments")
		if ("Deployment Request Tool", "PDR-1") not in self.skip_identities:
			self.failures.append({"source_doctype": "Deployment Request Tool", "source_name": "PDR-1", "error": "x"})

	events = applications = availability = time_logs = lambda self: None


class TestPaging(TestCase):
	def setUp(self):
		patches = (
			patch.object(history_import, "converted_index", return_value={}),
			patch.object(frappe, "db", MagicMock()),
			patch.object(frappe, "clear_messages"),
		)
		for item in patches:
			item.start()
			self.addCleanup(item.stop)

	def test_pages_until_done_and_a_broken_adapter_does_not_stop_the_rest(self):
		loader, commits = FakeLoader(), []
		broken = history_import.convert(loader, {}, True, lambda: commits.append(1), None)
		self.assertEqual(broken, {"terms": "Set the destination legal Company"})
		self.assertEqual(loader.calls.count("terms"), 1)  # not retried on later pages
		self.assertEqual(len(commits), 3)  # two boundaries, then the last page
		self.assertEqual(sum(f["source_name"] == "PDR-1" for f in loader.failures), 1)  # reported once
		self.assertFalse(loader.naming.dry_run)

	def test_a_preview_keeps_numbering_untouched(self):
		loader = FakeLoader()
		history_import.convert(loader, {}, False, lambda: None, None)
		self.assertTrue(loader.naming.dry_run)


class TestReports(TestCase):
	def test_each_record_is_reported_once_however_many_pages_met_it(self):
		rows = [{"source_doctype": "A", "source_name": 1}, {"source_doctype": "A", "source_name": "1"}, {"source_doctype": "B", "source_name": 1}]
		self.assertEqual(len(history_import._unique(rows)), 2)


class TestImportRequest(TestCase):
	def setUp(self):
		patches = (
			patch.object(history_import, "_assert_people_in"),
			patch.object(history_import.workspace, "config", return_value={"browser_capture": "c1"}),
			patch.object(frappe, "throw", side_effect=frappe.ValidationError),
		)
		for item in patches:
			item.start()
			self.addCleanup(item.stop)

	def test_refuses_without_the_word_a_backup_or_a_preview_of_this_capture(self):
		with patch.object(history_import, "_previewed", return_value=True):
			history_import.validate_request("HISTORY", True)
			for args in (("history", True), ("HISTORY", False), ("", True)):
				with self.subTest(args=args), self.assertRaises(frappe.ValidationError):
					history_import.validate_request(*args)
		with patch.object(history_import, "_previewed", return_value=False), self.assertRaises(frappe.ValidationError):
			history_import.validate_request("HISTORY", True)

	def test_a_preview_counts_only_for_the_capture_it_read(self):
		with tempfile.TemporaryDirectory() as root:
			reports = os.path.join(root, "reports")
			folder = os.path.join(reports, "20261115-120000-history-preview")
			os.makedirs(folder)
			with open(os.path.join(folder, "history-summary.json"), "w") as handle:
				json.dump({"capture": "c1"}, handle)
			with patch.object(history_import.workspace, "path", side_effect=lambda *parts: os.path.join(root, *parts)):
				self.assertTrue(history_import._previewed("c1"))
				self.assertFalse(history_import._previewed("c2"))
				self.assertFalse(history_import._previewed(""))


class TestControls(TestCase):
	def test_the_settings_form_offers_preview_and_import(self):
		self.assertIn("history_preview", browser.ACTIONS)
		self.assertIn("history_import", browser.ACTIONS)
		form = os.path.join(os.path.dirname(history_import.__file__), "onerc_vmms_migrate", "doctype",
			"vmms_migration_settings", "vmms_migration_settings.js")
		with open(form) as handle:
			source = handle.read()
		self.assertIn("add('Preview history', 'history_preview', 'History import')", source)
		self.assertTrue(re.search(r"start\('history_import', \{confirmation", source))


class TestReconcile(TestCase):
	def test_every_captured_record_is_converted_excluded_reported_or_left_alone(self):
		snapshot = {"source": {"source_id": "s"}}
		rows = {
			"Job Applicant": [
				{"name": "a", "is_volunteer": 1},
				{"name": "b", "is_volunteer": 1},
				{"name": "c", "is_volunteer": 0},
				{"name": "d", "is_volunteer": 1},
			],
			"Shift Type": [{"name": "Morning"}],
		}
		loader = MagicMock(
			excluded=[{"source_doctype": "Job Applicant", "source_name": "b"}],
			excluded_identities=set(),
			failures=[{"source_doctype": "Job Applicant", "source_name": "d", "error": "x"}],
		)
		history = MagicMock()
		history.rows.side_effect = lambda dt: rows.get(dt, [])
		with (
			patch("onerc_vmms_migrate.history_model.History", return_value=history),
			patch.object(history_import, "converted_index", return_value={("Job Applicant", "a"): "VAPP-1"}),
		):
			records = history_import.reconcile(snapshot, loader)
		self.assertEqual(
			records,
			{"Job Applicant": {"captured": 4, "converted": 1, "excluded": 1, "not_imported": 1, "left_alone": 1, "unaccounted": 0}},
		)

	def test_an_applicants_people_tables_stay_out_of_the_history_archive(self):
		snapshot = {"schemas": {
			"Job Applicant": {"fields": [{"fieldtype": "Table", "options": "Volunteer Language"}]},
			"Volunteer Language": {"fields": []},
		}}
		self.assertEqual(history_import.archived_doctypes(snapshot), ["Job Applicant"])
