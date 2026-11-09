from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from onerc_vmms_migrate.silence import synchronous_contacts

CONTACT = "frappe.core.doctype.user.user.create_contact"


class TestContactQueue(TestCase):
    def test_contacts_run_inside_the_person_transaction_without_using_the_full_queue(self):
        def full_queue(method, **kwargs):
            if not kwargs.get("now"):
                raise RuntimeError("QueueOverloaded")
            return "native contact"

        native = Mock(side_effect=full_queue)
        with patch.object(frappe, "enqueue", native):
            with synchronous_contacts():
                result = frappe.enqueue(CONTACT, user="synthetic", ignore_mandatory=True, enqueue_after_commit=True)
            self.assertIs(frappe.enqueue, native)
        self.assertEqual(result, "native contact")
        native.assert_called_once_with(CONTACT, user="synthetic", ignore_mandatory=True, enqueue_after_commit=False, now=True)

    def test_unrelated_jobs_keep_their_normal_queue_and_after_commit_settings(self):
        native = Mock()
        with patch.object(frappe, "enqueue", native):
            with synchronous_contacts():
                frappe.enqueue("other.app.job", queue="short", enqueue_after_commit=True)
        native.assert_called_once_with("other.app.job", queue="short", enqueue_after_commit=True)

    def test_enqueue_is_restored_on_failure(self):
        native = Mock(side_effect=ValueError("native validation"))
        with patch.object(frappe, "enqueue", native):
            with self.assertRaisesRegex(ValueError, "native validation"):
                with synchronous_contacts():
                    frappe.enqueue(CONTACT)
            self.assertIs(frappe.enqueue, native)
