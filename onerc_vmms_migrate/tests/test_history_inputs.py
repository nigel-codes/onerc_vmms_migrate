"""The history import reads a small copy of the capture after the first run
(0.10.2): the same held-back people and manifest, without the people data."""

import os
import tempfile
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate import browser, history_import, history_inputs


def full_snapshot():
	item = {"source_url": "/files/a.pdf", "status": "ready", "sha256": "x", "size": 1}
	missing = {"source_url": "/files/b.pdf", "status": "missing"}
	return {
		"source": {"source_id": "s1", "capture_id": "c1"},
		"schemas": {"User": {}, "Personnel Deployment Request": {}},
		"history": object(),
		"history_storage": {"count": 3, "sqlite_sha256": "h"},
		"users": {"a@x.org": {"name": "a@x.org", "bio": "long"}},
		"employees": {"VOL-1": {"name": "VOL-1"}, "VOL-2": {"name": "VOL-2"}},
		"applicants": {"APP-1": {}},
		"files": {"F1": {}},
		"password_hashes": {"a@x.org": "secret"},
		"_people_original": {"users": {}},
		"all_file_manifest": {"/files/a.pdf": item, "/files/b.pdf": missing},
		"captured_file_manifest": {"/files/a.pdf": dict(item)},
		"captured_files": {"/files/a.pdf": "captures/c1/files/a"},
		"capture_complete": False,
		"excluded_native_identities": {("Employee", "VOL-2"), ("User", "b@x.org")},
	}


class TestSmallInputs(TestCase):
	def setUp(self):
		self.root = tempfile.TemporaryDirectory()
		self.addCleanup(self.root.cleanup)

		def path(*parts):
			full = os.path.join(self.root.name, *parts)
			os.makedirs(os.path.dirname(full), exist_ok=True)
			return full

		self.key = "k1"
		for item in (
			patch.object(history_inputs.workspace, "path", side_effect=path),
			patch.object(history_inputs, "key", side_effect=lambda: self.key),
		):
			item.start()
			self.addCleanup(item.stop)
		self.conf = {"browser_capture": "c1"}

	def test_the_held_back_people_and_the_manifest_come_back_the_same(self):
		original = full_snapshot()
		history_inputs.save(self.conf, original)
		small = history_inputs.load(self.conf)
		self.assertEqual(small["excluded_native_identities"], original["excluded_native_identities"])
		self.assertEqual(small["captured_file_manifest"], original["captured_file_manifest"])
		self.assertEqual(small["all_file_manifest"], original["all_file_manifest"])
		self.assertEqual(small["captured_files"], original["captured_files"])
		self.assertEqual(small["source"], original["source"])
		self.assertEqual(small["schemas"], original["schemas"])
		self.assertEqual(small["history_storage"], original["history_storage"])
		self.assertIs(small["capture_complete"], False)

	def test_accounts_and_volunteers_are_kept_by_name_only(self):
		history_inputs.save(self.conf, full_snapshot())
		small = history_inputs.load(self.conf)
		self.assertEqual(small["users"], {"a@x.org"})
		self.assertEqual(small["employees"], {"VOL-1", "VOL-2"})
		self.assertIn("VOL-1", small["employees"])  # the only use the history makes of them

	def test_people_data_and_login_hashes_are_left_out(self):
		history_inputs.save(self.conf, full_snapshot())
		small = history_inputs.load(self.conf)
		for name in ("applicants", "files", "password_hashes", "_people_original", "history"):
			self.assertNotIn(name, small)
		with open(history_inputs.path("c1")) as handle:
			self.assertNotIn("secret", handle.read())
		self.assertTrue(small["_people_enriched"])  # extract() does not enrich them again

	def test_a_changed_setting_ledger_or_capture_means_working_it_out_again(self):
		history_inputs.save(self.conf, full_snapshot())
		self.key = "k2"
		self.assertIsNone(history_inputs.load(self.conf))
		self.key = "k1"
		self.assertIsNone(history_inputs.load({"browser_capture": "c2"}))
		self.assertIsNotNone(history_inputs.load(self.conf))

	def test_a_captured_manifest_that_differs_is_kept_as_it_is(self):
		original = full_snapshot()
		original["captured_file_manifest"]["/files/a.pdf"]["size"] = 2
		history_inputs.save(self.conf, original)
		self.assertEqual(history_inputs.load(self.conf)["captured_file_manifest"], original["captured_file_manifest"])

	def test_only_a_bulk_capture_with_its_accounts_history_can_be_kept_small(self):
		self.assertTrue(history_inputs.usable(full_snapshot()))
		inline = full_snapshot()
		inline.pop("history_storage")
		self.assertFalse(history_inputs.usable(inline))
		older = full_snapshot()
		older["schemas"].pop("User")  # its accounts are archived from `users`
		self.assertFalse(history_inputs.usable(older))


class TestRunReadsTheSmallInputs(TestCase):
	"""The first run works the held-back people out and stops; the next run
	reads the small inputs and never builds the people again."""

	def setUp(self):
		for item in (
			patch.object(history_import.workspace, "config", return_value={"browser_capture": "c1"}),
			patch("onerc_vmms_migrate.continuity.require_capture"),
			patch.object(history_import, "keep_imported_people"),
			patch("onerc_vmms_migrate.silence.silenced", MagicMock()),
		):
			item.start()
			self.addCleanup(item.stop)

	def test_without_them_the_run_saves_them_and_stops_before_importing(self):
		snapshot = full_snapshot()
		source = MagicMock()
		source.__enter__.return_value = source
		with (
			patch.object(history_inputs, "load", return_value=None),
			patch.object(history_inputs, "save") as save,
			patch("onerc_vmms_migrate.source.Source", return_value=source),
			patch("onerc_vmms_migrate.extract.extract", return_value=snapshot),
			patch("onerc_vmms_migrate.batches.build", return_value={}) as build,
			patch("onerc_vmms_migrate.batches.excluded_identities"),
			patch.object(history_import, "archive") as archive,
		):
			result = history_import._run("history-import", True, None)
		build.assert_called_once()
		save.assert_called_once()
		archive.assert_not_called()
		self.assertEqual(result, {"prepared": True, "held_back": 2})

	def test_with_them_the_people_are_not_built_again(self):
		snapshot = full_snapshot()
		source = MagicMock()
		source.__enter__.return_value = source
		with (
			patch.object(history_inputs, "load", return_value=snapshot),
			patch.object(history_import, "SnapshotSource", return_value=source) as small_source,
			patch("onerc_vmms_migrate.extract.extract", return_value=snapshot),
			patch("onerc_vmms_migrate.batches.build") as build,
			patch.object(history_import.workspace, "report_dir", return_value="/tmp/unused"),
			patch("onerc_vmms_migrate.silence.Guard"),
			patch.object(history_import, "archive", side_effect=RuntimeError("importing")),
		):
			with self.assertRaisesRegex(RuntimeError, "importing"):
				history_import._run("history-import", True, None)
		small_source.assert_called_once_with({"browser_capture": "c1"}, snapshot)
		build.assert_not_called()


class TestStartsAfresh(TestCase):
	"""A run that saved the small inputs is queued again as a new job."""

	def setUp(self):
		for item in (
			patch.object(browser, "authorize"),
			patch.object(browser, "state", return_value={}),
			patch.object(browser, "save_state"),
			patch.object(browser, "_progress_reporter", return_value=None),
			patch.object(frappe, "set_user"),
			patch.object(browser.frappe, "cache", MagicMock(get=MagicMock(return_value=b"t1"))),
		):
			item.start()
			self.addCleanup(item.stop)

	def test_a_prepared_run_queues_the_same_action_again(self):
		with patch.object(history_import, "apply", return_value={"prepared": True}), patch.object(browser, "_continue") as again:
			browser.execute("t1", "history_import", "Administrator")
		again.assert_called_once_with("history_import", "Administrator")

	def test_a_job_queued_that_way_is_never_queued_again(self):
		with (
			patch.object(browser, "state", return_value={"continued": True}),
			patch.object(history_import, "apply", return_value={"prepared": True}),
			patch.object(browser, "_continue") as again,
		):
			browser.execute("t1", "history_import", "Administrator")
		again.assert_not_called()

	def test_a_finished_or_failed_run_is_not_queued_again(self):
		with patch.object(history_import, "apply", return_value={"report": "x"}), patch.object(browser, "_continue") as again:
			browser.execute("t1", "history_import", "Administrator")
		with patch.object(history_import, "apply", side_effect=RuntimeError("stopped")), patch.object(browser, "_continue") as failed, patch.object(frappe.db, "rollback"):
			browser.execute("t1", "history_import", "Administrator")
		again.assert_not_called()
		failed.assert_not_called()
