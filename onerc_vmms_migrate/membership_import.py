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


class HistoricalMembershipMixin:
	def _get_missing_mandatory_fields(self):
		missing = super()._get_missing_mandatory_fields()
		if not any(row[0] == "membership_source" for row in missing):
			return missing
		if not self.get("legacy_membership_id") or self.get("accounting_status") != ACCOUNTING_STATUS:
			return missing
		import frappe

		trusted = bool(frappe.flags.vmms_migration_import)
		if not trusted and not self.is_new():
			stored = frappe.db.get_value(
				self.doctype,
				self.name,
				["membership_source", "legacy_membership_id", "accounting_status"],
				as_dict=True,
			)
			trusted = bool(
				stored
				and not stored.get("membership_source")
				and stored.get("legacy_membership_id") == self.get("legacy_membership_id")
				and stored.get("accounting_status") == ACCOUNTING_STATUS
				and frappe.db.exists(
					"Legacy Migration Record",
					{
						"source_doctype": "VM Membership",
						"target_doctype": self.doctype,
						"target_name": self.name,
						"outcome": "created",
					},
				)
			)
		# Blank means no modern gateway/proof claim was recorded. Accounting's
		# Historical marker independently excludes the original fee from posting.
		return [row for row in missing if row[0] != "membership_source"] if trusted else missing
