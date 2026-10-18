"""Build migration geography in batches while preserving native validations."""
from collections import defaultdict
from contextlib import contextmanager

import frappe


def boundaries(rows, parent_field):
    """Pure nested-set index for a forest; reject orphans and cycles."""
    names={row['name'] for row in rows}
    children=defaultdict(list)
    for row in rows:
        parent=row.get(parent_field) or None
        if parent and parent not in names:raise ValueError('A geographic tree has a missing parent.')
        children[parent].append(row['name'])
    values={}
    active=set()
    sequence=1
    def visit(name):
        nonlocal sequence
        if name in active:raise ValueError('A geographic tree contains a cycle.')
        active.add(name)
        left=sequence;sequence+=1
        for child in sorted(children[name]):visit(child)
        values[name]={'lft':left,'rgt':sequence};sequence+=1
        active.remove(name)
    for root in sorted(children[None]):visit(root)
    if set(values)!=names:raise ValueError('A geographic tree contains unreachable nodes or a cycle.')
    return values


@contextmanager
def batched_trees():
    """Defer index moves only in this process; rebuild before any commit.

    Geo Node and Cost Center controllers, parent checks and finance mirroring
    still run. Reindexing once avoids quadratic nested-set rewrites for 8,000
    imported residence nodes and their cost centers. Other workers are unchanged.
    """
    from frappe.utils import nestedset
    original=nestedset.update_nsm
    changed=set()
    parents={'Geo Node':'parent_geo_node','Cost Center':'parent_cost_center'}
    def deferred(doc):
        if doc.doctype not in parents:return original(doc)
        changed.add(doc.doctype)
        field=parents[doc.doctype]
        doc.old_parent=doc.get(field) or ''
        frappe.db.set_value(doc.doctype,doc.name,'old_parent',doc.old_parent,update_modified=False)
    nestedset.update_nsm=deferred
    try:
        yield
        for doctype in sorted(changed):
            rows=frappe.get_all(doctype,fields=['name',parents[doctype]])
            values=boundaries(rows,parents[doctype])
            frappe.db.bulk_update(doctype,values,update_modified=False)
            frappe.clear_document_cache(doctype)
    finally:
        nestedset.update_nsm=original
