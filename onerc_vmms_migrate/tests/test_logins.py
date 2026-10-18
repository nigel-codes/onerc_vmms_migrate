import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import frappe
from cryptography.fernet import Fernet
from onerc_vmms_migrate import logins
from onerc_vmms_migrate.source import SourceError


class TestRecipientKeyStorage(TestCase):
    def test_private_key_is_wrapped_at_rest_and_reused(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(logins.workspace, "root", return_value=directory), patch.object(frappe, "conf", frappe._dict(encryption_key=Fernet.generate_key().decode())):
            private, public = logins.keys()
            stored = (Path(directory) / "login-transfer/recipient-key.enc").read_bytes()
            self.assertNotIn(b"BEGIN PRIVATE KEY", stored)
            self.assertNotEqual(private, stored)
            self.assertEqual(logins.keys(), (private, public))
            frappe.conf.encryption_key = Fernet.generate_key().decode()
            with self.assertRaises(SourceError):
                logins.keys()
            self.assertEqual((Path(directory) / "login-transfer/recipient-key.enc").read_bytes(), stored)
