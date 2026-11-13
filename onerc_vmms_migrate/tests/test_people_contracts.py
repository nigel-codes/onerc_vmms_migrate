import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch
import frappe
from onerc_vmms_migrate import (
	extract,
	reference,
	people_applications,
	people_reports,
	import_review,
	people_run,
	load,
)
from onerc_vmms_migrate.history_model import History
from onerc_vmms_migrate.tests.test_transform import snapshot, volunteer_employee, crosswalks, CONF, TODAY
from onerc_vmms_migrate import transform


class TestPeopleContracts(TestCase):
	def test_full_capture_enriches_missing_operational_columns_without_replacing_explicit_values(self):
		snap = snapshot(
			members={"M": {"name": "M", "customer_name": "Recorded customer"}},
			memberships={"MS": {"name": "MS"}},
			history={
				"1": {
					"doctype": "VM Member",
					"record": {"name": "M", "customer_name": "Other", "modified": "2020-01-01"},
				},
				"2": {
					"doctype": "VM Membership",
					"record": {"name": "MS", "qr_code": "old QR", "paid": 1, "is_existing_member": 1},
				},
			},
		)
		extract.enrich(snap)
		self.assertEqual(snap["members"]["M"]["customer_name"], "Recorded customer")
		self.assertEqual(snap["members"]["M"]["modified"], "2020-01-01")
		self.assertEqual(snap["memberships"]["MS"]["qr_code"], "old QR")
		self.assertEqual(snap["memberships"]["MS"]["paid"], 1)

	def test_decisions_keep_reasons_without_fabricating_actors_stages_or_dates(self):
		rows = people_applications.decisions(
			{"rejection_reason": "Original rejection", "custom_remarks": "Send your document"}
		)
		self.assertEqual([r["decision"] for r in rows], ["Rejected", "More info requested"])
		self.assertEqual(rows[1]["reason"], "Send your document")
		self.assertTrue(all(not any(f in r for f in ("approver", "stage", "decided_on")) for r in rows))

	def test_unapproved_existing_status_translations_are_flagged_with_counts(self):
		rows = people_reports.status_rows(
			snapshot(
				applicants={
					"A": {"name": "A", "status": "Replied"},
					"B": {"name": "B", "status": "Open", "docstatus": 1},
				}
			),
			{"bundles": []},
		)
		self.assertEqual(sum(r["count"] for r in rows), 2)
		self.assertTrue(all(r["review"].startswith("Approval needed") for r in rows))

	def test_changed_email_uses_immutable_source_identity_and_never_inserts_a_second_person(self):
		bundle = {
			"key": "new@example.org",
			"sources": [("Employee", "VOL-1")],
			"source_hash": "new",
			"errors": [],
		}
		prior = {"source_hash": "old"}
		with (
			patch.object(
				load, "already_migrated", side_effect=lambda key: prior if key == "old@example.org" else None
			),
			patch.object(frappe, "db", MagicMock()) as db,
			patch.object(load, "load_bundle") as insert,
		):
			db.get_value.return_value = {"person_key": "old@example.org", "source_hash": "old"}
			result = people_run.import_people({}, None, {}, {"run_id": "R"}, [bundle])
		insert.assert_not_called()
		self.assertEqual(len(result["changed"]), 1)

	def test_hq_uses_seeded_national_and_regions_match_without_prefix(self):
		companies = [
			{"name": "Kenya Red Cross Society", "organisation_type": "Company"},
			{"name": "KRCS - HQ", "organisation_type": "Branch", "parent_company": "Kenya Red Cross Society"},
			{
				"name": "KRCS Coast Region",
				"organisation_type": "Region",
				"parent_company": "Kenya Red Cross Society",
			},
		]
		history = History(
			snapshot(history={str(i): {"doctype": "Company", "record": r} for i, r in enumerate(companies)})
		)
		db = MagicMock()
		db.exists.return_value = True
		db.get_value.side_effect = lambda dt, name, field=None: (
			None if isinstance(name, dict) else {"ROOT": "national", "COAST": "region"}.get(name)
		)

		def nodes(dt, **kwargs):
			return [
				{"name":"ROOT", "geo_node_name":"Kenya Red Cross Society", "geo_level":"national", "parent_geo_node":None, "geo_code":None},
				{"name":"COAST", "geo_node_name":"Coast Region", "geo_level":"region", "parent_geo_node":"ROOT", "geo_code":None},
			]

		mapping = MagicMock()
		report = {"errors": []}
		with (
			patch.object(frappe, "db", db),
			patch.object(frappe, "get_all", side_effect=nodes),
			patch.object(frappe, "get_doc") as create,
		):
			reference.geography(history, mapping, report)
		self.assertEqual(report["errors"], [])
		self.assertIn(("branch", "KRCS - HQ", "ROOT"), [call.args for call in mapping.call_args_list])
		self.assertIn(
			("branch", "KRCS Coast Region", "COAST"), [call.args for call in mapping.call_args_list]
		)
		create.assert_not_called()

	def test_prepare_never_creates_skills(self):
		snap = snapshot(
			users={
				"u": {
					"name": "u",
					"skill": [{"skill_name": "source skill"}],
					"additional_skills": [{"additional_skill": "source additional"}],
				}
			}
		)
		from contextlib import nullcontext

		with (
			patch.object(reference.Crosswalks, "read", return_value=crosswalks()),
			patch.object(reference.Crosswalks, "write"),
			patch.object(reference, "ensure") as ensure,
			patch.object(reference, "geography"),
			patch("onerc_vmms_migrate.trees.batched_trees", return_value=nullcontext()),
			patch.object(frappe, "db", MagicMock()),
		):
			reference.prepare(snap)
		self.assertFalse(any(call.args[0] == "VMMS Skill" for call in ensure.call_args_list))

	def test_missing_motivation_and_original_phone_forms_are_reported_with_values(self):
		b = transform.build(
			snapshot(
				employees={"E": volunteer_employee("E")},
				applicants={
					"A": {
						"name": "A",
						"email_id": "amina@example.org",
						"status": "Accepted",
						"reason_to_join_krcs": "Humanitarian",
					}
				},
			),
			crosswalks(motivation={"Humanitarian": "legacy-humanitarian"}),
			CONF,
			TODAY,
		)["bundles"][0]
		self.assertEqual(b["application"]["motivation"], [{"motivation": "legacy-humanitarian"}])
		self.assertFalse(b["application"]["created_by_migration"])
		self.assertIn("0712 345 678", json.dumps(b["_review_values"]))


class Required:
	def _get_missing_mandatory_fields(self):
		return [("primary_phone", "required"), ("relationship", "required")]


class Contact(import_review.ImportedContactMixin, Required):
	doctype = "VMMS Emergency Contact"
	name = "c1"
	parenttype = "VMMS Volunteer"
	parent = "VOL-1"

	def is_new(self):
		return True


class TestNarrowExceptions(TestCase):
	def test_only_internal_import_allows_new_contact_with_missing_fields(self):
		with patch.object(frappe, "flags", frappe._dict(vmms_migration_import=True)):
			self.assertEqual(Contact()._get_missing_mandatory_fields(), [])
		with patch.object(frappe, "flags", frappe._dict(vmms_migration_import=False)):
			self.assertEqual(len(Contact()._get_missing_mandatory_fields()), 2)


class TestCaptureAndFilesCompatibility(TestCase):
	def test_enrichment_keeps_preloaded_people_hash_and_tracks_new_evidence_separately(self):
		snap = snapshot(
			employees={"E": volunteer_employee("E")},
			history={
				"e": {
					"doctype": "Employee",
					"record": {"name": "E", "modified": "2026-11-01", "bank_name": "Recorded bank"},
				}
			},
		)
		before = transform.build(snap, crosswalks(), CONF, TODAY)["bundles"][0]
		extract.enrich(snap)
		after = transform.build(snap, crosswalks(), CONF, TODAY)["bundles"][0]
		self.assertEqual(before["source_hash"], after["source_hash"])
		self.assertNotEqual(before["source_evidence_hash"], after["source_evidence_hash"])
		self.assertTrue(people_run.unchanged({"source_hash": before["source_hash"]}, after))
		self.assertFalse(
			people_run.unchanged(
				{
					"source_hash": before["source_hash"],
					"source_evidence_hash": before["source_evidence_hash"],
				},
				after,
			)
		)

	def test_direct_db_file_phase_is_refused_with_a_browser_actionable_reason(self):
		from onerc_vmms_migrate import full_run

		with self.assertRaisesRegex(ValueError, "verified Bulk Export capture"):
			full_run.attachments({}, object(), "r")
