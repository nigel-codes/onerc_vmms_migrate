import copy
import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from onerc_vmms_migrate import placeholder_emails, transform
from onerc_vmms_migrate.tests.test_transform import CONF, TODAY, crosswalks, snapshot, volunteer_employee


def source(**parts):
    return snapshot(source={"source_id": "old-site"}, **parts)


def employee(name, **fields):
    return volunteer_employee(name, personal_email=None, **fields)


class TestPlaceholderAllocation(TestCase):
    def test_invalid_source_login_and_its_explicitly_linked_person_share_an_alias(self):
        snap = source(users={"bad-address": {"name": "bad-address", "email": "bad-address"}},
            employees={"E": employee("E", user_id="bad-address")})
        rows = placeholder_emails.allocate(snap)
        self.assertEqual({r["source_doctype"] for r in rows}, {"Employee", "User"})
        self.assertEqual({r["email"] for r in rows}, {"missingemail1@vmms.com"})

    def test_invalid_account_only_user_also_gets_a_stable_alias(self):
        snap = source(users={"bad": {"name": "bad", "email": None}})
        rows = placeholder_emails.allocate(snap)
        self.assertEqual(rows, [{"source_id": "old-site", "source_doctype": "User", "source_name": "bad", "email": "missingemail1@vmms.com"}])

    def test_explicit_volunteer_member_application_links_share_an_alias(self):
        snap = source(employees={"E": employee("E", job_applicant="A")},
            members={"M": {"name": "M", "volunteer": "E"}},
            applicants={"A": {"name": "A", "is_volunteer": 1}})
        rows = placeholder_emails.allocate(snap)
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["email"] for r in rows}, {"missingemail1@vmms.com"})
        self.assertEqual({r["source_name"] for r in rows}, {"E", "M", "A"})

    def test_names_and_phones_are_never_used_to_merge_missing_email_records(self):
        snap = source(employees={"E1": employee("E1"), "E2": employee("E2")})
        rows = placeholder_emails.allocate(snap)
        self.assertEqual(len({r["email"] for r in rows}), 2)

    def test_reordering_new_records_and_new_captures_never_renumber_aliases(self):
        snap = source(employees={"E2": employee("E2")})
        original = placeholder_emails.allocate(snap)
        snap["source"]["capture_id"] = "another-capture"
        snap["employees"] = {"E1": employee("E1"), **snap["employees"]}
        result = placeholder_emails.allocate(snap, original)
        emails = {r["source_name"]: r["email"] for r in result}
        self.assertEqual(emails, {"E2": "missingemail1@vmms.com", "E1": "missingemail2@vmms.com"})

    def test_source_and_destination_addresses_are_reserved(self):
        snap = source(users={"real": {"name": "real", "email": "missingemail1@vmms.com"}},
            employees={"E": employee("E")})
        rows = placeholder_emails.allocate(snap, reserved=["MISSINGEMAIL2@VMMS.COM"])
        self.assertEqual(rows[0]["email"], "missingemail3@vmms.com")

    def test_distinct_previously_allocated_identities_cannot_be_joined_silently(self):
        snap = source(employees={"E": employee("E")}, members={"M": {"name": "M"}})
        existing = placeholder_emails.allocate(snap)
        snap["members"]["M"]["volunteer"] = "E"
        with self.assertRaisesRegex(ValueError, "previously distinct"):
            placeholder_emails.allocate(snap, existing)

    def test_a_later_real_source_claim_to_an_allocated_address_requires_review(self):
        snap = source(employees={"E": employee("E")})
        existing = placeholder_emails.allocate(snap)
        snap["users"]["other"] = {"name": "other", "email": existing[0]["email"]}
        with self.assertRaisesRegex(ValueError, "claimed"):
            placeholder_emails.allocate(snap, existing)

    def test_source_names_are_namespaced_and_previous_aliases_remain_reserved(self):
        old = placeholder_emails.allocate(source(employees={"E": employee("E")}))
        other = source(employees={"E": employee("E")})
        other["source"]["source_id"] = "other-site"
        rows = placeholder_emails.allocate(other, old)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["email"] for r in rows}, {"missingemail1@vmms.com", "missingemail2@vmms.com"})

    def test_an_explicit_link_to_a_valid_identity_does_not_create_another_profile(self):
        snap = source(employees={"E": volunteer_employee("E", job_applicant="A")},
            applicants={"A": {"name": "A", "is_volunteer": 1}})
        rows = placeholder_emails.allocate(snap)
        self.assertEqual(rows, [])

    def test_a_corrupt_registry_cannot_share_an_alias_between_unlinked_records(self):
        snap = source(employees={"E1": employee("E1"), "E2": employee("E2")})
        existing = placeholder_emails.allocate(snap)
        existing[1]["email"] = existing[0]["email"]
        with self.assertRaisesRegex(ValueError, "unlinked"):
            placeholder_emails.allocate(snap, existing)


class TestPlaceholderPreparation(TestCase):
    def test_linked_source_user_retains_its_state_and_actual_hash_under_the_alias(self):
        snap = source(users={"bad": {"name": "bad", "email": "bad", "enabled": 0, "first_name": "Amina", "last_name": "Otieno"}},
            employees={"E": employee("E", user_id="bad")}, password_hashes={"bad": "original-hash"})
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(placeholder_emails, "registry_path", return_value=Path(folder) / "emails.json"):
                placeholder_emails.prepare(snap, {"assign_missing_emails": True}, reserved=[])
        (bundle,) = transform.build(snap, crosswalks(), CONF, TODAY)["bundles"]
        self.assertEqual(bundle["user"]["email"], "missingemail1@vmms.com")
        self.assertEqual(bundle["user"]["enabled"], 0)
        self.assertEqual(bundle["user"]["password_hash"], "original-hash")
        self.assertEqual(bundle["user"]["legacy_email_placeholder"], 1)
        self.assertIn(("User", "bad"), bundle["sources"])

    def test_prepare_persists_reuses_and_marks_alias_without_changing_source_rows(self):
        snap = source(employees={"E": employee("E")})
        originals = copy.deepcopy(snap)
        with tempfile.TemporaryDirectory() as folder:
            registry = Path(folder) / "placeholder-emails.json"
            with patch.object(placeholder_emails, "registry_path", return_value=registry):
                placeholder_emails.prepare(snap, {"assign_missing_emails": True}, reserved=[])
                second = copy.deepcopy(originals)
                placeholder_emails.prepare(second, {"assign_missing_emails": True}, reserved=[])
                self.assertEqual(snap["_migration_identity_emails"], second["_migration_identity_emails"])
            self.assertEqual(registry.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(registry.read_text())["version"], 1)
        self.assertEqual(snap["employees"], originals["employees"])
        result = transform.build(snap, crosswalks(), CONF, TODAY)
        (bundle,) = result["bundles"]
        self.assertEqual(bundle["key"], "missingemail1@vmms.com")
        self.assertEqual(bundle["profile"]["legacy_email_placeholder"], 1)
        self.assertIsNone(bundle["user"])
        self.assertEqual(bundle["volunteer"]["source_name"], "E")
        self.assertEqual(result["not_migrated"], [])

    def test_default_does_not_silently_enable_an_unapproved_policy(self):
        snap = source(employees={"E": employee("E")})
        placeholder_emails.prepare(snap, {}, reserved=[])
        self.assertNotIn("_migration_identity_emails", snap)

    def test_preparation_applies_a_real_email_link_without_creating_an_alias(self):
        snap = source(employees={"E": volunteer_employee("E", job_applicant="A")},
            applicants={"A": {"name": "A", "is_volunteer": 1}})
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(placeholder_emails, "registry_path", return_value=Path(folder) / "emails.json"):
                placeholder_emails.prepare(snap, {"assign_missing_emails": True}, reserved=[])
        (bundle,) = transform.build(snap, crosswalks(), CONF, TODAY)["bundles"]
        self.assertNotIn("legacy_email_placeholder", bundle["profile"])
        self.assertEqual(set(bundle["sources"]), {("Employee", "E"), ("Job Applicant", "A")})
