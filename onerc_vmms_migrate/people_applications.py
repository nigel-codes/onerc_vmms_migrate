"""Historical applications and recorded reasons, without acceptance or routing."""

import frappe
from onerc_vmms_migrate import historical
from onerc_vmms_migrate.history_model import application_state
from onerc_vmms_migrate.crosswalk import UNMAPPED, DROP


def decisions(row):
	# The old text establishes the decision/reason, not a stage, actor or time.
	return [
		{"decision": kind, "reason": row[field], "legacy_source_decision": 1}
		for field, kind in (("rejection_reason", "Rejected"), ("custom_remarks", "More info requested"))
		if row.get(field)
	]


def transfer(snapshot, bundles, crosswalks, run_id):
	report = {"created": 0, "existing": 0, "held": []}
	for bundle in bundles:
		profile = frappe.db.get_value(
			"Legacy Migration Record",
			{"source_doctype": "Person", "source_name": bundle["key"], "target_doctype": "Red Profile"},
			["target_name", "source_hash", "source_evidence_hash"],
			as_dict=True,
		)
		from onerc_vmms_migrate.people_run import unchanged

		if not profile or not unchanged(profile, bundle):
			continue
		for dt, name in bundle["sources"]:
			if dt != "Job Applicant":
				continue
			row = snapshot["applicants"][name]
			historical.archive(snapshot, run_id, records=[{"doctype": dt, "record": row}])
			target = frappe.db.get_value(
				"Legacy Migration Record",
				{"source_doctype": dt, "source_name": name, "target_doctype": "VMMS Volunteer Application"},
				"target_name",
			)
			if target:
				report["existing"] += 1
				historical.mark(snapshot, dt, name, "VMMS Volunteer Application", target, run_id)
				continue
			geo = crosswalks.lookup("branch", row.get("company"))
			if not geo or geo in (UNMAPPED, DROP):
				report["held"].append(
					{
						"source_doctype": dt,
						"source_name": name,
						"reason": "Application branch is unmapped; original retained",
					}
				)
				continue
			motivation = crosswalks.lookup("motivation", row.get("reason_to_join_krcs"))
			values = {
				"red_profile": profile["target_name"],
				"geo_node": geo,
				"approval_state": application_state(row),
				"approval_stage": "",
				"approval_stage_entered_on": None,
				"legacy_orphan_application": int(application_state(row) == "Approved"),
				"applied_on": row.get("creation"),
				"creation": row.get("creation"),
				"modified": row.get("modified"),
				"approval_decisions": decisions(row),
				"motivation": [{"motivation": motivation}]
				if motivation and motivation not in (UNMAPPED, DROP)
				else [],
			}
			doc = historical.insert("VMMS Volunteer Application", values, owner=row.get("owner"))
			frappe.get_doc(
				{
					"doctype": "Legacy Migration Record",
					"person_key": bundle["key"],
					"source_hash": bundle["source_hash"],
					"source_doctype": dt,
					"source_name": name,
					"target_doctype": doc.doctype,
					"target_name": doc.name,
					"outcome": "created",
					"run_id": run_id,
				}
			).insert(ignore_permissions=True)
			historical.mark(snapshot, dt, name, doc.doctype, doc.name, run_id)
			report["created"] += 1
	return report
