import json
from unittest import TestCase

from onerc_vmms_migrate import identity_review, transform
from onerc_vmms_migrate.tests.test_transform import CONF, TODAY, crosswalks, snapshot, volunteer_employee


class TestIdentityReview(TestCase):
    def build(self, snap):
        return transform.build(snap, crosswalks(), CONF, TODAY)

    def test_missing_names_keep_original_source_fields_and_are_not_counted_as_imported(self):
        snap = snapshot(source={"source_id": "s"}, employees={"E": volunteer_employee("E",
            first_name="Amina", last_name=None)})
        built = self.build(snap)
        rows = identity_review.rows(snap, built)
        (row,) = [r for r in rows if r["issue_code"] == "missing_name"]
        self.assertEqual(row["source_name"], "E")
        self.assertEqual(json.loads(row["original_names"])["first_name"], "Amina")
        self.assertIsNone(json.loads(row["original_names"])["last_name"])
        self.assertEqual(row["native_status"], "eligible_pending_native_validation")

    def test_each_shared_member_identifier_gets_its_own_report_row(self):
        snap = snapshot(members={name: {"name": name, "email_id": "shared@example.org",
            "member_name": "Amina Otieno"} for name in ("M1", "M2")})
        rows = identity_review.rows(snap, self.build(snap))
        conflicts = [r for r in rows if r["issue_code"] == "shared_member_email"]
        self.assertEqual({r["source_name"] for r in conflicts}, {"M1", "M2"})
        self.assertEqual(len({r["conflict_group"] for r in conflicts}), 1)

    def test_shared_volunteers_are_not_reported_as_imported_native_volunteers(self):
        snap = snapshot(employees={name: volunteer_employee(name) for name in ("E1", "E2")})
        rows = identity_review.rows(snap, self.build(snap))
        self.assertEqual({r["source_name"] for r in rows if r["issue_code"] == "shared_volunteer_email"}, {"E1", "E2"})
        self.assertTrue(all(r["native_status"] == "volunteer_identity_pending" for r in rows))

    def test_missing_email_placeholders_do_not_claim_to_have_a_login(self):
        snap = snapshot(source={"source_id": "s"}, employees={"E": volunteer_employee("E", personal_email=None)})
        snap["_migration_identity_emails"] = [{"source_id": "s", "source_doctype": "Employee", "source_name": "E",
            "email": "missingemail1@vmms.com", "is_placeholder": 1}]
        rows = identity_review.rows(snap, self.build(snap))
        (row,) = [r for r in rows if r["issue_code"] == "placeholder_email"]
        self.assertEqual(row["destination_email"], "missingemail1@vmms.com")
        self.assertEqual(row["login_status"], "no_login_created")
        self.assertEqual(row["outbound_email_allowed"], 0)
        self.assertEqual(row["native_status"], "eligible_pending_native_validation")

    def test_ledger_targets_are_attached_to_each_original_source_and_never_guessed(self):
        rows = [{"person": "p", "source_doctype": "Employee", "source_name": "E"},
            {"person": "p", "source_doctype": "Employee", "source_name": "E2"}]
        ledger = [{"person_key": "p", "source_doctype": "Person", "source_name": "p", "target_doctype": "Red Profile", "target_name": "RP1"},
            {"person_key": "p", "source_doctype": "Employee", "source_name": "E", "target_doctype": "VMMS Volunteer", "target_name": "E"}]
        enriched = identity_review.with_targets(rows, ledger)
        self.assertEqual(enriched[0]["destination_profile"], "RP1")
        self.assertEqual(enriched[0]["destination_record"], "E")
        self.assertEqual(enriched[1]["destination_record"], "")
        self.assertEqual(enriched[1]["record_status"], "native_record_not_found")

    def test_unsupported_invalid_user_is_also_reported(self):
        snap = snapshot(users={"bad": {"name": "bad", "email": None, "first_name": None, "enabled": 1}})
        rows = identity_review.rows(snap, self.build(snap))
        self.assertTrue(any(r["source_doctype"] == "User" and r["issue_code"] == "missing_login_email" for r in rows))

    def test_identification_conflict_links_distinct_people_to_the_same_claim(self):
        users = {email: {"name": email, "email": email, "first_name": "Amina", "last_name": "Otieno",
            "identification_type": "National ID", "id_number": "12345"} for email in ("one@example.org", "two@example.org")}
        snap = snapshot(users=users, employees={name: volunteer_employee(name, user_id=email)
            for name, email in zip(("E1", "E2"), users)})
        rows = identity_review.rows(snap, self.build(snap))
        conflicts = [r for r in rows if r["issue_code"] == "conflicting_identification"]
        self.assertEqual(len({r["person"] for r in conflicts}), 2)
        self.assertEqual(len({r["conflict_group"] for r in conflicts}), 1)
        self.assertTrue(all("12345" in r["conflicting_identification"] for r in conflicts))
