import time
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate import batches, historical
from onerc_vmms_migrate.crosswalk import Crosswalks
from onerc_vmms_migrate.history_load import Loader, SourceIdentityExcluded
from onerc_vmms_migrate.history_model import History


class TestHistorySourceGaps(TestCase):
	def loader(self, enabled=True, source_present=False):
		loader = object.__new__(Loader)
		loader.conf = {"allow_missing_history_dependencies": enabled}
		loader.snapshot = {"users": {}, "history": {}}
		if source_present:
			loader.snapshot["history"]["tor"] = {
				"doctype": "Personnel Terms of Reference",
				"record": {"name": "TOR-1"},
			}
		loader.history = History(loader.snapshot)
		loader.targets, loader.by_source = {}, {}
		loader.excluded_identities = set()
		return loader

	def test_confirmed_absent_source_is_excluded_only_with_review_setting(self):
		with (
			patch.object(historical, "converted", return_value=None),
			patch.object(frappe, "db", MagicMock()) as db,
		):
			db.exists.return_value = False
			for doctype, name in [
				("User", "missing@example.test"),
				("Personnel Terms of Reference", "TOR-1"),
			]:
				with self.subTest(doctype=doctype), self.assertRaises(SourceIdentityExcluded):
					self.loader().target(doctype, name)
			with self.assertRaises(ValueError) as raised:
				self.loader(False).target("Personnel Terms of Reference", "TOR-1")
			self.assertNotIsInstance(raised.exception, SourceIdentityExcluded)

	def test_source_present_but_unconverted_still_blocks(self):
		with patch.object(historical, "converted", return_value=None):
			with self.assertRaises(ValueError) as raised:
				self.loader(source_present=True).target("Personnel Terms of Reference", "TOR-1")
		self.assertNotIsInstance(raised.exception, SourceIdentityExcluded)

	def test_optional_missing_source_needs_no_invented_link(self):
		with patch.object(historical, "converted", return_value=None):
			self.assertIsNone(self.loader().target("Personnel Terms of Reference", "TOR-1", required=False))

	def test_existing_destination_is_used_without_exclusion(self):
		with (
			patch.object(historical, "converted", return_value=None),
			patch.object(frappe, "db", MagicMock()) as db,
		):
			db.exists.return_value = True
			self.assertEqual(self.loader().target("User", "present@example.test"), "present@example.test")

	def test_people_already_reported_as_not_migrated_are_known_history_exclusions(self):
		snapshot = {"users": {}}
		batches.excluded_identities(
			snapshot,
			{"bundles": [], "not_migrated": [{"source_doctype": "Employee", "source_name": "VOL-excluded"}]},
		)
		self.assertIn(("Employee", "VOL-excluded"), snapshot["excluded_native_identities"])

	def test_excluded_history_is_recorded_and_propagates_to_dependents(self):
		loader = self.loader()
		loader.run_id = "run"
		loader.results, loader.excluded, loader.failures = [], [], []

		def build(row):
			raise SourceIdentityExcluded("Required source TOR is absent")

		with (
			patch.object(frappe, "db", MagicMock()),
			patch.object(frappe, "clear_messages"),
			patch.object(historical, "converted", return_value=None),
			patch.object(historical, "insert") as insert,
			patch.object(historical, "exclude") as exclude,
		):
			self.assertIsNone(
				loader.one("Deployment Request Tool", {"name": "deployment"}, "VMMS Deployment", build)
			)
			with self.assertRaises(SourceIdentityExcluded):
				loader.target("Deployment Request Tool", "deployment")
			insert.assert_not_called()
			exclude.assert_called_once()
		self.assertFalse(loader.failures)
		self.assertEqual(loader.excluded[0]["source_name"], "deployment")

	def test_committed_exclusions_are_not_reattempted_on_retry(self):
		snapshot = {"source": {"source_id": "source", "capture_id": "capture"}, "users": {}, "history": {}}

		def get_all(doctype, **kwargs):
			return (
				[
					SimpleNamespace(
						source_doctype="Deployment Request Tool",
						source_name="deployment",
						conversion_note="Required source TOR is absent",
					)
				]
				if doctype == historical.ARCHIVE
				and kwargs.get("filters", {}).get("conversion_status") == "Excluded"
				else []
			)

		with patch.object(frappe, "get_all", side_effect=get_all), patch.object(Crosswalks, "read"):
			loader = Loader(snapshot, "run", None, {"allow_missing_history_dependencies": True})
			loader.batch_limit, loader.batch_attempted = 100, 0
			build = MagicMock()
			self.assertIsNone(
				loader.one("Deployment Request Tool", {"name": "deployment"}, "VMMS Deployment", build)
			)
			build.assert_not_called()
			self.assertEqual(loader.batch_attempted, 0)
			self.assertEqual(loader.excluded[0]["reason"], "Required source TOR is absent")
			loader.excluded = []
			loader.projects = lambda: loader.one(
				"Deployment Request Tool", {"name": "deployment"}, "VMMS Deployment", build
			)
			for method in ("terms", "deployments", "events", "applications", "availability", "time_logs"):
				setattr(loader, method, lambda: None)
			result = loader.run_batch(100, time.monotonic() + 60)
			self.assertEqual(result["attempted"], 0)
			self.assertEqual(result["excluded_count"], 1)
			self.assertTrue(result["complete"])

	def test_exclusion_updates_only_disposition_for_current_capture(self):
		snapshot = {"source": {"source_id": "source", "capture_id": "capture"}}
		with patch.object(frappe, "db", MagicMock()) as db:
			historical.exclude(snapshot, "Deployment Request Tool", "deployment", "Missing source TOR", "run")
			args, kwargs = db.set_value.call_args
			self.assertEqual(args[1]["snapshot_id"], "capture")
			self.assertEqual(args[1]["conversion_status"], "Retained")
			self.assertEqual(args[2]["conversion_status"], "Excluded")
			self.assertNotIn("source_payload", args[2])
			self.assertFalse(kwargs["update_modified"])
