"""Keep technical provenance private; never turn status into a made-up decision."""

import re

OLD_STAGE = "legacy-import"
OLD_LABEL = "Imported from onerc_vmms"
OLD_TERMS_NOTE = "Imported legacy terms; original wording and state retained in Legacy VMMS Document."
OLD_DEPLOYMENT_SUFFIX = (
	"\nImported legacy deployment; no lifecycle status was recorded. Planned is the destination default."
)
DEPLOYMENT_SUFFIX = "\nStatus was not recorded. Planned is the current default."
OLD_FILE_TERMS_NOTE = "Legacy terms were supplied as a file or URL. Original structured wording and approval were not recorded. This draft is retained as evidence and is inactive for new work."
FILE_TERMS_NOTE = "Terms are supplied as an attached document. A structured approval record is unavailable. Not available for new work."
ASSIGNMENT_NOTE = "Original deployment details are unavailable. Dates and scope follow the recorded assignment and terms. Status was not recorded; Planned is the current default."


def synthetic(row):
	"""Recognize only the exact synthetic approval rows written by older receivers."""
	if (
		row.get("parenttype") not in ("VMMS Volunteer Application", "VMMS Membership")
		or row.get("parentfield") != "approval_decisions"
		or row.get("stage") != OLD_STAGE
		or row.get("stage_label") != OLD_LABEL
		or row.get("approver") != "Administrator"
	):
		return False
	reason = str(row.get("reason") or "")
	if row["parenttype"] == "VMMS Volunteer Application":
		return row.get("decision") == "Approved" and bool(
			re.fullmatch(r"Approved in onerc_vmms before migration \(.+\)\.", reason)
		)
	return row.get("decision") in ("Approved", "Rejected") and bool(
		re.fullmatch(
			r"Recorded historical status in onerc_vmms; this timestamp records the import, not the original decision \(VM Membership .+\)\.",
			reason,
		)
	)


def clean_notes(doctype, value):
	if doctype == "VMMS Terms of Reference":
		if value == OLD_TERMS_NOTE:
			return ""
		if value == OLD_FILE_TERMS_NOTE:
			return FILE_TERMS_NOTE
	if doctype == "VMMS Deployment" and isinstance(value, str):
		if value.endswith(OLD_DEPLOYMENT_SUFFIX):
			return value[: -len(OLD_DEPLOYMENT_SUFFIX)] + DEPLOYMENT_SUFFIX
		if re.fullmatch(
			r"Derived historical container for legacy assignment .+\. The original parent deployment was absent\. Dates and scope come from its recorded assignment and terms; Planned is the destination default\.",
			value,
		):
			return ASSIGNMENT_NOTE
	return value
