from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate import emergency_contacts, transform
from onerc_vmms_migrate.tests.test_transform import CONF, TODAY, crosswalks, snapshot, volunteer_employee


class TestLegacyRelationship(TestCase):
    def test_a_known_relationship_does_not_create_or_change_lookup_values(self):
        with patch.object(frappe, "get_doc") as create, patch.object(frappe, "get_meta") as meta:
            emergency_contacts.prepare([{"relationship": "Mother"}])
        create.assert_not_called()
        meta.assert_not_called()

    def test_missing_relationship_gets_an_explicit_historical_lookup_hidden_from_new_forms(self):
        field = frappe._dict(fieldtype="Link", options="VMMS Relationship")
        database = MagicMock()
        database.exists.return_value = False
        with patch.object(frappe, "db", database), patch.object(frappe, "get_meta") as meta, \
                patch.object(frappe, "get_doc") as create:
            meta.return_value.get_field.return_value = field
            emergency_contacts.prepare([{"relationship": "Not recorded"}])
        values = create.call_args.args[0]
        self.assertEqual(values["relationship_name"], "Not recorded")
        self.assertEqual(values["is_active"], 0)
        self.assertIn("source", values["description"])

    def test_existing_unknown_lookup_is_reused_without_altering_society_configuration(self):
        with patch.object(frappe, "db", MagicMock()) as db, patch.object(frappe, "get_meta") as meta, \
                patch.object(frappe, "get_doc") as create:
            meta.return_value.get_field.return_value = frappe._dict(fieldtype="Link", options="VMMS Relationship")
            db.exists.return_value = "Not recorded"
            emergency_contacts.prepare([{"relationship": "Not recorded"}])
        create.assert_not_called()

    def test_old_text_schema_does_not_need_a_lookup_record(self):
        with patch.object(frappe, "get_meta") as meta, patch.object(frappe, "get_doc") as create:
            meta.return_value.get_field.return_value = frappe._dict(fieldtype="Data")
            emergency_contacts.prepare([{"relationship": "Not recorded"}])
        create.assert_not_called()

    def test_missing_relationship_is_reported_and_keeps_contact_name_phone_and_consent(self):
        result = transform.build(snapshot(employees={"E": volunteer_employee("E", relation=None)}), crosswalks(), CONF, TODAY)
        (bundle,) = result["bundles"]
        contact = bundle["application"]["emergency_contacts"][0]
        self.assertEqual(contact["contact_name"], "Jane Otieno")
        self.assertEqual(contact["primary_phone"], "+254722000111")
        self.assertEqual(contact["relationship"], "Not recorded")
        self.assertEqual(contact["may_contact_in_emergency"], 0)
        self.assertTrue(any("relationship" in warning and "not recorded" in warning.lower() for warning in bundle["warnings"]))
