"""A user page must not remove source links from the placeholder identity graph."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from onerc_vmms_migrate import people_run, placeholder_emails, load, workspace
from onerc_vmms_migrate.tests.test_transform import snapshot, volunteer_employee


class TestAccountPageAliases(TestCase):
	def setUp(self):
		self.temp = self.enterContext(tempfile.TemporaryDirectory())
		self.root = Path(self.temp)
		self.registry = self.root / "placeholder-emails.json"
		self.enterContext(patch.object(placeholder_emails, "registry_path", return_value=self.registry))
		self.enterContext(patch.object(frappe, "get_all", return_value=[]))
		self.enterContext(patch.object(frappe, "db", MagicMock()))
		frappe.db.get_value.return_value = None
		frappe.db.exists.return_value = False
		self.enterContext(patch.object(frappe, "get_doc", return_value=MagicMock()))
		self.enterContext(
			patch.object(workspace, "path", return_value=str(self.root / "account-errors.json"))
		)
		self.enterContext(patch.object(workspace, "report_dir", return_value=str(self.root)))
		self.enterContext(patch("onerc_vmms_migrate.continuity.require_capture"))
		self.enterContext(patch("onerc_vmms_migrate.historical.mark"))
		self.loader = self.enterContext(
			patch.object(load, "_user", side_effect=lambda ctx: ctx["bundle"]["user"]["email"])
		)
		self.conf = {"assign_missing_emails": True}
		self.original = snapshot(
			source={"source_id": "source"},
			users={
				"a@example.test": {"name": "a@example.test", "email": "a@example.test", "enabled": 1},
				"z-invalid-login": {"name": "z-invalid-login", "email": None, "enabled": 0},
			},
			employees={
				"E1": volunteer_employee("E1", personal_email=None, user_id="z-invalid-login"),
				"E2": volunteer_employee("E2", personal_email=None, user_id="z-invalid-login"),
			},
		)
		placeholder_emails.prepare(deepcopy(self.original), self.conf, reserved=[])
		self.before = self.registry.read_bytes()

	def work(self, offset, size=1):
		return people_run.work(
			deepcopy(self.original),
			None,
			self.conf,
			{
				"phase": "accounts",
				"offset": offset,
				"batch_size": size,
				"run_id": "run",
				"source_id": "source",
			},
		)

	def test_page_excluding_the_linking_user_keeps_the_existing_shared_alias(self):
		processed, total, failed, summary = self.work(0)
		self.assertEqual((processed, total, failed), (1, 2, []))
		self.assertEqual(summary["loaded"], 1)
		self.assertEqual(self.loader.call_args.args[0]["bundle"]["user"]["email"], "a@example.test")
		self.assertEqual(self.registry.read_bytes(), self.before)

	def test_next_page_imports_the_invalid_login_under_its_original_alias(self):
		self.work(0)
		processed, total, failed, summary = self.work(1)
		self.assertEqual((processed, total, failed), (1, 2, []))
		self.assertEqual(summary["placeholder_accounts"], 1)
		login = self.loader.call_args.args[0]["bundle"]["user"]
		self.assertEqual(login["email"], "missingemail1@vmms.com")
		self.assertEqual(login["enabled"], 0)
		self.assertEqual(login["source_name"], "z-invalid-login")
		self.assertEqual(self.registry.read_bytes(), self.before)

	def test_repeated_pages_keep_registry_bytes_and_fingerprint_inputs_unchanged(self):
		for offset in (0, 1, 0, 1):
			self.work(offset)
			self.assertEqual(self.registry.read_bytes(), self.before)
		rows = json.loads(self.before)["assignments"]
		self.assertEqual(len(rows), 3)
		self.assertEqual({r["email"] for r in rows}, {"missingemail1@vmms.com"})

	def test_a_genuinely_shared_alias_between_unlinked_people_still_blocks_before_loading(self):
		self.original["employees"]["E3"] = volunteer_employee("E3", personal_email=None)
		placeholder_emails.prepare(deepcopy(self.original), self.conf, reserved=[])
		data = json.loads(self.registry.read_text())
		for row in data["assignments"]:
			row["email"] = "missingemail1@vmms.com"
		self.registry.write_text(json.dumps(data))
		corrupt = self.registry.read_bytes()
		with self.assertRaisesRegex(ValueError, "unlinked identities"):
			self.work(0)
		self.loader.assert_not_called()
		self.assertEqual(self.registry.read_bytes(), corrupt)
