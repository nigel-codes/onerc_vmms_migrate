"""Account passes reuse source User mappings made by the earlier people pass."""

from copy import deepcopy
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from onerc_vmms_migrate import accounts, load, workspace
from onerc_vmms_migrate.tests.test_transform import snapshot
from onerc_vmms_migrate.onerc_vmms_migrate.doctype.legacy_migration_record.legacy_migration_record import (
	LegacyMigrationRecord,
)


class TestAccountMappingReuse(TestCase):
	def setUp(self):
		self.temp = self.enterContext(tempfile.TemporaryDirectory())
		self.root = Path(self.temp)
		self.records = {}
		self.users = {"person@example.test": {"enabled": 1, "roles": ["VMMS Volunteer"]}}
		self.database = MagicMock()
		self.database.get_value.side_effect = self.get_value
		self.database.exists.side_effect = self.exists
		self.enterContext(patch.object(frappe, "db", self.database))
		self.enterContext(patch.object(frappe, "get_doc", side_effect=self.document))
		self.enterContext(
			patch.object(frappe, "throw", side_effect=lambda message: self.fail_validation(message))
		)
		self.enterContext(patch.object(frappe, "clear_messages"))
		self.enterContext(patch.object(workspace, "path", return_value=str(self.root / "failures.json")))
		self.enterContext(patch("onerc_vmms_migrate.historical.mark"))
		self.native_user = load._user
		self.enterContext(patch.object(load, "_user", side_effect=self.user))

	def fail_validation(self, message):
		raise frappe.ValidationError(message)

	def matches(self, row, filters):
		if isinstance(filters, str):
			return row.name == filters
		return all(
			row.get(k) != v[1] if isinstance(v, tuple) and v[0] == "!=" else row.get(k) == v
			for k, v in filters.items()
		)

	def exists(self, doctype, filters):
		if doctype == "User":
			return filters if filters in self.users else None
		return next((row.name for row in self.records.values() if self.matches(row, filters)), None)

	def get_value(self, doctype, filters, fields, as_dict=False, **kwargs):
		row = next((row for row in self.records.values() if self.matches(row, filters)), None)
		if not row:
			return None
		return (
			frappe._dict({f: row.get(f) for f in fields})
			if isinstance(fields, (list, tuple))
			else row.get(fields)
		)

	def document(self, values):
		self.assertEqual(values["doctype"], load.LEDGER)
		doc = frappe._dict(values)
		doc.name = "mapping-" + str(len(self.records) + 1)

		def insert(**kwargs):
			# Use the actual native migration-ledger validator that rejects duplicates.
			LegacyMigrationRecord.validate(doc)
			self.records[doc.name] = doc
			return doc

		doc.insert = insert
		return doc

	def user(self, ctx):
		login = ctx["bundle"]["user"]
		if login["email"] in self.users:
			return self.native_user(ctx)
		self.users[login["email"]] = {"enabled": login["enabled"], "roles": []}
		return login["email"]

	def person_mapping(self, target="person@example.test"):
		return self.document(
			{
				"doctype": load.LEDGER,
				"person_key": "person@example.test",
				"source_doctype": "User",
				"source_name": "person@example.test",
				"target_doctype": "User",
				"target_name": target,
				"source_hash": "complete-person-bundle-hash",
				"outcome": "created",
				"run_id": "run",
			}
		).insert()

	def source(self, *names):
		return snapshot(
			source={"source_id": "source"},
			users={
				name: {
					"name": name,
					"email": name,
					"first_name": "Source",
					"last_name": "Account",
					"enabled": 1,
				}
				for name in names
			},
		)

	def test_people_first_user_mapping_is_reused_without_changing_identity_or_account(self):
		existing = self.person_mapping()
		before = deepcopy(dict(existing))
		users = deepcopy(self.users)
		result = accounts.transfer(self.source("person@example.test"), {}, "run")
		self.assertEqual(result["failed"], [])
		self.assertEqual((result["loaded"], result["existing"]), (0, 1))
		self.assertEqual(len(self.records), 1)
		self.assertEqual(dict(existing), before)
		self.assertEqual(self.users, users)

	def test_mixed_people_and_account_only_page_succeeds_and_retries_without_duplicate_mappings(self):
		self.person_mapping()
		snap = self.source("person@example.test", "new@example.test")
		first = accounts.transfer(snap, {}, "run")
		self.assertEqual(first["failed"], [])
		self.assertEqual((first["loaded"], first["existing"]), (1, 1))
		self.assertEqual(len(self.records), 2)
		second = accounts.transfer(snap, {}, "run")
		self.assertEqual(second["failed"], [])
		self.assertEqual((second["loaded"], second["existing"]), (0, 2))
		self.assertEqual(len(self.records), 2)

	def test_a_source_user_mapping_to_another_destination_is_not_overwritten_or_counted_as_success(self):
		existing = self.person_mapping(target="different@example.test")
		result = accounts.transfer(self.source("person@example.test"), {}, "run")
		self.assertEqual(len(result["failed"]), 1)
		self.assertIn("different destination", result["failed"][0]["error"])
		self.assertEqual((result["loaded"], result["existing"]), (0, 0))
		self.assertEqual(existing.target_name, "different@example.test")
		self.assertEqual(len(self.records), 1)

	def test_existing_account_first_mapping_keeps_its_separate_fingerprint_and_person_key(self):
		snap = self.source("new@example.test")
		first = accounts.transfer(snap, {}, "run")
		self.assertEqual(first["failed"], [])
		row = next(iter(self.records.values()))
		self.assertEqual(row.person_key, "account:new@example.test")
		before = deepcopy(dict(row))
		second = accounts.transfer(snap, {}, "run")
		self.assertEqual(second["failed"], [])
		self.assertEqual(dict(row), before)
