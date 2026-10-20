from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate import workspace
from onerc_vmms_migrate.crosswalk import DROP
from onerc_vmms_migrate.history_load import Loader


class TestHistoryGeography(TestCase):
	def loader(self, overrides=None):
		loader = object.__new__(Loader)
		loader.conf = {"history_branch_overrides": overrides or {}}
		loader.crosswalks = MagicMock()
		loader.crosswalks.lookup.return_value = DROP
		return loader

	def test_explicit_history_override_preserves_excluded_people_branch(self):
		loader = self.loader({"Kenya Red Cross Society": "GEO-national"})
		with patch.object(frappe, "db", MagicMock()) as db:
			db.exists.return_value = True
			self.assertEqual(loader.geo("Kenya Red Cross Society"), "GEO-national")
		self.assertEqual(loader.crosswalks.lookup("branch", "Kenya Red Cross Society"), DROP)

	def test_excluded_branch_without_override_still_stops(self):
		with self.assertRaisesRegex(ValueError, "Unresolved source branch"):
			self.loader().geo("Test branch")

	def test_history_override_must_name_an_existing_node(self):
		with patch.object(frappe, "db", MagicMock()) as db:
			db.exists.return_value = False
			with self.assertRaisesRegex(ValueError, "History Geo Node does not exist"):
				self.loader({"National": "missing"}).geo("National")

	def test_normal_branch_mapping_remains_unchanged(self):
		loader = self.loader()
		loader.crosswalks.lookup.return_value = "GEO-county"
		self.assertEqual(loader.geo("County"), "GEO-county")

	def test_history_override_configuration_is_explicit_and_validated(self):
		self.assertEqual(
			workspace.history_branch_overrides('{"National":"GEO-national"}'), {"National": "GEO-national"}
		)
		for invalid in ("[]", '{"National": "-"}', '{"": "GEO-national"}', '{"National": 1}'):
			with self.subTest(invalid=invalid), self.assertRaises(ValueError):
				workspace.history_branch_overrides(invalid)
