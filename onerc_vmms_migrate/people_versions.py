"""Original Version diffs, linked only after their people exist in the ledger.

Payloads retain source field names and source-side credential redaction. They
are evidence of the old edit, never replayed as updates to a target document.
"""

import hashlib
import frappe
from onerc_vmms_migrate import historical

TARGETS = {
	"User": "User",
	"Employee": "VMMS Volunteer",
	"Job Applicant": "VMMS Volunteer Application",
	"VM Member": "VMMS Member",
	"VM Membership": "VMMS Membership",
}


def rows(snapshot):
	store = snapshot.get("history", {})
	candidates = (
		store.rows("Version")
		if hasattr(store, "rows")
		else (item["record"] for item in store.values() if item["doctype"] == "Version")
	)
	return (row for row in candidates if row.get("ref_doctype") in TARGETS)


def resolve(row):
	target = frappe.db.get_value(
		"Legacy Migration Record",
		{
			"source_doctype": row["ref_doctype"],
			"source_name": row["docname"],
			"target_doctype": TARGETS[row["ref_doctype"]],
		},
		["target_doctype", "target_name"],
		as_dict=True,
	)
	return target if target and frappe.db.exists(target["target_doctype"], target["target_name"]) else None


def name(source_id, source_name, target):
	return hashlib.sha256(
		(
			"Version\0"
			+ source_id
			+ "\0"
			+ source_name
			+ "\0"
			+ target["target_doctype"]
			+ "\0"
			+ target["target_name"]
		).encode()
	).hexdigest()[:32]


def values(row, target):
	return {
		**{field: row.get(field) for field in ("data", "owner", "creation", "modified", "modified_by")},
		"ref_doctype": target["target_doctype"],
		"docname": target["target_name"],
	}


def transfer(snapshot, selected, run_id):
	source_id = str(snapshot["source"].get("source_id") or snapshot["source"]["db_name"])
	report = {"created": 0, "existing": 0, "held": []}
	for row in selected:
		historical.archive(snapshot, run_id, records=[{"doctype": "Version", "record": row}])
		target = resolve(row)
		if not target:
			report["held"].append(
				{
					"source_doctype": "Version",
					"source_name": row["name"],
					"ref_doctype": row.get("ref_doctype"),
					"docname": row.get("docname"),
					"reason": "Person target is not imported; original Version retained for a later pass.",
				}
			)
			continue
		native = name(source_id, row["name"], target)
		if frappe.db.exists("Version", native):
			report["existing"] += 1
		else:
			historical.insert("Version", values(row, target), name=native, owner=row.get("owner"))
			report["created"] += 1
		historical.mark(snapshot, "Version", row["name"], "Version", native, run_id)
	return report


def page(snapshot, after, size):
	store = snapshot.get("history", {})
	if hasattr(store, "versions_page"):
		return store.versions_page(after, size, tuple(TARGETS))
	return sorted((row for row in rows(snapshot) if row["name"] > after), key=lambda r: r["name"])[:size]
