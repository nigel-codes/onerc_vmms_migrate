"""Source evidence is immutable; later allocation review is a separate concern."""
import hashlib

import frappe
from frappe.model.document import Document


class LegacyMPesaPayment(Document):
    def validate(self):
        payload = self.source_payload or ''
        if hashlib.sha256(payload.encode()).hexdigest() != self.source_hash:
            frappe.throw('Historical payment payload does not match its source hash.')
        previous = self.get_doc_before_save()
        if previous:
            for field in self.meta.fields:
                if field.fieldname not in ('mapping_status', 'allocations') and self.get(field.fieldname) != previous.get(field.fieldname):
                    frappe.throw('Original historical payment evidence cannot be changed; retain a new source version.')
