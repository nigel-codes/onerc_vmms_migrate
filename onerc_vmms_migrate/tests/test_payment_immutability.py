import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe
from onerc_vmms_migrate.onerc_vmms_migrate.doctype.legacy_m_pesa_payment.legacy_m_pesa_payment import LegacyMPesaPayment


class TestPaymentEvidencePermissions(TestCase):
    def document(self):
        original = {'receipt_number': 'ORIGINAL', 'mapping_status': 'Unresolved'}
        values = dict(original)
        doc = SimpleNamespace(source_payload='{}', source_hash=hashlib.sha256(b'{}').hexdigest(),
            meta=SimpleNamespace(fields=[SimpleNamespace(fieldname=f) for f in values]),
            get=values.get, get_doc_before_save=lambda: SimpleNamespace(get=original.get))
        return doc, values

    def test_review_does_not_allow_altering_original_receipt(self):
        doc, values = self.document()
        with patch.object(frappe, 'throw', side_effect=ValueError):
            LegacyMPesaPayment.validate(doc)
            values['receipt_number'] = 'CHANGED'
            with self.assertRaises(ValueError):
                LegacyMPesaPayment.validate(doc)

    def test_payload_tampering_is_rejected_before_save(self):
        doc, _ = self.document()
        doc.source_payload = '{"altered":true}'
        with patch.object(frappe, 'throw', side_effect=ValueError), self.assertRaises(ValueError):
            LegacyMPesaPayment.validate(doc)

    def test_history_is_not_public_or_normally_writable(self):
        import onerc_vmms_migrate
        root = Path(onerc_vmms_migrate.__file__).parent
        folder = root/'onerc_vmms_migrate/doctype'/frappe.scrub('Legacy M-Pesa Payment')
        schema = json.loads((folder/(folder.name+'.json')).read_text())
        self.assertFalse(schema['index_web_pages_for_search'])
        self.assertTrue(schema['track_changes'])
        self.assertTrue(all(p['role'] == 'System Manager' and not any(p.get(f) for f in ('write','create','delete')) for p in schema['permissions']))
