"""Represent a missing legacy relationship without guessing how people are related."""

UNKNOWN_RELATIONSHIP = "Not recorded"


def prepare(contacts):
    if not any(row.get("relationship") == UNKNOWN_RELATIONSHIP for row in contacts):
        return
    import frappe

    field = frappe.get_meta("VMMS Emergency Contact").get_field("relationship")
    if field and field.fieldtype == "Data":
        return
    if not field or field.fieldtype != "Link" or field.options != "VMMS Relationship":
        raise ValueError("The installed emergency-contact relationship field needs an explicit migration mapping.")
    if not frappe.db.exists(field.options, UNKNOWN_RELATIONSHIP):
        frappe.get_doc({
            "doctype": field.options,
            "relationship_name": UNKNOWN_RELATIONSHIP,
            "is_active": 0,
            "sequence": 9999,
            "description": "The legacy source did not record this contact's relationship. Retained for historical migration; ask the person to update it. Hidden from new application choices.",
        }).insert(ignore_permissions=True)
