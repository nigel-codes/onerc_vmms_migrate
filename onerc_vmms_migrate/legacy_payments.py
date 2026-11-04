"""Immutable Navari evidence. No gateway, accounting or membership lifecycle calls."""
import hashlib
from datetime import datetime
from decimal import Decimal, InvalidOperation

from onerc_vmms_migrate.history_model import canonical

DOCTYPE = 'Legacy M-Pesa Payment'
TYPES = {
    'Mpesa Express Request': ('Inbound', 'STK request'),
    'Mpesa C2B Payment Register': ('Inbound', 'C2B receipt'),
    'B2C Disbursement Request': ('Outbound', 'Disbursement request'),
    'B2C Payment Disbursement': ('Outbound', 'Disbursement header'),
    'B2C Payment Disbursement Reference': ('Outbound', 'Allocation evidence'),
}


def first(row, *fields):
    return next((row[f] for f in fields if row.get(f) is not None and row[f] != ''), None)


def timestamp(value):
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=' ')
    text = str(value)
    try:
        return (datetime.strptime(text, '%Y%m%d%H%M%S') if text.isdigit()
                else datetime.fromisoformat(text)).isoformat(sep=' ')
    except ValueError:
        return None  # Exact original representation remains in source_payload.


def project(source_id, doctype, row, snapshot_id=None, run_id=None):
    if not isinstance(source_id, str) or not source_id or doctype not in TYPES or not row.get('name'):
        raise ValueError('Unsupported or unnamed legacy payment evidence.')
    payload = canonical(row)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    amount = first(row, 'amount', 'transamount', 'paid_amount', 'allocated_amount')
    if amount is not None:
        try:
            amount = Decimal(str(amount))
        except InvalidOperation:
            raise ValueError('Invalid historical payment amount.') from None
        if not amount.is_finite():
            raise ValueError('Invalid historical payment amount.')
    return {
        'doctype': DOCTYPE,
        'name': hashlib.sha256((str(source_id) + '\0' + doctype + '\0' + str(row['name']) + '\0' + digest).encode()).hexdigest(),
        'source_id': str(source_id), 'source_doctype': doctype, 'source_name': str(row['name']),
        'snapshot_id': snapshot_id, 'source_hash': digest, 'source_payload': payload, 'run_id': run_id,
        'source_owner': row.get('owner'), 'source_creation': row.get('creation'),
        'source_modified': row.get('modified'), 'source_modified_by': row.get('modified_by'),
        'source_docstatus': int(row.get('docstatus') or 0),
        'direction': TYPES[doctype][0], 'evidence_kind': TYPES[doctype][1],
        'original_status': first(row, 'status', 'payment_status') or '',
        'amount': str(amount) if amount is not None else None,
        'currency': first(row, 'currency', 'default_currency') or '',
        'receipt_number': first(row, 'transaction_id', 'transid', 'reference_no') or '',
        'checkout_request_id': row.get('checkout_request_id'), 'merchant_request_id': row.get('merchant_request_id'),
        'conversation_id': row.get('conversation_id'), 'originator_conversation_id': row.get('originator_conversation_id'),
        'transaction_on': timestamp(first(row, 'transaction_date', 'transaction_completed_datetime', 'transtime')),
        'source_reference_doctype': row.get('reference_doctype'), 'source_reference_name': row.get('reference_name'),
        'source_payment_entry': row.get('payment_entry'), 'source_company': row.get('company'),
        'account_reference': first(row, 'account_reference', 'billrefnumber'),
        'party_phone': first(row, 'phone_number', 'msisdn'), 'result_code': row.get('result_code'),
        'result_description': first(row, 'result_desc', 'result_description'),
        'mapping_status': 'Unresolved',
    }


def retain(snapshot, run_id, records):
    """Caller owns the transaction; imports never commit or invoke live payments."""
    import frappe
    source = snapshot['source']
    source_id = source.get('source_id') or source['db_name']
    created = reused = 0
    for item in records:
        if item['doctype'] not in TYPES:
            continue
        values = project(source_id, item['doctype'], item['record'],
                         source.get('capture_id') or source.get('snapshot_id'), run_id)
        if frappe.db.exists(DOCTYPE, values['name']):
            found = frappe.db.get_value(DOCTYPE, values['name'], ['source_hash', 'source_payload'], as_dict=True)
            if not found or any(found.get(field) != values[field] for field in ('source_hash', 'source_payload')):
                raise ValueError('Existing historical payment evidence failed its integrity check.')
            reused += 1
        else:
            if not frappe.db.exists('DocType', DOCTYPE):
                raise ValueError('Install the updated migration schema before retaining historical payments.')
            frappe.get_doc(values).insert(ignore_permissions=True, set_name=values['name'])
            created += 1
    return {'created': created, 'reused': reused}


def verify(snapshot):
    """Compare the searchable financial projection as well as its retained body."""
    import frappe
    history = snapshot.get('history', {})
    captured = any(name in snapshot.get('schemas', {}) for name in TYPES)
    if not captured and isinstance(history, dict):
        captured = any(item['doctype'] in TYPES for item in history.values())
    if not captured:
        return []
    source_id = snapshot['source'].get('source_id') or snapshot['source']['db_name']
    fields = ['name', 'source_hash', 'source_payload', 'receipt_number', 'original_status', 'amount', 'currency']
    existing = {row['name']: row for row in frappe.get_all(DOCTYPE, filters={'source_id': str(source_id)}, fields=fields)}
    failures = []
    for item in snapshot.get('history', {}).values():
        if item['doctype'] not in TYPES:
            continue
        expected = project(source_id, item['doctype'], item['record'])
        found = existing.get(expected['name'])
        if not found or any(str(found.get(f) or '') != str(expected.get(f) or '') for f in fields):
            failures.append({'source_doctype': item['doctype'], 'source_name': item['record']['name'],
                             'problem': 'Historical payment projection missing or changed'})
    return failures
