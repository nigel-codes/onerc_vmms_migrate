"""The review setting must catch the same native validation as Red Profile."""

from unittest import TestCase
from unittest.mock import patch

import frappe

from onerc_vmms_migrate import load


class TestPhoneReview(TestCase):
	def review(self, function, data, enabled=True):
		ctx = {"conf": {"drop_invalid_phones": enabled}, "warnings": []}

		def validate(value, **kwargs):
			if len(value) > 20:
				raise frappe.ValidationError("Invalid phone")

		with (
			patch("onerc_core.society.services.config.validate_phone_number"),
			patch.object(frappe.utils, "validate_phone_number", side_effect=validate),
			patch.object(frappe, "clear_last_message"),
		):
			result = function(ctx, data)
		return ctx, result

	def test_invalid_reference_phone_does_not_discard_reference(self):
		data = {"vmms_references": [{"reference_name": "Referee", "phone": "+2542049963400719047340"}]}
		ctx, _ = self.review(load._review_profile_phones, data)
		self.assertEqual(data["vmms_references"][0]["reference_name"], "Referee")
		self.assertIsNone(data["vmms_references"][0]["phone"])
		self.assertIn("vmms_references[1].phone", ctx["warnings"][0])
		self.assertIn("+2542049963400719047340", ctx["warnings"][0])

	def test_invalid_required_contact_phone_omits_only_that_contact(self):
		rows = [
			{"contact_name": "Invalid", "primary_phone": "+2542049963400719047340"},
			{"contact_name": "Valid", "primary_phone": "+254712345678"},
		]
		ctx, result = self.review(load._review_emergency_contacts, rows)
		self.assertEqual([row["contact_name"] for row in result], ["Valid"])
		self.assertIn("contact omitted", ctx["warnings"][0])
		self.assertEqual(rows[0]["primary_phone"], "+2542049963400719047340")

	def test_invalid_optional_contact_phone_preserves_contact(self):
		rows = [
			{
				"contact_name": "Valid",
				"primary_phone": "+254712345678",
				"alternative_phone": "+2542049963400719047340",
			}
		]
		_, result = self.review(load._review_emergency_contacts, rows)
		self.assertEqual(len(result), 1)
		self.assertIsNone(result[0]["alternative_phone"])

	def test_strict_reference_phone_still_blocks(self):
		with self.assertRaises(frappe.ValidationError):
			self.review(
				load._review_profile_phones,
				{"vmms_references": [{"phone": "+2542049963400719047340"}]},
				False,
			)

	def test_valid_phones_are_unchanged_without_warnings(self):
		data = {"phone": "+254712345678", "vmms_references": [{"phone": "+254798765432"}]}
		ctx, _ = self.review(load._review_profile_phones, data)
		self.assertEqual(data["phone"], "+254712345678")
		self.assertEqual(data["vmms_references"][0]["phone"], "+254798765432")
		self.assertFalse(ctx["warnings"])

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
