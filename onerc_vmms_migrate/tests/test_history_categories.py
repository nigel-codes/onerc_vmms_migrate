from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate import historical
from onerc_vmms_migrate.history_load import Loader


class TestSeededCategories(TestCase):
	def loader(self):
		loader = object.__new__(Loader)
		loader.snapshot = {}
		loader.run_id = "test"
		loader.targets = {}
		loader.results, loader.failures, loader.excluded, loader.warnings = [], [], [], []
		loader.excluded_identities = set()
		return loader

	def test_seeded_categories_are_linked_without_overwriting_destination(self):
		for name in ("Conferences", "Local", "Meetups", "Webinars", "Zoom Meeting"):
			with self.subTest(name=name):
				loader = self.loader()
				destination = frappe._dict(name=name, enabled=1, icon_svg="destination icon", docstatus=0)
				with (
					patch.object(frappe, "db", MagicMock()) as db,
					patch.object(frappe, "get_doc", return_value=destination),
					patch.object(historical, "converted", return_value=None),
					patch.object(historical, "insert") as insert,
					patch.object(historical, "mark") as mark,
				):
					db.exists.return_value = name
					actual = loader.one(
						"Event Category",
						{"name": name},
						"Event Category",
						lambda row: {"enabled": 1, "icon_svg": "source icon"},
						name=name,
					)
					self.assertEqual(actual, name)
					insert.assert_not_called()
					mark.assert_called_once_with({}, "Event Category", name, "Event Category", name, "test")
				self.assertEqual(destination.icon_svg, "destination icon")
				self.assertFalse(loader.failures)
				self.assertEqual(loader.results[0]["outcome"], "existing")
				self.assertIn("icon_svg", loader.warnings[0]["different_fields"])
				self.assertEqual(loader.targets[("Event Category", name)], name)

	def test_unseeded_category_or_other_doctype_still_uses_strict_insert(self):
		loader = self.loader()
		for doctype, name in (("Event Category", "Custom"), ("Event Host", "Conferences")):
			with (
				patch.object(frappe, "db", MagicMock()),
				patch.object(frappe, "clear_messages"),
				patch.object(historical, "converted", return_value=None),
				patch.object(historical, "insert", side_effect=ValueError("collision")) as insert,
			):
				self.assertIsNone(loader.one(doctype, {"name": name}, doctype, lambda row: {}, name=name))
				insert.assert_called_once()
		self.assertEqual(len(loader.failures), 2)

	def test_absent_seed_category_falls_through_to_normal_insertion(self):
		loader = self.loader()
		with patch.object(frappe, "db", MagicMock()) as db, patch.object(historical, "insert") as insert:
			db.exists.return_value = False
			self.assertIsNone(loader.reuse_event_category({"enabled": 1}, "Meetups"))
			insert.assert_not_called()

	def test_disabled_destination_category_is_not_reenabled(self):
		loader = self.loader()
		destination = frappe._dict(name="Local", enabled=0, docstatus=0)
		with (
			patch.object(frappe, "db", MagicMock()) as db,
			patch.object(frappe, "get_doc", return_value=destination),
		):
			db.exists.return_value = "Local"
			self.assertIs(loader.reuse_event_category({"enabled": 1}, "Local"), destination)
		self.assertEqual(destination.enabled, 0)
		self.assertIn("enabled", loader.warnings[0]["different_fields"])

	def test_unexpected_category_status_does_not_get_reused(self):
		loader = self.loader()
		with (
			patch.object(frappe, "db", MagicMock()) as db,
			patch.object(frappe, "get_doc", return_value=frappe._dict(name="Local", docstatus=2)),
		):
			db.exists.return_value = "Local"
			with self.assertRaisesRegex(ValueError, "unexpected document status"):
				loader.reuse_event_category({"enabled": 1}, "Local")
