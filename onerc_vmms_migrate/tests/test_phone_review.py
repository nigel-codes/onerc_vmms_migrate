"""The review setting must catch the same native validation as Red Profile."""

from unittest import TestCase
from unittest.mock import patch

import frappe

from onerc_vmms_migrate import load


class TestPhoneReview(TestCase):
	def check(self, allow_review):
		ctx = {"conf": {"drop_invalid_phones": allow_review}, "warnings": []}
		data = {"phone": "+2542049963400719047340", "legacy_phone": "+2542049963400719047340"}

		def throw(message, *args, **kwargs):
			raise frappe.ValidationError(message)

		with (
			patch("onerc_core.society.services.config.validate_phone_number"),
			patch.object(frappe, "throw", side_effect=throw),
			patch.object(frappe, "clear_last_message"),
			patch.object(frappe, "_", side_effect=lambda value, *a, **kw: value),
		):
			load._check_phone(ctx, data)
		return ctx, data

	def test_review_keeps_original_and_omits_operational_value_rejected_by_frappe(self):
		ctx, data = self.check(True)
		self.assertIsNone(data["phone"])
		self.assertEqual(data["legacy_phone"], "+2542049963400719047340")
		self.assertTrue(ctx["warnings"])

	def test_strict_setting_still_refuses_the_invalid_value(self):
		with self.assertRaises(frappe.ValidationError):
			self.check(False)
