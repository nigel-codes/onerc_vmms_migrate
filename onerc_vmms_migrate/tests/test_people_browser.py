import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch
import frappe
from onerc_vmms_migrate import browser, batch_browser, batches, workspace


class TestPeopleBrowser(TestCase):
	def setUp(self):
		p = patch.object(frappe, "session", frappe._dict(user="Administrator"))
		p.start()
		self.addCleanup(p.stop)

	def test_people_start_needs_no_rehearsal_freeze_or_test_destination(self):
		settings = frappe._dict(browser_capture="capture")
		with (
			patch.object(browser, "authorize"),
			patch.object(frappe, "get_single", return_value=settings),
			patch.object(batches, "checkpoint", return_value=None),
			patch.object(batch_browser, "assert_job_idle"),
			patch.object(batch_browser, "queue", return_value={"job_id": "job"}) as queue,
			patch.object(frappe, "cache", MagicMock()),
		):
			result = browser.start("people_import", confirmation="IMPORT PEOPLE", batch_size=50)
		self.assertEqual(result["job_id"], "job")
		self.assertEqual(queue.call_args.args[:1], ("people_import",))
		self.assertEqual(queue.call_args.args[2], 50)

	def test_resuming_people_preserves_its_scope_and_only_continues_automatically_when_requested(self):
		value = {
			"scope": "people",
			"mode": "import",
			"fingerprint": "same",
			"capture_id": "c",
			"status": "paused",
		}
		with (
			patch.object(batches, "checkpoint", return_value=value),
			patch.object(browser, "fingerprint", return_value="same"),
			patch.object(batches, "commit_checkpoint"),
			patch.object(batch_browser, "queue") as queue,
			patch.object(frappe, "cache", MagicMock()),
		):
			batch_browser.resume_locked(50, False, "token", auto_continue=True)
		self.assertEqual(queue.call_args.args[0], "people_import")
		self.assertTrue(value["auto_continue"])

	def test_review_settings_are_native_fields_and_override_config_in_all_paths(self):
		schema = json.loads(
			(
				Path(__file__).parents[1]
				/ "onerc_vmms_migrate/doctype/vmms_migration_settings/vmms_migration_settings.json"
			).read_text()
		)
		for field in ("import_with_review", "drop_invalid_phones"):
			self.assertEqual(next(f for f in schema["fields"] if f["fieldname"] == field)["default"], "1")
		settings = frappe._dict(browser_capture="c", import_with_review=1, drop_invalid_phones=1)
		with (
			patch.object(frappe, "conf", frappe._dict()),
			patch.object(frappe, "db", MagicMock()),
			patch.object(frappe, "get_single", return_value=settings),
		):
			conf = workspace.config()
		self.assertTrue(conf.import_with_review)
		self.assertTrue(conf.drop_invalid_phones)
		self.assertEqual(conf.residence_levels, ["sub_county", "county"])

	def test_first_batch_checks_hosted_file_capability_before_business_writes(self):
		source = MagicMock()
		source.data = {"schemas": {"Version": {}}, "history": {"present": {}}, "source": {"source_id": "S"}}
		with (
			patch.object(frappe, "db", MagicMock()) as db,
			patch.object(batches, "commit_checkpoint"),
			patch(
				"onerc_vmms_migrate.run.selftest",
				side_effect=AssertionError("No rehearsals or native self-tests"),
			) as test,
			patch(
				"onerc_vmms_migrate.file_transfer.assert_supported",
				side_effect=RuntimeError("Unsupported hosted File insert"),
			),
		):
			with self.assertRaisesRegex(RuntimeError, "Unsupported hosted File"):
				batch_browser.initialize("people_import", 50, {"preserve_passwords": True}, source)
		db.set_single_value.assert_not_called()
		test.assert_not_called()
