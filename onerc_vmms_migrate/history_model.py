"""Pure access to a full business snapshot and explicit historical mappings."""
from collections import defaultdict
import hashlib
import json


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), default=str)


def key(doctype, name):
    return hashlib.sha256((str(doctype) + '\0' + str(name)).encode()).hexdigest()


class History:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.schemas = snapshot.get('schemas', {})
        self.store=snapshot.get('history',{})
        if hasattr(self.store,'rows'):
            from onerc_vmms_migrate.history_store import Tables
            self.tables=Tables(self.store,self.schemas)
            return
        self.tables = defaultdict(dict)
        self.child_rows = defaultdict(list)
        for item in snapshot.get('history', {}).values():
            dt, row = item['doctype'], item['record']
            self.tables[dt][row['name']] = row
            if row.get('parenttype') and row.get('parentfield'):
                self.child_rows[(row['parenttype'], row['parent'], row['parentfield'])].append(row)
        for rows in self.child_rows.values():
            rows.sort(key=lambda r: (int(r.get('idx') or 0), r['name']))

    def rows(self, doctype):
        return list(self.tables[doctype].values())

    def get(self, doctype, name):
        return self.tables[doctype].get(name)

    def children(self, doctype, name, field):
        if hasattr(self.store,'children'):
            return self.store.children(doctype,name,field)
        return self.child_rows.get((doctype, name, field), [])


def split_name(full_name):
    parts = str(full_name or '').strip().split(None, 1)
    return (parts[0], parts[1] if len(parts) > 1 else '') if parts else ('', '')


def membership_state(row, paid=False):
    if int(row.get('docstatus') or 0) == 2:
        return 'Cancelled', 'Withdrawn'
    status = row.get('status') or 'Draft'
    mapping = {'Draft': ('Draft', 'Draft'),
               'Pending': ('Awaiting Approval' if paid else 'Awaiting Payment', 'Submitted'),
               'Rejected': ('Cancelled', 'Rejected'),
               'Active': ('Active', 'Approved'), 'Expired': ('Expired', 'Approved')}
    if status not in mapping:
        raise ValueError('Unsupported legacy membership status: ' + str(status))
    return mapping[status]


def assignment_state(row):
    if int(row.get('docstatus') or 0)==2:
        return 'Withdrawn'
    mapping = {'Pending': 'Pending', 'Accepted': 'Accepted', 'Rejected': 'Declined', 'Cancelled': 'Withdrawn'}
    status = row.get('deployment_status')
    if status not in mapping:
        raise ValueError('Unsupported legacy assignment status: ' + str(status))
    return mapping[status]


def application_state(row):
    if int(row.get('docstatus') or 0) == 2:
        return 'Withdrawn'
    status = str(row.get('status') or 'Open').casefold()
    return {'open': 'Draft', 'replied': 'In Review', 'accepted': 'Approved',
            'rejected': 'Rejected', 'hold': 'In Review', 'pending': 'Submitted'}.get(status, 'Draft')
