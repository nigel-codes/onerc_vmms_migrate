from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate import accounts, historical, load


class TestPlaceholderLoading(TestCase):
    def test_existing_alias_login_requires_the_original_source_ledger_mapping(self):
        ctx = {"bundle": {"user": {"email": "missingemail1@vmms.com", "source_name": "bad",
            "legacy_email_placeholder": 1}, "profile": {}}, "conf": {}}
        database = MagicMock()
        database.exists.side_effect = lambda doctype, *_: doctype == "User"
        with patch.object(frappe, "db", database):
            with self.assertRaisesRegex(ValueError, "original source mapping"):
                load._user(ctx)

    def test_account_alias_keeps_source_state_and_actual_credential_without_creating_a_profile(self):
        snapshot = {"users": {"bad": {"name": "bad", "first_name": "Amina", "enabled": 0}},
            "password_hashes": {"bad": "the-original-hash"},
            "_migration_identity_emails": [{"source_doctype": "User", "source_name": "bad",
                "email": "missingemail1@vmms.com", "is_placeholder": 1}]}
        contexts = []
        def insert(ctx):
            contexts.append(ctx)
            return ctx["bundle"]["user"]["email"]
        database = MagicMock()
        database.exists.return_value = False
        with patch.object(frappe, "db", database), patch.object(frappe, "get_doc", return_value=MagicMock()), \
                patch.object(load, "_user", side_effect=insert), patch.object(historical, "mark"), \
                patch.object(accounts.workspace, "path", return_value="unused"), patch.object(accounts.workspace, "write_json"):
            result = accounts.transfer(snapshot, {"assign_missing_emails": True}, "test")
        self.assertEqual(result["loaded"], 1)
        self.assertEqual(result["failed"], [])
        self.assertEqual(contexts[0]["bundle"]["user"]["enabled"], 0)
        self.assertEqual(contexts[0]["bundle"]["user"]["password_hash"], "the-original-hash")
        self.assertEqual(contexts[0]["bundle"]["user"]["legacy_email_placeholder"], 1)

    def test_placeholder_may_not_be_adopted_by_an_existing_login(self):
        database = MagicMock()
        database.exists.return_value = True
        database.get_value.return_value = None
        ctx = {"bundle": {"key": "missingemail1@vmms.com", "profile": {"email": "missingemail1@vmms.com",
            "first_name": "Amina", "last_name": "Otieno", "legacy_email_placeholder": 1}},
            "conf": {}, "warnings": []}
        with patch.object(frappe, "db", database), patch.object(frappe, "get_meta", return_value=MagicMock()), \
                patch.object(load, "_check_links"), patch.object(load, "_review_profile_phones"):
            with self.assertRaisesRegex(ValueError, "placeholder.*claimed"):
                load._profile(ctx, None)

    def test_placeholder_may_not_silently_adopt_an_existing_profile(self):
        database = MagicMock()
        database.exists.return_value = False
        database.get_value.return_value = "SOME-OTHER-PROFILE"
        ctx = {"bundle": {"key": "missingemail1@vmms.com", "profile": {"email": "missingemail1@vmms.com",
            "first_name": "Amina", "last_name": "Otieno", "legacy_email_placeholder": 1}},
            "conf": {}, "warnings": []}
        with patch.object(frappe, "db", database), patch.object(frappe, "get_meta", return_value=MagicMock()), \
                patch.object(load, "_check_links"), patch.object(load, "_review_profile_phones"):
            with self.assertRaisesRegex(ValueError, "placeholder.*claimed"):
                load._profile(ctx, None)
