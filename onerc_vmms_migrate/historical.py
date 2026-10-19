"""Narrow, audited insert seam for facts already recorded by legacy systems.

Run native schema/type/mandatory/link validation, but never today's lifecycle
controllers. Inserting an old paid booking must not charge it again, generate
another ticket, change its dates, notify a person, or post financial entries.
Only the explicit operational doctype allowlist below can use this seam.
"""
import hashlib
import json
import uuid

import frappe
from frappe.utils import now_datetime

from onerc_vmms_migrate.history_model import canonical, key

ARCHIVE = 'Legacy VMMS Document'
ALLOWED = {'VMMS Membership', 'VMMS Volunteer Application', 'VMMS Terms of Reference',
           'VMMS Deployment', 'VMMS Deployment Request', 'VMMS Deployment Assignment',
           'VMMS Availability Schedule', 'VMMS Certification', 'VMMS Time Log',
           'VMMS Task', 'VMMS Event Attendance', 'Project', 'Task', 'Event',
           'Event Category', 'Event Host', 'Event Venue', 'Buzz Event', 'Event Ticket Type',
           'Event Booking', 'Event Ticket', 'Event Check In', 'Buzz Custom Field'}


def insert(doctype, values, name=None):
    if doctype not in ALLOWED:
        raise ValueError('Historical insertion is not allowed for this doctype.')
    doc = frappe.new_doc(doctype)
    doc.update(values)
    doc._action = "save"
    if name:
        doc.name = name
    else:
        from frappe.model.naming import set_new_name
        set_new_name(doc)
    # Child rows were attached while the parent had no final name yet.
    # Populate the native parent links before mandatory/link validation.
    doc.set_parent_in_children()
    if frappe.db.exists(doctype, doc.name):
        raise ValueError(f'{doctype} already contains this record; resolve the collision before importing.')
    doc.owner = 'Administrator'
    doc.creation = values.get('creation') or now_datetime()
    doc.modified = values.get('modified') or doc.creation
    doc.modified_by = 'Administrator'
    doc.docstatus = int(values.get('docstatus') or 0)
    doc.flags.in_insert = True
    # Cancelled historical dependencies remain valid *references*. Enforce
    # existence for every link; do not revive a cancelled document or offer it
    # for new work merely to preserve a past assignment or booking.
    for linked in [doc,*doc.get_all_children()]:
        invalid,_cancelled=linked.get_invalid_links()
        if invalid:
            raise frappe.LinkValidationError('A historical record references a missing destination document.')
    unclaimed=doctype=='Event Booking' and doc.get('legacy_unclaimed_booking') and not doc.get('user')
    if unclaimed:
        # Validate every other required field without inventing a booker. This
        # explicit legacy exception stores NULL ownership: it never grants all
        # Guest sessions access to someone else's booking. A later claim must
        # establish the correct account before normal edits can save the row.
        doc.user='Administrator'
    try:doc._validate()
    finally:
        if unclaimed:doc.user=None
    doc.db_insert()
    for child in doc.get_all_children():
        child.name = child.name or uuid.uuid4().hex[:10]
        child.parent = doc.name
        child.parenttype = doc.doctype
        child.owner = doc.owner
        child.creation = doc.creation
        child.modified = doc.modified
        child.modified_by = doc.modified_by
        child.docstatus = doc.docstatus
        child.db_insert()
    return doc


def archive_id(source_id, doctype, name):
    return hashlib.sha256((source_id + '\0' + doctype + '\0' + name).encode()).hexdigest()


def archive(snapshot, run_id):
    """Retain every captured row, including fields without an operational home.

    This is retention, counted separately from native conversion in all reports.
    An archive is immutable per source record version; later captures retain
    earlier versions as well, rather than overwriting the audit trail.
    """
    source_id = str(snapshot['source'].get('source_id') or snapshot['source']['db_name'])
    created = reused = 0
    existing=set(frappe.get_all(ARCHIVE,pluck='name'))
    pending=[]
    columns=['name','creation','modified','owner','modified_by','docstatus','idx','source_id','record_id',
        'source_doctype','source_name','snapshot_id','source_hash','source_payload','run_id','conversion_status',
        'imported_on','legacy_parenttype','legacy_parent','legacy_parentfield']
    imported_on=now_datetime()
    from itertools import chain
    records = chain(snapshot.get('history', {}).values(), ({'doctype':'User','record':u} for u in snapshot.get('users',{}).values()))
    total=len(snapshot.get('history',{}))+len(snapshot.get('users',{}))
    # Users use the safe person allowlist. No authentication or API secrets.
    for item in records:
        dt, row = item['doctype'], item['record']
        payload = canonical(row)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        record_id = archive_id(source_id, dt, row['name'])
        name = hashlib.sha256((record_id + digest).encode()).hexdigest()
        if name in existing:
            reused += 1
            continue
        pending.append((name,imported_on,imported_on,'Administrator','Administrator',0,0,
            source_id,record_id,dt,str(row['name']),
            snapshot['source'].get('capture_id') or snapshot['source'].get('snapshot_id'),
            digest,payload,run_id,'Retained',imported_on,row.get('parenttype'),row.get('parent'),row.get('parentfield')))
        created += 1
        if len(pending)>=1000:
            frappe.db.bulk_insert(ARCHIVE,columns,pending)
            pending=[]
    if pending:frappe.db.bulk_insert(ARCHIVE,columns,pending)
    return {'created': created, 'reused': reused, 'total': total}


def mark(snapshot, source_doctype, source_name, target_doctype, target_name, run_id):
    source_id = str(snapshot['source'].get('source_id') or snapshot['source']['db_name'])
    frappe.db.set_value(ARCHIVE, {'record_id': archive_id(source_id, source_doctype, source_name)},
        {'target_doctype': target_doctype, 'target_name': target_name,
         'conversion_status': 'Converted'}, update_modified=False)
    return target_name


def converted(snapshot, doctype, name):
    source_id = str(snapshot['source'].get('source_id') or snapshot['source']['db_name'])
    rows = frappe.get_all(ARCHIVE, filters={'record_id': archive_id(source_id, doctype, name),
        'conversion_status': 'Converted'}, fields=['target_doctype','target_name'], order_by='creation desc', limit=1)
    if rows and frappe.db.exists(rows[0].target_doctype, rows[0].target_name):
        return rows[0].target_name
    return None
