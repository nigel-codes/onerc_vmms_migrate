import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import frappe

from onerc_vmms_migrate import placeholder_emails, placeholder_mail


class NativeQueue:
    def send(self, *args, **kwargs):
        return "native sender reached"


class Queue(placeholder_mail.PlaceholderEmailGuard, NativeQueue):
    def __init__(self, *addresses):
        self.recipients = [frappe._dict(recipient=value) for value in addresses]


class TestPlaceholderMail(TestCase):
    def test_registered_placeholder_is_blocked_even_with_force_send(self):
        with patch.object(placeholder_emails, "blocked_addresses", return_value={"missingemail1@vmms.com"}):
            with self.assertRaisesRegex(frappe.ValidationError, "placeholder"):
                Queue("Someone <MISSINGEMAIL1@VMMS.COM>").send(force_send=True)

    def test_a_mixed_queue_cannot_send_any_mail_to_a_placeholder(self):
        with patch.object(placeholder_emails, "blocked_addresses", return_value={"missingemail1@vmms.com"}):
            with self.assertRaises(frappe.ValidationError):
                Queue("real@example.org", "missingemail1@vmms.com").send()

    def test_normal_mail_and_unallocated_similar_addresses_keep_native_behavior(self):
        with patch.object(placeholder_emails, "blocked_addresses", return_value={"missingemail1@vmms.com"}):
            self.assertEqual(Queue("real@example.org").send(), "native sender reached")
            self.assertEqual(Queue("missingemail2@vmms.com").send(), "native sender reached")

    def test_registry_failure_cannot_enable_delivery(self):
        with patch.object(placeholder_emails, "blocked_addresses", side_effect=ValueError("corrupt registry")):
            with self.assertRaises(ValueError):
                Queue("missingemail1@vmms.com").send(force_send=True)

    def test_guard_uses_preserved_registry_even_if_assignment_setting_is_disabled(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "placeholder-emails.json"
            path.write_text('{"version":1,"assignments":[{"email":"missingemail1@vmms.com"}]}')
            with patch.object(placeholder_emails, "registry_path", return_value=path):
                self.assertEqual(placeholder_emails.blocked_addresses(), {"missingemail1@vmms.com"})

    def test_losing_the_registry_cannot_enable_delivery_to_old_aliases(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(placeholder_emails, "registry_path", return_value=Path(folder) / "absent.json"):
                with self.assertRaisesRegex(ValueError, "registry"):
                    Queue("missingemail1@vmms.com").send(force_send=True)
