"""Remove only receiver-generated approval events; preserve their private audit."""

import hashlib
import json
import os
import uuid

import frappe

from onerc_vmms_migrate import batch_browser, browser, historical, presentation, run, workspace
from onerc_vmms_migrate.history_model import key
from onerc_vmms_migrate.silence import Guard
from onerc_vmms_migrate.transaction import large_transaction

DECISIONS = "VMMS Approval Decision"
APPLICATION = "VMMS Volunteer Application"
MEMBERSHIP = "VMMS Membership"
NOTE_TYPES = ("VMMS Terms of Reference", "VMMS Deployment")


def eligible(links):
	return {
		(row["target_doctype"], str(row["target_name"]))
		for row in links
		if row.get("outcome") == "created"
		and (
			(row["target_doctype"] == APPLICATION and row["source_doctype"] in ("Employee", "Job Applicant"))
			or (row["target_doctype"] == MEMBERSHIP and row["source_doctype"] == "VM Membership")
		)
	}


def chunks(rows):
	for offset in range(0, len(rows), 1000):
		yield rows[offset : offset + 1000]


@large_transaction
def execute():
	if not frappe.db.table_exists(DECISIONS) or not frappe.db.table_exists("Legacy Migration Record"):
		return
	batch_browser.assert_job_idle()
	browser.assert_idle()
	guard = Guard()
	links = frappe.get_all(
		"Legacy Migration Record",
		filters={"target_doctype": ["in", [APPLICATION, MEMBERSHIP]], "outcome": "created"},
		fields=["source_doctype", "target_doctype", "target_name", "outcome"],
	)
	if not links and not frappe.db.count(historical.ARCHIVE, {"conversion_status": "Converted"}):
		return
	allowed = eligible(links)
	candidates = frappe.db.get_values(
		DECISIONS,
		{"stage": presentation.OLD_STAGE, "stage_label": presentation.OLD_LABEL, "approver": "Administrator"},
		fieldname=["*"],
		as_dict=True,
		for_update=True,
	)
	candidates = [
		row
		for row in candidates
		if presentation.synthetic(row) and (row.parenttype, str(row.parent)) in allowed
	]
	parents = {}
	for dt in (APPLICATION, MEMBERSHIP):
		names = sorted({row.parent for row in candidates if row.parenttype == dt})
		for group in chunks(names):
			for row in frappe.db.get_values(
				dt,
				{"name": ["in", group]},
				["name", "approval_state", "approval_stage", "approval_stage_entered_on"],
				as_dict=True,
				for_update=True,
			):
				parents[(dt, str(row.name))] = row
	decisions, held, stage_changes = [], [], []
	for row in candidates:
		parent = parents.get((row.parenttype, str(row.parent)))
		if not parent or parent.approval_state != row.decision:
			held.append(
				{
					"doctype": row.parenttype,
					"name": row.parent,
					"reason": "Current approval state differs from the generated source-status row",
				}
			)
			continue
		decisions.append(dict(row))
	selected_parents = {(d["parenttype"], str(d["parent"])) for d in decisions}
	for (dt, name), row in parents.items():
		if row.approval_stage == presentation.OLD_LABEL and (dt, name) in selected_parents:
			stage_changes.append(
				{
					"doctype": dt,
					"name": name,
					"before": {
						"approval_stage": row.approval_stage,
						"approval_stage_entered_on": row.approval_stage_entered_on,
					},
				}
			)

	# Notes are eligible only through retained conversion identities or proven derived links.
	converted = frappe.get_all(
		historical.ARCHIVE,
		filters={
			"conversion_status": "Converted",
			"target_doctype": ["in", [*NOTE_TYPES, "VMMS Deployment Assignment"]],
		},
		fields=["source_doctype", "source_name", "target_doctype", "target_name"],
	)
	notes_allowed = {
		(r.target_doctype, str(r.target_name)) for r in converted if r.target_doctype in NOTE_TYPES
	}
	deployments = {
		str(row.name): row
		for row in frappe.get_all("VMMS Deployment", fields=["name", "notes", "terms_of_reference"])
	}
	assignments = {
		str(row.name): row
		for row in frappe.get_all("VMMS Deployment Assignment", fields=["name", "deployment"])
	}
	file_terms = set()
	for row in converted:
		if row.source_doctype == "Deployment Request Tool" and row.target_doctype == "VMMS Deployment":
			deployment = deployments.get(str(row.target_name))
			if deployment:
				file_terms.add(
					(
						str(deployment.terms_of_reference),
						"legacy_file_" + key("Deployment Request Tool", row.source_name)[:24],
					)
				)
		if (
			row.source_doctype == "Personnel Deployment Request"
			and row.target_doctype == "VMMS Deployment Assignment"
		):
			assignment = assignments.get(str(row.target_name))
			derived = "legacy-assignment-" + key("assignment", row.source_name)[:24]
			if assignment and assignment.deployment == derived:
				notes_allowed.add(("VMMS Deployment", derived))
	terms = frappe.get_all(
		"VMMS Terms of Reference", fields=["name", "notes", "tor_key", "legacy_unstructured_terms"]
	)
	for row in terms:
		if row.legacy_unstructured_terms and (str(row.name), row.tor_key) in file_terms:
			notes_allowed.add(("VMMS Terms of Reference", str(row.name)))
	original_notes = {}
	for source in frappe.get_all(
		historical.ARCHIVE,
		filters={
			"conversion_status": "Converted",
			"source_doctype": "Personnel Terms of Reference",
			"target_doctype": "VMMS Terms of Reference",
		},
		fields=["target_name", "source_payload", "source_hash"],
	):
		if hashlib.sha256(source.source_payload.encode()).hexdigest() != source.source_hash:
			raise ValueError("Retained source terms changed; presentation cleanup was not applied.")
		text = json.loads(source.source_payload).get("notes")
		original_notes[str(source.target_name)] = text if isinstance(text, str) else ""
	note_changes = []
	for dt, rows in [("VMMS Terms of Reference", terms), ("VMMS Deployment", deployments.values())]:
		for row in rows:
			if (dt, str(row.name)) not in notes_allowed:
				continue
			changed = presentation.clean_notes(dt, row.notes)
			if dt == "VMMS Terms of Reference" and row.notes == presentation.OLD_TERMS_NOTE:
				changed = original_notes.get(str(row.name), changed)
			if changed != row.notes:
				note_changes.append({"doctype": dt, "name": row.name, "before": row.notes, "after": changed})
	if not decisions and not note_changes and not stage_changes and not held:
		return
	directory = workspace.report_dir(run._run_id("presentation-repair") + "-" + uuid.uuid4().hex[:8])
	# Audit export must succeed before any generated record is removed.
	workspace.write_json(
		os.path.join(directory, "presentation-repair-before.json"),
		{
			"generated_decisions": decisions,
			"stage_changes": stage_changes,
			"note_changes": note_changes,
			"held": held,
		},
	)
	for group in chunks([row["name"] for row in decisions]):
		frappe.db.delete(DECISIONS, {"name": ["in", group]})
	for dt in (APPLICATION, MEMBERSHIP):
		names = [row["name"] for row in stage_changes if row["doctype"] == dt]
		for group in chunks(names):
			frappe.db.set_value(
				dt,
				{"name": ["in", group]},
				{"approval_stage": "", "approval_stage_entered_on": None},
				update_modified=False,
			)
	for row in note_changes:
		current = frappe.db.get_value(row["doctype"], row["name"], "notes", for_update=True)
		if current != row["before"]:
			raise ValueError(
				"A presentation note changed while repair was running; no unreviewed text was overwritten."
			)
		frappe.db.set_value(row["doctype"], row["name"], "notes", row["after"], update_modified=False)
	for dt, name in {(row["parenttype"], row["parent"]) for row in decisions} | {
		(row["doctype"], row["name"]) for row in note_changes
	}:
		frappe.clear_document_cache(dt, name)
	guard.check()
	workspace.write_json(
		os.path.join(directory, "presentation-repair-result.json"),
		{
			"generated_decisions_removed": len(decisions),
			"stage_labels_cleared": len(stage_changes),
			"notes_reworded": len(note_changes),
			"held": len(held),
			"approval_states_changed": 0,
			"genuine_decisions_changed": 0,
			"status": "applied_in_patch_transaction",
		},
	)
