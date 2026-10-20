from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate.history_load import Loader


class TestHistoryReview(TestCase):
	def loader(self, review=True):
		loader = object.__new__(Loader)
		loader.conf = {"drop_invalid_phones": review}
		loader.warnings = []
		return loader

	def test_terms_phone_review_preserves_stakeholder_and_source_value(self):
		loader = self.loader()
		row = {"stakeholder": "Recorded stakeholder", "phone_number": "erterte"}
		with (
			patch.object(
				frappe.utils, "validate_phone_number", side_effect=frappe.ValidationError("Invalid phone")
			),
			patch.object(frappe, "clear_last_message"),
		):
			loader.review_phone("Personnel Terms of Reference", "TOR-1", row, "phone_number")
		self.assertEqual(row["stakeholder"], "Recorded stakeholder")
		self.assertIsNone(row["phone_number"])
		self.assertEqual(loader.warnings[0]["original_value"], "erterte")
		self.assertEqual(loader.warnings[0]["source_name"], "TOR-1")

	def test_strict_history_phone_still_blocks(self):
		with (
			patch.object(
				frappe.utils, "validate_phone_number", side_effect=frappe.ValidationError("Invalid phone")
			),
			patch.object(frappe, "clear_last_message"),
			self.assertRaises(frappe.ValidationError),
		):
			self.loader(False).review_phone(
				"Personnel Terms of Reference", "TOR-1", {"phone_number": "SFDS"}, "phone_number"
			)

	def test_missing_unit_is_created_with_recorded_label_without_conversion(self):
		loader = self.loader()
		with patch.object(frappe, "db", MagicMock()) as db, patch.object(frappe, "get_doc") as get_doc:
			db.exists.return_value = False
			get_doc.return_value.insert.return_value.name = "Pax"
			self.assertEqual(loader.resource_unit("Pax", "TOR-1"), "Pax")
			self.assertEqual(get_doc.call_args.args[0], {"doctype": "UOM", "uom_name": "Pax"})
		self.assertIn("Pax", loader.warnings[0]["warning"])

	def test_existing_unit_is_reused_without_writes(self):
		with patch.object(frappe, "db", MagicMock()) as db, patch.object(frappe, "get_doc") as get_doc:
			db.exists.return_value = "TRT"
			self.assertEqual(self.loader().resource_unit("TRT", "TOR-1"), "TRT")
			get_doc.assert_not_called()
