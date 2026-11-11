"""Use the product's required fields and Frappe mandatory validator without a site."""

import json
import importlib
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
import vmmsx
from frappe.model import base_document
from onerc_vmms_migrate import historical, hooks, membership_import

DT = "VMMS Membership"
SCHEMA = json.loads(
	(Path(vmmsx.__file__).parent / "vmms_member/doctype/vmms_membership/vmms_membership.json").read_text()
)


class Meta:
	istable = False

	def get(self, key, filters=None):
		return [frappe._dict(parent=DT, **field) for field in SCHEMA["fields"] if field.get("reqd")]


class NativeMembership(dict):
	doctype = DT

	def __init__(self, **values):
		super().__init__(
			{
				field["fieldname"]: field.get("default") or "recorded"
				for field in SCHEMA["fields"]
				if field.get("reqd")
			}
		)
		self.update(values)
		self.meta = Meta()
		self.flags = frappe._dict()
		self.db_insert = MagicMock()

	def is_new(self):
		return self.get("_new", True)

	def get_all_children(self):
		return []

	def set_parent_in_children(self):
		pass

	def get_invalid_links(self):
		return [], []

	_get_missing_mandatory_fields = base_document.BaseDocument._get_missing_mandatory_fields

	def _validate(self):
		missing = self._get_missing_mandatory_fields()
		if missing:
			raise frappe.MandatoryError(", ".join(field for field, _ in missing))


def imported(**values):
	mixins = tuple(
		getattr(importlib.import_module(path.rsplit(".", 1)[0]), path.rsplit(".", 1)[1])
		for path in hooks.extend_doctype_class[DT]
	)
	return type("ImportedMembership", (*mixins, NativeMembership), {})(**values)


class TestHistoricalMembershipSource(TestCase):
	def setUp(self):
		self.enterContext(patch.object(base_document, "_", side_effect=lambda text, **kwargs: text))
		self.enterContext(patch.object(frappe, "flags", frappe._dict(vmms_migration_import=True)))

	def values(self):
		return membership_import.values({"source_name": "VM-MSH-2026-00001", "legacy_status": "Pending"})

	def test_actual_required_source_field_accepts_only_marked_historical_import(self):
		self.assertEqual(
			next(f for f in SCHEMA["fields"] if f["fieldname"] == "membership_source")["reqd"], 1
		)
		doc = imported(**self.values())
		self.assertEqual(doc._get_missing_mandatory_fields(), [])
		self.assertIsNone(doc["membership_source"])
		self.assertEqual(doc["accounting_status"], "Historical")

	def test_historical_insert_persists_blank_source_without_gateway_or_proof(self):
		doc = imported()
		with (
			patch.object(frappe, "new_doc", return_value=doc),
			patch.object(frappe, "db", MagicMock()) as database,
			patch.object(historical, "now_datetime", return_value="2026-11-11 12:00:00"),
		):
			database.exists.return_value = False
			historical.insert(DT, self.values(), name="VM-MSH-2026-00001")
		doc.db_insert.assert_called_once()
		self.assertIsNone(doc["membership_source"])
		self.assertEqual(doc["legacy_membership_id"], "VM-MSH-2026-00001")

	def test_unmarked_and_ordinary_new_memberships_keep_native_mandatory_fields(self):
		doc = imported(**self.values())
		doc["legacy_membership_id"] = None
		self.assertIn("membership_source", [field for field, _ in doc._get_missing_mandatory_fields()])
		doc = imported(**self.values())
		frappe.flags.vmms_migration_import = False
		self.assertIn("membership_source", [field for field, _ in doc._get_missing_mandatory_fields()])

	def test_existing_proven_import_allows_only_its_already_blank_source(self):
		frappe.flags.vmms_migration_import = False
		doc = imported(**self.values())
		doc.name = "VM-MSH-2026-00001"
		doc["_new"] = False
		stored = {
			field: doc.get(field)
			for field in ("membership_source", "legacy_membership_id", "accounting_status")
		}
		with patch.object(frappe, "db", MagicMock()) as database:
			database.get_value.return_value = stored
			database.exists.return_value = True
			self.assertEqual(doc._get_missing_mandatory_fields(), [])
			database.exists.return_value = False
			self.assertIn("membership_source", [field for field, _ in doc._get_missing_mandatory_fields()])
			database.exists.return_value = True
			doc["member"] = None
			self.assertEqual([field for field, _ in doc._get_missing_mandatory_fields()], ["member"])
			stored["membership_source"] = "Gateway"
			self.assertIn("membership_source", [field for field, _ in doc._get_missing_mandatory_fields()])
