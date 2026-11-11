"""Restore only an approved captured credential, inside the person's transaction."""

from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch
import json

import frappe
from passlib.hash import pbkdf2_sha256
from onerc_vmms_migrate import batches, load, login_restoration


class TestApprovedLoginRestoration(TestCase):
	def setUp(self):
		self.email = "coordinator@example.test"
		self.source_hash = pbkdf2_sha256.using(rounds=1000).hash("synthetic-source-password")
		self.old_hash = pbkdf2_sha256.using(rounds=1000).hash("synthetic-target-password")
		self.current_hash = self.old_hash
		self.callbacks = []
		self.audits = {}
		self.database = MagicMock()
		self.database.sql.side_effect = self.sql
		self.database.exists.side_effect = lambda dt, name: (
			name == self.email if dt == "User" else name in self.audits
		)
		self.database.after_commit.add.side_effect = self.callbacks.append
		self.value = {
			"scope": "people",
			"mode": "import",
			"status": "ready",
			"phase": "people",
			"source_id": "source",
			"capture_id": "capture",
			"session_id": "session",
			"run_id": "run",
		}
		self.login = {"email": self.email, "source_name": self.email, "password_hash": self.source_hash}
		self.snapshot = {
			"source": {"source_id": "source", "snapshot_id": "capture", "logins": {"included": True}},
			"users": {self.email: {"email": self.email}},
			"password_hashes": {self.email: self.source_hash},
		}
		self.ctx = {
			"bundle": {"key": self.email, "user": self.login, "profile": {}},
			"source": SimpleNamespace(data=self.snapshot),
			"run_id": "run",
			"ledger": [],
			"warnings": [],
			"conf": {"preserve_passwords": True, "browser_capture": "capture"},
		}
		self.enterContext(patch.object(frappe, "db", self.database))
		self.enterContext(patch.object(frappe, "local", frappe._dict(site="destination.example.test")))
		self.enterContext(patch.object(frappe, "session", frappe._dict(user="Administrator")))
		self.enterContext(patch.object(frappe, "flags", frappe._dict(vmms_migration_import=True)))
		self.enterContext(patch.object(frappe, "get_doc", side_effect=self.document))
		self.enterContext(patch.object(batches, "checkpoint", return_value=self.value))
		self.enterContext(
			patch.object(
				login_restoration,
				"APPROVED_RESTORATIONS",
				frozenset(
					{
						login_restoration.approval_key(
							"destination.example.test", "source", "capture", "session", self.email, self.email
						)
					}
				),
			)
		)
		self.clear = self.enterContext(patch("frappe.sessions.clear_sessions"))
		self.log_error = self.enterContext(patch.object(frappe, "log_error"))

	def sql(self, statement, params):
		if statement.startswith("SELECT password"):
			return [(self.current_hash,)]
		if statement.startswith("INSERT INTO __Auth"):
			self.assertEqual(params[0], self.email)
			self.current_hash = params[1]
			return []
		self.fail("Unexpected database query")

	def document(self, values):
		self.assertEqual(values["doctype"], "Activity Log")
		document = frappe._dict(name="audit-" + str(len(self.audits) + 1))

		def insert(**kwargs):
			self.audits[document.name] = deepcopy(values)
			return document

		document.insert = insert
		return document

	def test_existing_login_restores_only_approved_source_password_and_remains_adopted(self):
		before = deepcopy(self.value)
		self.assertEqual(load._user(self.ctx), self.email)
		self.assertTrue(pbkdf2_sha256.verify("synthetic-source-password", self.current_hash))
		self.assertEqual(self.value, before)
		self.assertEqual(self.ctx["ledger"][-1]["outcome"], "adopted")
		self.database.commit.assert_not_called()
		self.clear.assert_not_called()
		self.assertEqual(len(self.callbacks), 1)
		self.assertEqual(len(self.audits), 1)
		self.assertEqual(len(self.ctx["password_restorations"]), 1)
		evidence = json.dumps([self.ctx["password_restorations"], self.audits, self.ctx["warnings"]])
		for secret in (self.old_hash, self.source_hash, "synthetic-source-password"):
			self.assertNotIn(secret, evidence)

	def test_unapproved_existing_login_conflicts_still_stop_without_changing_credentials(self):
		with patch.object(login_restoration, "APPROVED_RESTORATIONS", frozenset()):
			with self.assertRaisesRegex(ValueError, "different password credential"):
				load._user(self.ctx)
		self.assertEqual(self.current_hash, self.old_hash)
		self.assertFalse(self.callbacks)
		self.assertFalse(self.audits)

	def test_approval_does_not_transfer_to_a_different_site_session_capture_or_source(self):
		for key in ("source_id", "capture_id", "session_id", "run_id", "scope", "mode", "status", "phase"):
			with self.subTest(key=key):
				before = self.value[key]
				self.value[key] = "different"
				self.assertFalse(login_restoration.restore_approved(self.ctx, self.login))
				self.value[key] = before
		frappe.local.site = "another.example.test"
		self.assertFalse(login_restoration.restore_approved(self.ctx, self.login))
		self.assertEqual(self.current_hash, self.old_hash)

	def test_only_the_recipient_verified_captured_hash_is_accepted(self):
		self.snapshot["password_hashes"][self.email] = self.old_hash
		self.assertFalse(login_restoration.restore_approved(self.ctx, self.login))
		self.snapshot["password_hashes"][self.email] = self.source_hash
		self.snapshot["source"]["logins"]["included"] = False
		self.assertFalse(login_restoration.restore_approved(self.ctx, self.login))
		self.assertFalse(self.audits)
		self.assertEqual(self.current_hash, self.old_hash)

	def test_untrusted_or_unprotected_load_cannot_restore_a_password(self):
		frappe.flags.vmms_migration_import = False
		self.assertFalse(login_restoration.restore_approved(self.ctx, self.login))
		frappe.flags.vmms_migration_import = True
		self.ctx["conf"]["preserve_passwords"] = False
		self.assertFalse(login_restoration.restore_approved(self.ctx, self.login))
		self.assertEqual(self.current_hash, self.old_hash)

	def test_successful_commit_cleanup_expires_only_this_accounts_other_sessions(self):
		load._user(self.ctx)
		self.callbacks[0]()
		self.clear.assert_called_once_with(user=self.email, keep_current=True, force=True)

	def test_person_rollback_cancels_effect_of_deferred_session_cleanup(self):
		load._user(self.ctx)
		# Frappe retains after-commit callbacks on rollback to a person savepoint.
		# The rolled-back audit row is the durable guard for that person's callback.
		self.audits.clear()
		self.current_hash = self.old_hash
		self.callbacks[0]()
		self.clear.assert_not_called()
		self.assertEqual(self.current_hash, self.old_hash)

	def test_cleanup_error_cannot_turn_a_committed_people_batch_into_a_failure(self):
		load._user(self.ctx)
		self.clear.side_effect = RuntimeError("session service unavailable")
		self.callbacks[0]()
		self.log_error.assert_called_once()
		self.assertTrue(pbkdf2_sha256.verify("synthetic-source-password", self.current_hash))

	def test_an_exact_matching_credential_is_adopted_without_another_repair(self):
		self.current_hash = self.source_hash
		self.assertEqual(load._user(self.ctx), self.email)
		self.assertFalse(self.audits)
		self.assertFalse(self.callbacks)

	def test_diagnostic_outage_during_cleanup_does_not_raise_after_commit(self):
		load._user(self.ctx)
		self.clear.side_effect = RuntimeError("session service unavailable")
		self.log_error.side_effect = RuntimeError("logging service unavailable")
		self.callbacks[0]()
		self.assertEqual(len(self.audits), 1)
		self.assertTrue(pbkdf2_sha256.verify("synthetic-source-password", self.current_hash))
