"""Fields for legacy document evidence that has no structured modern equivalent."""
import frappe


def after_install():
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
    create_custom_fields({'VMMS Professional Reference':[
        {'fieldname':'legacy_email','fieldtype':'Small Text','label':'Legacy email requiring review',
         'read_only':1,'insert_after':'email'},
    ],'VMMS Education':[
        {'fieldname':'legacy_institution','fieldtype':'Small Text','label':'Original imported institution text',
         'read_only':1,'insert_after':'institution'},
        {'fieldname':'legacy_qualification','fieldtype':'Small Text','label':'Original imported qualification text',
         'read_only':1,'insert_after':'qualification'},
    ],'Red Profile':[
        {'fieldname':'legacy_phone','fieldtype':'Small Text','label':'Original imported phone',
         'read_only':1,'insert_after':'phone'},
        {'fieldname':'legacy_untyped_id_number','fieldtype':'Data','label':'Legacy identifier (type not recorded)',
         'read_only':1,'insert_after':'identifications',
         'description':'Original identifier preserved without claiming an ID type or verified identity.'},
        {'fieldname':'legacy_identity_claims','fieldtype':'Code','options':'JSON',
         'label':'Legacy identity claims requiring review','read_only':1,'insert_after':'legacy_untyped_id_number',
         'description':'Conflicting source claims are retained here without declaring a primary identity or merging accounts.'},
    ],'VMMS Terms of Reference':[
        {'fieldname':'legacy_unstructured_terms','fieldtype':'Check','label':'Legacy terms supplied as a document',
         'read_only':1,'insert_after':'notes','description':'Original attachment or URL retained. Structured terms and approval were not recorded by the old deployment tool.'},
        {'fieldname':'legacy_terms_attachment','fieldtype':'Attach','label':'Original legacy terms attachment',
         'read_only':1,'insert_after':'legacy_unstructured_terms'},
        {'fieldname':'legacy_terms_url','fieldtype':'Small Text','label':'Original legacy terms URL',
         'read_only':1,'insert_after':'legacy_terms_attachment'},
    ],'Event Booking':[
        {'fieldname':'legacy_unclaimed_booking','fieldtype':'Check','label':'Legacy booking without an account',
         'read_only':1,'insert_after':'user','description':'No account was recorded in the old booking. No guest or unrelated account receives ownership.'},
        {'fieldname':'legacy_primary_contact','fieldtype':'Data','label':'Original booking contact',
         'read_only':1,'insert_after':'legacy_unclaimed_booking'},
    ]},update=True)

    if frappe.db.get_single_value('VMMS Migration Settings','restore_email_link_login') is None:
        frappe.db.set_single_value('VMMS Migration Settings','restore_email_link_login',1)
