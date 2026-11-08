"""A mapper's own output is not sufficient evidence of native completeness."""

from collections import Counter
import tempfile
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate import run


def bundle(profile):
	return {
		"key": "person",
		"profile": profile,
		"errors": [],
		"warnings": [],
		"volunteer": None,
		"member": None,
		"memberships": [],
	}


class TestMappingReportReadiness(TestCase):
	def test_native_schema_failure_does_not_get_counted_as_ready(self):
		result = {
			"bundles": [bundle({})],
			"not_migrated": [],
			"mapping_issues": [
				{
					"person": "person",
					"severity": "error",
					"code": "text_too_long",
					"field": "Red Profile.vmms_education[1].maj_opt_subj",
				}
			],
		}
		cw = MagicMock()
		cw.problems.return_value = []
		cw.placeholder_hits = Counter()
		with (
			tempfile.TemporaryDirectory() as directory,
			patch.object(run.workspace, "report_dir", return_value=directory),
			patch.object(run.workspace, "write_json"),
			patch.object(run, "_say"),
		):
			summary = run._report_plan("test", cw, result)
		self.assertEqual(summary["ready"], 0)
		self.assertEqual(summary["mapping_errors"], 1)


class TestNativeFieldReconciliation(TestCase):
	def reconcile(self, expected, actual):
		database = MagicMock()
		database.get_value.return_value = frappe._dict(actual)
		ledger = frappe._dict(
			target_doctype="Red Profile", target_name="PROFILE", source_name="person", outcome="created"
		)
		with (
			tempfile.TemporaryDirectory() as directory,
			patch.object(run, "_run_id", return_value="test"),
			patch.object(run.workspace, "report_dir", return_value=directory),
			patch.object(run.workspace, "write_json"),
			patch.object(run, "_say"),
			patch.object(frappe, "get_all", return_value=[ledger]),
			patch.object(frappe, "db", database),
			patch.object(frappe, "get_doc", return_value=frappe._dict(actual)),
		):
			return run.reconcile([bundle(expected)])

	def test_wrong_blood_group_or_missing_zero_cannot_pass_existing_six_field_check(self):
		expected = {"email": "person@example.org", "vmms_blood_group": "O+", "vmms_number_of_dependants": 0}
		actual = {"email": "person@example.org", "vmms_blood_group": "A+"}
		self.assertEqual(self.reconcile(expected, actual)["mismatches"], 2)

	def test_dropped_education_grade_is_a_native_mismatch(self):
		expected = {
			"email": "person@example.org",
			"vmms_education": [{"institution": "School", "class_per": "A"}],
		}
		actual = {
			"email": "person@example.org",
			"vmms_education": [frappe._dict(institution="School", class_per="B")],
		}
		self.assertEqual(self.reconcile(expected, actual)["mismatches"], 1)
