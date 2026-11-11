"""Historical children must have parent links before native validation."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from frappe.model.document import Document
from onerc_vmms_migrate import historical


class TestHistoricalInsert(TestCase):
    def test_resolved_owner_is_written_with_the_document(self):
        doc, child = self.document()
        with patch.object(frappe, "new_doc", return_value=doc), patch.object(frappe, "db", SimpleNamespace(exists=lambda *args: False)):
            historical.insert("VMMS Terms of Reference", {"creation": "2026-01-01"}, name="legacy-tor", owner="coordinator@example.test")
        self.assertEqual(doc.owner, "coordinator@example.test")
        self.assertEqual(child.owner, doc.owner)

    def document(self):
        child = SimpleNamespace(name=None, parent=None, parenttype=None,
            parentfield="objectives", get_invalid_links=lambda: ([], []), db_insert=MagicMock())
        doc = SimpleNamespace(doctype="VMMS Terms of Reference", name=None, flags=frappe._dict(),
            docstatus=0, get_invalid_links=lambda: ([], []), get_all_children=lambda: [child],
            db_insert=MagicMock())
        doc.update = lambda values: doc.__dict__.update(values)
        doc.set_parent_in_children = lambda: Document.set_parent_in_children(doc)
        def validate():
            if not child.parent or child.parenttype != doc.doctype:
                raise frappe.MandatoryError("parent")
        doc._validate = MagicMock(side_effect=validate)
        return doc, child

    def test_children_validate_and_persist_with_final_parent_name(self):
        doc, child = self.document()
        with patch.object(frappe, "new_doc", return_value=doc), patch.object(frappe, "db", SimpleNamespace(exists=lambda *args: False)):
            result = historical.insert("VMMS Terms of Reference", {"creation":"2026-01-01", "objectives":[{"objective":"Recorded objective"}]}, name="legacy-tor")
        self.assertEqual(result.name, "legacy-tor")
        self.assertEqual((child.parent, child.parenttype, child.parentfield),
                         ("legacy-tor", "VMMS Terms of Reference", "objectives"))
        doc._validate.assert_called_once()
        doc.db_insert.assert_called_once()
        child.db_insert.assert_called_once()

    def test_invalid_destination_links_still_prevent_insertion(self):
        doc, child = self.document()
        child.get_invalid_links = lambda: ([('methodology','missing','Methodology')], [])
        with patch.object(frappe, "new_doc", return_value=doc), patch.object(frappe, "db", SimpleNamespace(exists=lambda *args: False)):
            with self.assertRaises(frappe.LinkValidationError):
                historical.insert("VMMS Terms of Reference", {"creation":"2026-01-01"}, name="legacy-tor")
        doc.db_insert.assert_not_called()
        child.db_insert.assert_not_called()

    def test_explicit_legacy_name_keeps_required_native_series_default(self):
        doc, child = self.document()
        doc.doctype = "VMMS Membership"
        doc.naming_series = "MSHIP-.#####"
        child.parenttype = doc.doctype
        def validate():
            if not doc.naming_series:
                raise frappe.MandatoryError("naming_series")
        doc._validate.side_effect = validate
        with patch.object(frappe, "new_doc", return_value=doc), patch.object(frappe, "db", SimpleNamespace(exists=lambda *args: False)):
            result = historical.insert("VMMS Membership", {"creation": "2026-01-01", "naming_series": None}, name="VM-MSH-2026-02593")
        self.assertEqual(result.name, "VM-MSH-2026-02593")
        self.assertEqual(result.naming_series, "MSHIP-.#####")
        doc.db_insert.assert_called_once()

    def test_explicit_series_is_retained_with_legacy_name(self):
        doc, child = self.document()
        doc.naming_series = "DEFAULT-.#####"
        with patch.object(frappe, "new_doc", return_value=doc), patch.object(frappe, "db", SimpleNamespace(exists=lambda *args: False)):
            result = historical.insert("VMMS Terms of Reference", {"creation": "2026-01-01", "naming_series": "REVIEWED-.#####"}, name="legacy-tor")
        self.assertEqual(result.naming_series, "REVIEWED-.#####")
