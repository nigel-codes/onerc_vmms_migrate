import json
from unittest import TestCase
from unittest.mock import patch, MagicMock

from onerc_vmms_migrate import people_versions


class TestPeopleVersions(TestCase):
	def test_targets_are_ledger_driven_and_payload_actor_and_dates_are_unchanged(self):
		row = {
			"name": "V1",
			"ref_doctype": "Employee",
			"docname": "VOL-26-1",
			"owner": "old@example.org",
			"creation": "2020-01-01",
			"modified": "2021-01-01",
			"modified_by": "cc@example.org",
			"data": json.dumps({"changed": [["cell_number", "0712 345 678", "0799"]]}),
		}
		target = {"target_doctype": "VMMS Volunteer", "target_name": "VOL-26-1"}
		values = people_versions.values(row, target)
		self.assertEqual(values["data"], row["data"])
		for key in ("owner", "creation", "modified", "modified_by"):
			self.assertEqual(values[key], row[key])
		self.assertEqual(values["ref_doctype"], "VMMS Volunteer")
		self.assertEqual(values["docname"], "VOL-26-1")
		self.assertNotEqual(
			people_versions.name("source-a", "V1", target), people_versions.name("source-b", "V1", target)
		)

	def test_missing_targets_are_retained_and_retried_after_the_person_exists(self):
		row = {"name": "V1", "ref_doctype": "Job Applicant", "docname": "JA-1", "data": "{}"}
		with (
			patch.object(people_versions, "resolve", return_value=None),
			patch.object(people_versions, "historical") as history,
		):
			result = people_versions.transfer({"source": {"source_id": "S"}}, [row], "R")
		self.assertEqual(result["held"][0]["source_name"], "V1")
		history.insert.assert_not_called()

	def test_retry_does_not_insert_a_second_version(self):
		target = {"target_doctype": "User", "target_name": "person@example.org"}
		with (
			patch.object(people_versions, "resolve", return_value=target),
			patch.object(people_versions, "frappe") as frappe,
			patch.object(people_versions, "historical") as history,
		):
			frappe.db.exists.return_value = True
			report = people_versions.transfer(
				{"source": {"source_id": "S"}},
				[{"name": "V1", "ref_doctype": "User", "docname": "old", "data": "{}"}],
				"R",
			)
		self.assertEqual(report["existing"], 1)
		history.insert.assert_not_called()
