"""Installed fields, choice keys and native data limits are separate obligations."""

from unittest import TestCase

from onerc_vmms_migrate.mapping_validation import profile_issues


SCHEMA = {
	"Red Profile": {
		"vmms_access_to_internet": {"fieldtype": "Select", "options": "\nYes\nNo\nSometimes"},
		"vmms_number_of_dependants": {"fieldtype": "Int"},
		"vmms_blood_group": {"fieldtype": "Link", "options": "VMMS Blood Group"},
		"vmms_education": {"fieldtype": "Table", "options": "VMMS Education"},
		"vmms_supporting_documents": {"fieldtype": "Table", "options": "VMMS Supporting Document"},
	},
	"VMMS Education": {
		"institution": {"fieldtype": "Data", "reqd": 1},
		"maj_opt_subj": {"fieldtype": "Data"},
		"level": {"fieldtype": "Link", "options": "VMMS Education Level"},
	},
	"VMMS Supporting Document": {
		"type": {"fieldtype": "Link", "options": "Supporting Document Type"},
		"document_name": {"fieldtype": "Data"},
		"attachment": {"fieldtype": "Attach"},
	},
}
LOOKUPS = {
	"VMMS Blood Group": [{"name": "O+"}],
	"VMMS Education Level": [{"name": "graduate", "is_active": 1}, {"name": "old", "is_active": 0}],
	"Supporting Document Type": [{"name": "Other", "vmms_requires_document_name": 1}],
}


class TestMappingValidation(TestCase):
	def issues(self, profile, **kwargs):
		return profile_issues(profile, SCHEMA, LOOKUPS, **kwargs)

	def test_existing_fields_and_matching_keys_pass(self):
		self.assertEqual(
			self.issues(
				{
					"vmms_access_to_internet": "No",
					"vmms_number_of_dependants": 0,
					"vmms_blood_group": "O+",
					"vmms_education": [{"institution": "Original school", "level": "graduate"}],
				}
			),
			[],
		)

	def test_missing_destination_is_reported_by_exact_field_path(self):
		issue = self.issues({"vmms_linkedin": "https://example.org"})[0]
		self.assertEqual(issue["code"], "missing_target_field")
		self.assertEqual(issue["field"], "Red Profile.vmms_linkedin")

	def test_populated_native_field_still_fails_for_an_unsupported_select_value(self):
		self.assertEqual(self.issues({"vmms_access_to_internet": "Often"})[0]["code"], "invalid_select")

	def test_link_name_is_checked_independently_of_field_existence(self):
		self.assertEqual(self.issues({"vmms_blood_group": "Missing"})[0]["code"], "missing_link_target")

	def test_long_major_subjects_are_blocked_without_truncation(self):
		profile = {"vmms_education": [{"institution": "School", "maj_opt_subj": "x" * 141}]}
		issue = self.issues(profile)[0]
		self.assertEqual(issue["code"], "text_too_long")
		self.assertIn("vmms_education[1].maj_opt_subj", issue["field"])
		self.assertEqual(profile["vmms_education"][0]["maj_opt_subj"], "x" * 141)

	def test_required_child_fields_are_checked(self):
		self.assertEqual(
			self.issues({"vmms_education": [{"level": "graduate"}]})[0]["code"], "missing_required_value"
		)

	def test_inactive_choice_is_a_readback_review_issue_not_a_false_missing_link(self):
		issue = self.issues({"vmms_education": [{"institution": "School", "level": "old"}]})[0]
		self.assertEqual(issue["code"], "inactive_link_target")
		self.assertEqual(issue["severity"], "review")

	def test_empty_insurance_is_unanswered_and_populated_insurance_is_rejected(self):
		self.assertEqual(self.issues({"vmms_has_insurance": None}), [])
		self.assertEqual(self.issues({"vmms_has_insurance": "No"})[0]["code"], "approved_exclusion_violation")

	def test_supporting_document_name_is_checked_for_planned_attachments(self):
		profile = {"vmms_supporting_documents": [{"type": "Other"}]}
		files = [
			{
				"purpose": "table",
				"table": "vmms_supporting_documents",
				"index": 0,
				"url": "/private/files/source.pdf",
			}
		]
		issue = self.issues(profile, files=files)[0]
		self.assertEqual(issue["code"], "missing_document_name")

	def test_integer_overflow_is_detected_before_sql_can_reject_or_coerce_it(self):
		self.assertEqual(self.issues({"vmms_number_of_dependants": 2**40})[0]["code"], "integer_out_of_range")


class TestBatchMappingGate(TestCase):
	def test_preflight_blocks_a_present_field_with_an_invalid_choice(self):
		from unittest.mock import MagicMock, patch
		from onerc_vmms_migrate import batches, mapping_validation, run
		from onerc_vmms_migrate.crosswalk import Crosswalks

		snap = {"source": {"profile_mapping_version": 2, "continuity": {"version": 1, "tables": {}}}}
		built = {"bundles": [{"key": "person", "errors": [], "profile": {}}]}
		problem = {
			"person": "person",
			"field": "Red Profile.vmms_access_to_internet",
			"code": "invalid_select",
			"severity": "error",
		}
		cw = MagicMock()
		cw.problems.return_value = []
		with (
			patch.object(batches, "build", return_value=built),
			patch.object(Crosswalks, "read", return_value=cw),
			patch.object(run, "_report_plan", return_value={}),
			patch.object(mapping_validation, "installed_issues", return_value=[problem]),
		):
			result = batches.work(
				snap, None, {}, {"phase": "preflight", "offset": 0, "batch_size": 1, "run_id": "test"}, None
			)
		self.assertIn(problem, result[2])


class TestSourceTransformGate(TestCase):
	def test_preflight_blocks_an_invalid_source_claim_even_if_no_native_value_was_generated(self):
		from unittest.mock import MagicMock, patch
		from onerc_vmms_migrate import batches, mapping_validation, run
		from onerc_vmms_migrate.crosswalk import Crosswalks

		snap = {"source": {"profile_mapping_version": 2, "continuity": {"version": 1, "tables": {}}}}
		built = {"bundles": [{"key": "person", "errors": ["github is invalid"], "profile": {}}]}
		cw = MagicMock()
		cw.problems.return_value = []
		with (
			patch.object(batches, "build", return_value=built),
			patch.object(Crosswalks, "read", return_value=cw),
			patch.object(run, "_report_plan", return_value={}),
			patch.object(mapping_validation, "installed_issues", return_value=[]),
		):
			result = batches.work(
				snap, None, {}, {"phase": "preflight", "offset": 0, "batch_size": 1, "run_id": "test"}, None
			)
		self.assertTrue(any(row.get("error") == "github is invalid" for row in result[2]))
