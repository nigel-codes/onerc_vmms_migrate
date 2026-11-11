from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate import membership_import


class TestHistoricalMembershipAccounting(TestCase):
	def test_history_data_cannot_match_the_outstanding_membership_predicate(self):
		for paid in (0, 1):
			row = membership_import.values(
				{"source_name": "MEMBERSHIP-1", "paid": paid, "legacy_status": "Pending"}
			)
			self.assertIsNone(row["membership_source"])
			self.assertNotIn(row["accounting_status"], ("", None, "Failed"))
			self.assertEqual(row["legacy_membership_id"], "MEMBERSHIP-1")
			self.assertEqual(row["legacy_status"], "Pending")
			self.assertEqual(row["legacy_paid"], paid)
			self.assertEqual(row["approval_stage"], "")
			self.assertIsNone(row["approval_stage_entered_on"])

	def test_accounting_option_is_added_once_without_removing_existing_options(self):
		field = frappe._dict(options="\nPosted\nFailed")
		with (
			patch.object(frappe, "get_meta", return_value=MagicMock(get_field=lambda name: field)),
			patch("frappe.custom.doctype.property_setter.property_setter.make_property_setter") as setter,
		):
			membership_import.install_status()
			self.assertEqual(setter.call_args.args[3], "\nPosted\nFailed\nHistorical")
			field.options += "\nHistorical"
			membership_import.install_status()
		setter.assert_called_once()
