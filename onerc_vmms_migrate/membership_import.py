"""Historical memberships retain their payment facts without new fee postings."""

ACCOUNTING_STATUS = "Historical"


def values(row):
	return {
		"membership_source": None,
		"accounting_status": ACCOUNTING_STATUS,
		"legacy_membership_id": row["source_name"],
		"legacy_status": row.get("legacy_status"),
		"legacy_paid": row.get("paid"),
		"legacy_is_existing_member": row.get("is_existing_member"),
		"legacy_qr_code": row.get("qr_code"),
		"approval_stage": "",
		"approval_stage_entered_on": None,
		"creation": row.get("creation"),
		"modified": row.get("modified"),
	}


def install_status():
	import frappe
	from frappe.custom.doctype.property_setter.property_setter import make_property_setter

	field = frappe.get_meta("VMMS Membership").get_field("accounting_status")
	options = (field.options or "").splitlines()
	if ACCOUNTING_STATUS not in options:
		make_property_setter(
			"VMMS Membership",
			"accounting_status",
			"options",
			"\n".join([*options, ACCOUNTING_STATUS]),
			"Text",
			validate_fields_for_doctype=False,
		)
