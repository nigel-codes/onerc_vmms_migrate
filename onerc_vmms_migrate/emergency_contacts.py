"""Resolve actual relationship names, including inactive historical words."""
UNKNOWN_RELATIONSHIP = "Not recorded"  # A valid source word, never a fallback.


def prepare(contacts, review=None):
    import frappe
    for index,row in enumerate(contacts,1):
        value=row.get("relationship")
        if not value:continue
        # MariaDB's case-insensitive lookup returns the stored canonical name.
        actual=frappe.db.exists("VMMS Relationship",value)
        if actual:
            row["relationship"]=actual
        else:
            if review:review(f"emergency_contacts[{index}].relationship",value)
            row["relationship"]=None
