"""Post-import repairs: the rules that need no database. Behaviour on records is
rehearsed against a migrated clone, inside a transaction that is rolled back."""

import os
import re
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate import post_import_repairs as repairs


class TestIdentityPatterns(TestCase):
	def test_only_seven_or_eight_digits_are_national_ids(self):
		self.assertEqual(repairs.id_pattern("1234567"), repairs.NATIONAL_ID_PATTERN)
		self.assertEqual(repairs.id_pattern("30960975"), repairs.NATIONAL_ID_PATTERN)
		for number, kind in (
			("829944", "6 digits"),
			("123456789", "9+ digits"),
			("35581360.0", "7-8 digits ending in .0 (spreadsheet)"),
			("3558 1360", "7-8 digits with spaces or dashes"),
			("AK0156779", "letters then digits (passport-like)"),
			("CK40099", "other letters and digits"),
			("n/a?", "other"),
		):
			with self.subTest(number=number):
				self.assertEqual(repairs.id_pattern(number), kind)


class TestApplyRequest(TestCase):
	def setUp(self):
		patches = (
			patch.object(repairs, "_assert_people_finished"),
			patch.object(repairs, "_previewed", return_value=True),
			patch.object(frappe, "throw", side_effect=frappe.ValidationError),
		)
		for item in patches:
			item.start()
			self.addCleanup(item.stop)

	def test_runs_in_the_agreed_order_whatever_was_ticked_first(self):
		chosen = repairs.validate_request(["life_memberships", "system_manager"], "REPAIR", True)
		self.assertEqual(chosen, ["system_manager", "life_memberships"])

	def test_refuses_without_confirmation_backup_or_a_choice(self):
		for args in (
			(["system_manager"], "repair", True),
			(["system_manager"], "REPAIR", False),
			([], "REPAIR", True),
			(["drop_everything"], "REPAIR", True),
		):
			with self.subTest(args=args), self.assertRaises(frappe.ValidationError):
				repairs.validate_request(*args)

	def test_refuses_before_a_preview(self):
		with patch.object(repairs, "_previewed", return_value=False), self.assertRaises(frappe.ValidationError):
			repairs.validate_request(["system_manager"], "REPAIR", True)


class TestRepairList(TestCase):
	def test_every_repair_plans_and_applies(self):
		self.assertEqual(tuple(repairs.PLANS), repairs.ORDER)
		self.assertEqual(tuple(repairs.APPLY), repairs.ORDER)

	def test_the_settings_dialog_offers_the_same_repairs_in_the_same_order(self):
		form = os.path.join(os.path.dirname(repairs.__file__), "onerc_vmms_migrate", "doctype",
			"vmms_migration_settings", "vmms_migration_settings.js")
		with open(form) as handle:
			block = re.search(r"const repair_choices = \[(.*?)\n\];", handle.read(), re.S).group(1)
		offered = re.findall(r"\['(\w+)', '([^']+)'\]", block)
		self.assertEqual(offered, list(repairs.REPAIRS.items()))


class TestImportedDecisionsMayMove(TestCase):
	"""Routing re-saves applications that carry an old decision with no approver,
	stage or date. The import's own exception must let that save through, and
	only for a row that is marked imported and has a migration record."""

	def row(self, marked=1, recorded=True, flag=False):
		from onerc_vmms_migrate.import_review import ImportedDecisionMixin

		class Base:
			def _get_missing_mandatory_fields(self):
				return [("stage", ""), ("approver", ""), ("decided_on", ""), ("decision", "")]

		class Row(ImportedDecisionMixin, Base):
			doctype, name, parenttype, parent = "VMMS Approval Decision", "row-1", "VMMS Volunteer Application", "VAPP-1"

			def get(self, field):
				return marked if field == "legacy_source_decision" else None

			def is_new(self):
				return False

		patches = (
			patch.object(frappe, "db", MagicMock(get_value=MagicMock(return_value=marked), exists=MagicMock(return_value=recorded))),
			patch.object(frappe, "flags", frappe._dict(vmms_migration_import=flag)),
		)
		for item in patches:
			item.start()
			self.addCleanup(item.stop)
		return Row()

	def missing(self, **kwargs):
		return [field for field, _ in self.row(**kwargs)._get_missing_mandatory_fields()]

	def test_a_recorded_imported_decision_saves_without_approver_stage_or_date(self):
		self.assertEqual(self.missing(), ["decision"])

	def test_an_unmarked_or_unrecorded_decision_is_still_held_to_the_rules(self):
		self.assertEqual(self.missing(marked=0), ["stage", "approver", "decided_on", "decision"])
		self.assertEqual(self.missing(recorded=False), ["stage", "approver", "decided_on", "decision"])
