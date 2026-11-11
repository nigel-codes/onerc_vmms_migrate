from unittest import TestCase
from unittest.mock import MagicMock,patch
import frappe
from onerc_vmms_migrate import emergency_contacts,transform
from onerc_vmms_migrate.tests.test_transform import CONF,TODAY,crosswalks,snapshot,volunteer_employee

class TestLegacyRelationship(TestCase):
    def test_inactive_old_words_are_reused_and_case_matches_the_stored_name(self):
        for raw,actual in (("Brother","Brother"),("FATHER","Father"),("Not recorded","Not recorded")):
            db=MagicMock();db.exists.return_value=actual
            contacts=[{"relationship":raw}]
            with patch.object(frappe,"db",db),patch.object(frappe,"get_doc") as create:
                emergency_contacts.prepare(contacts)
            self.assertEqual(contacts[0]["relationship"],actual)
            create.assert_not_called()
    def test_unknown_word_keeps_contact_and_reports_original_without_creating_a_lookup(self):
        db=MagicMock();db.exists.return_value=None
        review=MagicMock();contacts=[{"contact_name":"Aunty","primary_phone":"123","relationship":"Not on list"}]
        with patch.object(frappe,"db",db),patch.object(frappe,"get_doc") as create:
            emergency_contacts.prepare(contacts,review)
        self.assertIsNone(contacts[0]["relationship"])
        self.assertEqual(contacts[0]["contact_name"],"Aunty")
        review.assert_called_once_with("emergency_contacts[1].relationship","Not on list")
        create.assert_not_called()
    def test_missing_relationship_keeps_contact_with_a_blank_and_original_review(self):
        bundle=transform.build(snapshot(employees={"E":volunteer_employee("E",relation=None)}),crosswalks(),CONF,TODAY)["bundles"][0]
        contact=bundle["application"]["emergency_contacts"][0]
        self.assertIsNone(contact["relationship"])
        self.assertEqual(contact["contact_name"],"Jane Otieno")
        self.assertEqual(contact["may_contact_in_emergency"],0)
        self.assertTrue(any("relationship" in r["field"] for r in bundle["_review_values"]))
