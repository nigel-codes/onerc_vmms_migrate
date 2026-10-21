from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate import load, presentation


class TestPresentation(TestCase):
	def test_synthetic_row_is_identified_by_all_markers_not_just_admin(self):
		row = {
			"parenttype": "VMMS Volunteer Application",
			"parent": "A1",
			"parentfield": "approval_decisions",
			"stage": "legacy-import",
			"stage_label": "Imported from onerc_vmms",
			"approver": "Administrator",
			"reason": "Approved in onerc_vmms before migration (Employee E1, Employee E1).",
			"decision": "Approved",
		}
		self.assertTrue(presentation.synthetic(row))
		for field, value in [
			("stage", "county-review"),
			("stage_label", "County approval"),
			("approver", "actual@example.test"),
			("reason", "Original approval reason"),
		]:
			with self.subTest(field=field):
				self.assertFalse(presentation.synthetic({**row, field: value}))

	def test_native_approved_status_does_not_invent_an_approver_or_event(self):
		doc = frappe._dict(doctype="VMMS Volunteer Application", name="A1")
		with patch.object(frappe, "db", MagicMock()) as db, patch.object(frappe, "get_doc") as get_doc:
			load._mark_approved(doc)
			values = db.set_value.call_args.args[2]
			self.assertEqual(values["approval_state"], "Approved")
			self.assertEqual(values["approval_stage"], "")
			self.assertIsNone(values["approval_stage_entered_on"])
			get_doc.assert_not_called()

	def test_only_generated_notes_are_reworded_and_original_text_is_preserved(self):
		source = "Meeting with county team."
		old = source + presentation.OLD_DEPLOYMENT_SUFFIX
		self.assertEqual(
			presentation.clean_notes("VMMS Deployment", old), source + presentation.DEPLOYMENT_SUFFIX
		)
		self.assertEqual(presentation.clean_notes("VMMS Deployment", source), source)
		self.assertEqual(presentation.clean_notes("VMMS Terms of Reference", presentation.OLD_TERMS_NOTE), "")
		self.assertEqual(
			presentation.clean_notes("VMMS Terms of Reference", "Imported medical equipment"),
			"Imported medical equipment",
		)


class TestPresentationPatch(TestCase):
	def setup_patch(self):
		from onerc_vmms_migrate.patches import repair_generated_presentation as repair

		self.repair = repair
		self.db = self.enterContext(patch.object(frappe, "db", MagicMock()))
		self.enterContext(patch.object(frappe, "conf", frappe._dict()))
		self.enterContext(patch.object(repair.batch_browser, "assert_job_idle"))
		self.enterContext(patch.object(repair.browser, "assert_idle"))
		self.enterContext(patch.object(repair, "Guard"))
		self.enterContext(patch.object(frappe, "clear_document_cache"))
		self.enterContext(patch.object(repair.workspace, "report_dir", return_value="/tmp/private-audit"))
		self.enterContext(patch.object(repair.run, "_run_id", return_value="audit-run"))
		self.write = self.enterContext(patch.object(repair.workspace, "write_json"))
		self.link = frappe._dict(
			source_doctype="Employee", target_doctype=repair.APPLICATION, target_name="A1", outcome="created"
		)
		self.synthetic = frappe._dict(
			name="generated",
			parenttype=repair.APPLICATION,
			parent="A1",
			parentfield="approval_decisions",
			stage=presentation.OLD_STAGE,
			stage_label=presentation.OLD_LABEL,
			approver="Administrator",
			decision="Approved",
			reason="Approved in onerc_vmms before migration (Employee E1, Employee E1).",
		)
		self.real = frappe._dict(
			self.synthetic,
			name="real",
			stage="county-review",
			stage_label="County approval",
			reason="Verified documents.",
		)
		parent = frappe._dict(
			name="A1",
			approval_state="Approved",
			approval_stage=presentation.OLD_LABEL,
			approval_stage_entered_on="2026-10-20",
		)
		self.db.get_values.side_effect = lambda dt, *a, **k: (
			[self.synthetic, self.real] if dt == repair.DECISIONS else [parent]
		)
		self.enterContext(
			patch.object(
				frappe,
				"get_all",
				side_effect=lambda dt, **k: [self.link] if dt == "Legacy Migration Record" else [],
			)
		)

	def test_exact_generated_row_is_archived_before_removal_and_real_audit_is_untouched(self):
		self.setup_patch()
		sequence = []
		self.write.side_effect = lambda *a: sequence.append("audit")
		self.db.delete.side_effect = lambda *a: sequence.append("delete")
		self.repair.execute()
		self.assertEqual(sequence[0], "audit")
		self.db.delete.assert_called_once_with(self.repair.DECISIONS, {"name": ["in", ["generated"]]})
		self.db.set_value.assert_called_once_with(
			self.repair.APPLICATION,
			{"name": ["in", ["A1"]]},
			{"approval_stage": "", "approval_stage_entered_on": None},
			update_modified=False,
		)
		result = self.write.call_args.args[1]
		self.assertEqual(result["approval_states_changed"], 0)
		self.assertEqual(result["genuine_decisions_changed"], 0)
		self.assertEqual(result["generated_decisions_removed"], 1)

	def test_audit_export_failure_prevents_any_deletion(self):
		self.setup_patch()
		self.write.side_effect = OSError("disk full")
		with self.assertRaises(OSError):
			self.repair.execute()
		self.db.delete.assert_not_called()
		self.db.set_value.assert_not_called()

	def test_adopted_records_are_not_eligible_for_cleanup(self):
		from onerc_vmms_migrate.patches import repair_generated_presentation as repair

		row = {
			"source_doctype": "Employee",
			"target_doctype": repair.APPLICATION,
			"target_name": "A1",
			"outcome": "adopted",
		}
		self.assertEqual(repair.eligible([row]), set())
		row["outcome"] = "created"
		self.assertEqual(repair.eligible([row]), {(repair.APPLICATION, "A1")})
