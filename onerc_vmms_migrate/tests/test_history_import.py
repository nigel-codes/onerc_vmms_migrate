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

	def test_a_coordinator_who_was_not_imported_falls_back_to_the_countys_one_coordinator(self):
		row = {"name": "PDR-1", "owner": "gone@x"}
		loader = bare_loader()
		with patch.object(Loader, "target", return_value=None), patch.object(Loader, "county_coordinators", return_value=["cc@x"]) as held:
			self.assertEqual(loader.coordinator("Deployment Request Tool", row, "GEO-9"), "cc@x")
		held.assert_called_once_with("GEO-9")
		self.assertIn("county coordinator cc@x recorded", loader.warnings[0]["warning"])
		self.assertIn("gone@x", loader.warnings[0]["warning"])

	def test_none_or_several_county_coordinators_leave_administrator_and_say_which(self):
		for held, words in (([], "no County Coordinator"), (["a@x", "b@x"], "2 County Coordinators")):
			loader = bare_loader()
			with self.subTest(held=held), patch.object(Loader, "target", return_value=None), \
					patch.object(Loader, "county_coordinators", return_value=held):
				self.assertEqual(loader.coordinator("Deployment Request Tool", {"name": "PDR-1", "owner": "gone@x"}, "GEO-9"), "Administrator")
				self.assertIn(words, loader.warnings[0]["warning"])
				self.assertIn("Administrator recorded", loader.warnings[0]["warning"])

	def test_a_tool_the_old_administrator_made_gets_the_county_coordinator_and_a_warning(self):
		# Rehearsal 16 Nov: 4 old tools were made by Administrator and silently
		# kept Administrator as the volunteers' contact, with no warning.
		row = {"name": "PDR-1", "owner": "Administrator"}
		loader = bare_loader()
		with patch.object(Loader, "target", return_value="Administrator"), \
				patch.object(Loader, "county_coordinators", return_value=["cc@x"]):
			self.assertEqual(loader.coordinator("Deployment Request Tool", row, "GEO-9"), "cc@x")
		self.assertIn("county coordinator cc@x recorded", loader.warnings[0]["warning"])
		loader = bare_loader()
		with patch.object(Loader, "target", return_value="Administrator"), \
				patch.object(Loader, "county_coordinators", return_value=[]):
			self.assertEqual(loader.coordinator("Deployment Request Tool", row, "GEO-9"), "Administrator")
		self.assertIn("Administrator recorded", loader.warnings[0]["warning"])
		self.assertNotIn("was not imported", loader.warnings[0]["warning"])

	def test_county_coordinators_are_the_enabled_holders_exactly_at_the_place(self):
		loader = bare_loader()
		with (
			patch("vmmsx.approvals.services.routing.holders_exactly_at", return_value=["b@x", "Administrator", "off@x", "a@x"]) as holders,
			patch.object(frappe, "db", MagicMock()) as db,
		):
			db.get_single_value.return_value = None
			db.get_value.side_effect = lambda doctype, name, field: name != "off@x"
			self.assertEqual(loader.county_coordinators("GEO-9"), ["a@x", "b@x"])
			self.assertEqual(loader.county_coordinators("GEO-9"), ["a@x", "b@x"])  # asked once per place
			self.assertEqual(loader.county_coordinators(None), [])
		holders.assert_called_once_with("GEO-9", "County Coordinator")

	def test_deployments_and_events_keep_their_old_creator(self):
		from onerc_vmms_migrate.history_load import OWNER_DOCTYPES

		self.assertTrue({"VMMS Deployment", "Buzz Event"} <= OWNER_DOCTYPES)

	def test_a_deployments_notes_are_the_old_notes_with_nothing_added(self):
		source = open(os.path.join(os.path.dirname(history_import.__file__), "history_load.py")).read()
		self.assertIn("'notes':r.get('notes')}", source)
		self.assertNotIn("DEPLOYMENT_SUFFIX", source)


class TestEventTimes(TestCase):
	def test_the_time_zone_label_is_derived_as_buzz_would_on_save(self):
		loader = bare_loader()
		values = {"time_zone": "Africa/Nairobi", "start_date": "2026-05-04", "start_time": "09:00:00", "end_time": "17:00:00"}
		loader.event_times({"name": 7}, values)
		self.assertEqual(values["time_zone_label"], "EAT")
		self.assertEqual(loader.warnings, [])

	def test_an_end_before_the_start_on_one_day_is_kept_and_reported(self):
		loader = bare_loader()
		values = {"start_date": "2026-05-04", "end_date": "2026-05-04", "start_time": "14:00:00", "end_time": "09:00:00"}
		loader.event_times({"name": 48}, values)
		self.assertEqual((values["start_time"], values["end_time"]), ("14:00:00", "09:00:00"))
		self.assertIn("not after start time", loader.warnings[0]["warning"])
		self.assertNotIn("time_zone_label", values)

	def test_an_event_over_several_days_may_end_earlier_in_the_day(self):
		loader = bare_loader()
		loader.event_times({"name": 9}, {"start_date": "2026-05-04", "end_date": "2026-05-06", "start_time": "14:00:00", "end_time": "09:00:00"})
		self.assertEqual(loader.warnings, [])


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

	events = applications = availability = time_logs = helpdesk = lambda self: None


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
			patch.object(history_import, "_assert_quiet_site"),
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


class TestQuietSite(TestCase):
	"""The import belongs inside the cutover: once jobs run again, their own
	messages would stop it part-way."""

	def setUp(self):
		throw = patch.object(frappe, "throw", side_effect=frappe.ValidationError)
		throw.start()
		self.addCleanup(throw.stop)

	def check(self, cutover, scheduler_off):
		with (
			patch.object(browser, "state", return_value={"cutover": cutover} if cutover else {}),
			patch("frappe.utils.scheduler.is_scheduler_disabled", return_value=scheduler_off),
		):
			history_import._assert_quiet_site()

	def test_refused_after_the_cutover_with_the_scheduler_running(self):
		with self.assertRaises(frappe.ValidationError):
			self.check(None, False)

	def test_allowed_during_the_cutover_or_with_the_scheduler_off(self):
		self.check({"scheduler_enabled": True, "mute_emails": 0}, False)
		self.check(None, True)


class TestEvidence(TestCase):
	def test_mpesa_records_are_archived_but_never_converted(self):
		snapshot = {"schemas": {"Buzz Event": {"fields": []}, "Mpesa Express Request": {"fields": []}}}
		rows = {"Buzz Event": [{"name": 1}], "Mpesa Express Request": [{"name": "MPESA-1"}, {"name": "MPESA-2"}]}
		history = MagicMock()
		history.rows.side_effect = lambda dt: rows.get(dt, [])
		with (
			patch("onerc_vmms_migrate.history_model.History", return_value=history),
			patch.object(history_import.historical, "archive", return_value={"created": 1, "reused": 0}) as archive,
			patch.object(frappe, "db", MagicMock()),
		):
			totals = history_import.archive(snapshot, "run", lambda: None, None)
		archived = [item["doctype"] for call in archive.call_args_list for item in call.kwargs["records"]]
		self.assertEqual(archived, ["Buzz Event", "Mpesa Express Request", "Mpesa Express Request"])
		self.assertEqual(totals["Mpesa Express Request"], 2)
		self.assertNotIn("Mpesa Express Request", history_import.CONVERTED)

	def test_evidence_that_will_not_archive_is_reported_and_the_history_goes_on(self):
		snapshot = {"schemas": {"Buzz Event": {"fields": []}, "Mpesa Express Request": {"fields": []}}}
		rows = {"Buzz Event": [{"name": 1}], "Mpesa Express Request": [{"name": "MPESA-1"}]}
		history = MagicMock()
		history.rows.side_effect = lambda dt: rows.get(dt, [])

		def archive(snapshot, run_id, records):
			if records[0]["doctype"] == "Mpesa Express Request":
				raise ValueError("Invalid historical payment amount.")
			return {"created": 1, "reused": 0}

		with (
			patch("onerc_vmms_migrate.history_model.History", return_value=history),
			patch.object(history_import.historical, "archive", side_effect=archive),
			patch.object(frappe, "db", MagicMock()),
		):
			totals = history_import.archive(snapshot, "run", lambda: None, None)
		self.assertEqual(totals["evidence_not_retained"], 1)
		self.assertIn("Invalid historical payment amount", totals["evidence_problems"][0])
		self.assertEqual(totals["created"], 1)


class TestEventReach(TestCase):
	def test_published_placed_banners_and_claimed_bookings_are_counted(self):
		rows = [
			frappe._dict(is_published=1, banner_image="/private/files/a.png", geo_node="G1", end_date="2099-01-01", start_date="2099-01-01"),
			frappe._dict(is_published=1, banner_image="/files/b.png", geo_node=None, end_date=None, start_date="2020-01-01"),
			frappe._dict(is_published=0, banner_image="/private/files/c.png", geo_node="G1", end_date=None, start_date=None),
		]
		meta = MagicMock()
		meta.has_field.return_value = True
		with (
			patch.object(frappe, "get_all", return_value=rows),
			patch.object(frappe, "get_meta", return_value=meta),
			patch.object(frappe, "db", MagicMock(count=MagicMock(return_value=3))),
			patch.object(frappe.utils, "getdate", return_value="2026-11-16"),
		):
			out = history_import.event_reach(["1", "2", "3"], ["B1", "B2", "B3", "B4", "B5"])
		self.assertEqual(out, {
			"events_published": 2, "events_unpublished": 1, "events_without_place": 1,
			"published_with_private_banner": 1, "published_still_running": 1,
			"bookings_claimed": 3, "bookings_unclaimed": 2,
		})
		self.assertEqual(history_import.event_reach([], []), {})


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


class TestFileScope(TestCase):
	def test_only_the_history_records_files_are_copied_never_an_applicants(self):
		snapshot = {
			"schemas": {"Job Applicant": {"fields": []}, "Buzz Event": {"fields": []}},
			"all_file_manifest": {
				"/files/cv.pdf": {"references": [{"attached_to_doctype": "Job Applicant", "attached_to_name": "a"}]},
				"/files/banner.png": {"references": [
					{"attached_to_doctype": "Buzz Event", "attached_to_name": "e"},
					{"attached_to_doctype": "Job Applicant", "attached_to_name": "a"},
				]},
			},
		}
		history = MagicMock()
		history.rows.side_effect = lambda dt: [{"name": dt[0].lower()}]
		with (
			patch("onerc_vmms_migrate.history_model.History", return_value=history),
			patch("onerc_vmms_migrate.full_run.attachments", return_value={}) as attachments,
		):
			history_import.files(snapshot, MagicMock(), "run", live=True)
		part = attachments.call_args.args[0]
		self.assertEqual(list(part["all_file_manifest"]), ["/files/banner.png"])
		self.assertEqual(
			part["all_file_manifest"]["/files/banner.png"]["references"],
			[{"attached_to_doctype": "Buzz Event", "attached_to_name": "e"}],
		)
		self.assertEqual({row["doctype"] for row in part["history"].values()}, {"Buzz Event"})
		self.assertEqual(part["users"], {})


class TestTorAttachmentRewrite(TestCase):
	"""Rehearsal 16 Nov: the second Import history rewrote all 66 tool TOR
	attachments with the same values and reported them as updated."""

	def test_a_first_import_fills_both_fields(self):
		from onerc_vmms_migrate.full_run import tor_attachment_values
		self.assertEqual(tor_attachment_values("/private/files/new.pdf", {"legacy_terms_attachment": "/private/files/old.pdf", "tor_attachment": None}),
			{"legacy_terms_attachment": "/private/files/new.pdf", "tor_attachment": "/private/files/new.pdf"})

	def test_a_second_import_writes_nothing(self):
		from onerc_vmms_migrate.full_run import tor_attachment_values
		self.assertEqual(tor_attachment_values("/private/files/new.pdf", {"legacy_terms_attachment": "/private/files/new.pdf", "tor_attachment": "/private/files/new.pdf"}), {})

	def test_a_document_staff_chose_is_kept(self):
		from onerc_vmms_migrate.full_run import tor_attachment_values
		self.assertEqual(tor_attachment_values("/private/files/new.pdf", {"legacy_terms_attachment": "/private/files/new.pdf", "tor_attachment": "/private/files/staff.pdf"}), {})
		self.assertEqual(tor_attachment_values("/private/files/new.pdf", {"legacy_terms_attachment": None}), {"legacy_terms_attachment": "/private/files/new.pdf"})


class TestImportedPeopleAreNeverHeldBack(TestCase):
	def test_a_person_in_the_ledger_leaves_the_exclusion_set(self):
		snapshot = {"excluded_native_identities": {("Employee", "VOL-1"), ("Employee", "VOL-2"), ("User", "a@x.org")}}
		ledger = [frappe._dict(source_doctype="Employee", source_name="VOL-1"), frappe._dict(source_doctype="User", source_name="a@x.org")]
		with patch.object(frappe, "get_all", return_value=ledger):
			history_import.keep_imported_people(snapshot)
		self.assertEqual(snapshot["excluded_native_identities"], {("Employee", "VOL-2")})
