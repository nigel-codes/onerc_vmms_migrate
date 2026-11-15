"""Project placement, live-capture numbering and truthful participation links."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate.history_load import Loader
from onerc_vmms_migrate.history_model import History
from onerc_vmms_migrate.naming_continuity import NamingContinuity


def history(**tables):
    return History({"history": {
        f"{doctype}:{row['name']}": {"doctype": doctype, "record": row}
        for doctype, rows in tables.items() for row in rows
    }})


class TestProjectMapping(TestCase):
    def setUp(self):
        self.loader = object.__new__(Loader)
        self.loader.history = history(Project=[{
            "name": "PROJ-0093", "project_name": "Recorded project",
            "company": "KRCS Murang'a", "status": "Completed",
            "expected_start_date": "2025-06-01", "owner": "creator@example.test",
        }])
        self.loader.snapshot = {"source": {"continuity": {"naming": {
            "series": [{"name": "PROJ-", "current": 118}]
        }}}}
        self.loader.naming = NamingContinuity(self.loader.snapshot)
        self.loader.warnings = []
        self.loader.conf = {}
        self.loader.crosswalks = MagicMock()
        self.loader.crosswalks.lookup.return_value = "GEO-00030"
        self.values = {}

        def remember(source_dt, row, target_dt, build, name=None, **kwargs):
            self.values[name or row["name"]] = build(row)

        self.loader.one = remember
        self.db = MagicMock()
        self.db.exists.side_effect = lambda dt, value: value if dt in ("Company", "Geo Node") else None
        self.enterContext(patch.object(frappe, "db", self.db))
        self.enterContext(patch(
            "vmmsx.deployment.services.project.default_company", return_value="Destination legal company"
        ))
        self.enterContext(patch.object(frappe, "get_all", side_effect=lambda dt, **kw:
            ["PROJ-0400"] if dt == "Project" else []))
        self.meta = SimpleNamespace(
            autoname="naming_series:",
            has_field=lambda name: name in ("vmms_geo_node", "department", "expected_start_date"),
            get_field=lambda name: SimpleNamespace(options="PROJ-.####", default="PROJ-.####"),
            fields=[frappe._dict(fieldname=n, fieldtype=t, options=o, reqd=0) for n, t, o in (
                ("project_name", "Data", None), ("status", "Select", None),
                ("vmms_geo_node", "Link", "Geo Node"), ("company", "Link", "Company"),
                ("expected_start_date", "Date", None),
            )],
        )
        self.enterContext(patch.object(frappe, "get_meta", return_value=self.meta))

    def test_maps_branch_to_geo_but_keeps_target_legal_company_and_source_id(self):
        self.loader.projects()
        row = self.values["PROJ-0093"]
        self.assertEqual((row["company"], row["vmms_geo_node"]),
                         ("Destination legal company", "GEO-00030"))
        self.assertEqual(row["project_name"], "Recorded project")
        self.assertEqual(row["status"], "Completed")
        self.assertEqual(str(row["expected_start_date"]), "2025-06-01")

    def test_numbers_include_latest_capture_reservations_and_existing_target_ids(self):
        # A newer production capture can exceed every name in the old local fixture.
        self.loader.snapshot["source"]["continuity"]["naming"]["series"][0]["current"] = 805
        self.loader.naming = NamingContinuity(self.loader.snapshot)
        self.loader.projects()
        self.assertEqual(self.loader.naming.report()["series_floors"]["PROJ-"], 805)
        self.assertTrue(any(call.args[1] == ("PROJ-", 805) for call in self.db.sql.call_args_list))

    def test_existing_target_name_can_exceed_both_captured_counter_and_source_names(self):
        self.loader.projects()
        self.assertEqual(self.loader.naming.report()["series_floors"]["PROJ-"], 400)

    def test_missing_native_geography_field_is_reported_before_a_silent_unplaced_import(self):
        self.meta.has_field = lambda name: False
        with self.assertRaisesRegex(ValueError, "geography"):
            self.loader.projects()

    def test_no_legal_company_is_not_guessed_from_an_old_branch(self):
        with patch("vmmsx.deployment.services.project.default_company", return_value=None):
            with self.assertRaisesRegex(ValueError, "legal Company"):
                self.loader.projects()

    def test_changed_destination_naming_policy_requires_review_before_import(self):
        self.meta.get_field = lambda name: SimpleNamespace(options="OTHER-.####", default="OTHER-.####")
        with self.assertRaisesRegex(ValueError, "numbering policy"):
            self.loader.projects()


class TestProjectParticipationLinks(TestCase):
    def links(self, **tables):
        from onerc_vmms_migrate.project_mapping import ProjectLinks
        return ProjectLinks(history(**tables))

    def test_blank_tor_project_is_recovered_only_from_consistent_recorded_context(self):
        links = self.links(**{
            "Project": [{"name": "PROJ-1"}],
            "Personnel Terms of Reference": [{"name": "TOR"}],
            "Deployment Request Tool": [
                {"name": "D1", "project": "PROJ-1", "terms_of_reference": "TOR"},
                {"name": "D2", "project": "PROJ-1", "terms_of_reference": "TOR"},
            ],
        })
        self.assertEqual(links.terms_project("TOR"), "PROJ-1")

    def test_blank_tor_project_with_multiple_project_contexts_is_not_guessed(self):
        links = self.links(**{
            "Project": [{"name": "P1"}, {"name": "P2"}],
            "Personnel Terms of Reference": [{"name": "TOR"}],
            "Deployment Request Tool": [
                {"name": "D1", "project": "P1", "terms_of_reference": "TOR"},
                {"name": "D2", "project": "P2", "terms_of_reference": "TOR"},
            ],
        })
        with self.assertRaisesRegex(ValueError, "conflicting Project"):
            links.terms_project("TOR")

    def test_missing_tor_project_also_checks_requests_inheriting_the_parent_tor(self):
        links = self.links(**{
            "Project": [{"name": "P1"}, {"name": "P2"}],
            "Personnel Terms of Reference": [{"name": "TOR"}],
            "Deployment Request Tool": [{"name": "D", "project": "P1", "terms_of_reference": "TOR"}],
            "Personnel Deployment Request": [{"name": "A", "deployment": "D", "project": "P2"}],
        })
        with self.assertRaisesRegex(ValueError, "conflicting Project"):
            links.terms_project("TOR")

    def test_orphan_request_cannot_change_the_explicit_project_of_its_offered_tor(self):
        links = self.links(**{
            "Project": [{"name": "P1"}, {"name": "P2"}],
            "Personnel Terms of Reference": [{"name": "TOR", "project": "P1"}],
        })
        with self.assertRaisesRegex(ValueError, "Project conflict"):
            links.assignment_project({"name": "A", "project": "P2", "terms_of_reference": "TOR"})

    def test_explicit_missing_tor_is_not_replaced_by_parent_terms(self):
        links = self.links(**{
            "Project": [{"name": "P"}],
            "Personnel Terms of Reference": [{"name": "PARENT-TOR", "project": "P"}],
            "Deployment Request Tool": [
                {"name": "D", "project": "P", "terms_of_reference": "PARENT-TOR"}
            ],
        })
        with self.assertRaisesRegex(ValueError, "Missing source TOR"):
            links.assignment_project({"name": "A", "deployment": "D", "terms_of_reference": "MISSING"})

    def test_different_offered_terms_in_the_same_project_are_kept_compatible(self):
        links = self.links(**{
            "Project": [{"name": "P"}],
            "Personnel Terms of Reference": [
                {"name": "OLD-TOR", "project": "P"}, {"name": "NEW-TOR", "project": "P"}
            ],
            "Deployment Request Tool": [
                {"name": "D", "project": "P", "terms_of_reference": "NEW-TOR"}
            ],
        })
        self.assertEqual(links.assignment_project({
            "name": "A", "deployment": "D", "project": "P", "terms_of_reference": "OLD-TOR"
        }), "P")

    def test_unsupported_project_reference_is_held_without_creating_a_project(self):
        links = self.links(**{
            "Personnel Terms of Reference": [{"name": "TOR", "project": "MISSING"}]
        })
        with self.assertRaisesRegex(ValueError, "Missing source Project"):
            links.terms_project("TOR")


class TestExistingProjectParticipation(TestCase):
    def setUp(self):
        self.loader = object.__new__(Loader)
        self.loader.snapshot = {}
        self.loader.history = history(**{
            "Project": [{"name": "P"}],
            "Personnel Terms of Reference": [
                {"name": "OLD", "project": "P"}, {"name": "NEW", "project": "P"},
            ],
            "Deployment Request Tool": [
                {"name": "D", "project": "P", "terms_of_reference": "NEW"}
            ],
        })
        self.loader.targets = {}
        self.loader.by_source = {
            ("Project", "P", "Project"): "P",
            ("Employee", "EMP", "VMMS Volunteer"): "VOL",
            ("Personnel Terms of Reference", "OLD", "VMMS Terms of Reference"): "OLD",
            ("Personnel Terms of Reference", "NEW", "VMMS Terms of Reference"): "NEW",
            ("Deployment Request Tool", "D", "VMMS Deployment"): "DEP",
        }
        self.loader.excluded_identities = set()
        self.loader.conf = {}
        self.loader.results, self.loader.failures = [], []
        self.loader.naming = MagicMock()
        self.deployment = frappe._dict(terms_of_reference="NEW")
        self.assignment = frappe._dict(
            deployment="DEP", volunteer="VOL", terms_of_reference="OLD", status="Accepted"
        )
        self.enterContext(patch.object(frappe, "get_doc", side_effect=lambda dt, name:
            self.deployment if dt == "VMMS Deployment" else self.assignment))
        self.db = self.enterContext(patch.object(frappe, "db", MagicMock()))
        self.db.get_value.return_value = "P"
        self.row = {"name": "A", "employee": "EMP", "project": "P", "deployment": "D",
                    "terms_of_reference": "OLD", "deployment_status": "Accepted"}

    def test_retry_retains_different_original_offered_terms_in_the_same_project(self):
        self.loader.validate_assignment(self.row, "ASSIGN")
        self.assertEqual(self.assignment.terms_of_reference, "OLD")
        self.db.set_value.assert_not_called()

    def test_retry_detects_a_wrong_volunteer_without_reassigning_it(self):
        self.assignment.volunteer = "SOMEONE-ELSE"
        with self.assertRaisesRegex(ValueError, "volunteer"):
            self.loader.validate_assignment(self.row, "ASSIGN")
        self.assertEqual(self.assignment.volunteer, "SOMEONE-ELSE")

    def test_orphan_retry_does_not_accept_an_unrelated_deployment_in_the_same_project(self):
        row = {**self.row, "deployment": "deleted-source-tool"}
        with patch("onerc_vmms_migrate.historical.converted", return_value=None):
            with self.assertRaisesRegex(ValueError, "derived deployment"):
                self.loader.validate_assignment(row, "ASSIGN")

    def test_retry_rejects_assignment_geo_different_from_its_actual_deployment(self):
        self.assignment.geo_node = "OTHER-GEO"
        self.deployment.geo_node = "GEO-00030"
        with self.assertRaisesRegex(ValueError, "geography"):
            self.loader.validate_assignment(self.row, "ASSIGN")

    def test_retry_detects_silently_substituted_terms(self):
        self.assignment.terms_of_reference = "NEW"
        with self.assertRaisesRegex(ValueError, "offered TOR"):
            self.loader.validate_assignment(self.row, "ASSIGN")

    def test_retry_detects_wrong_native_project_even_if_source_references_agree(self):
        self.db.get_value.return_value = "OTHER-PROJECT"
        with self.assertRaisesRegex(ValueError, "Native TOR Project"):
            self.loader.validate_assignment(self.row, "ASSIGN")

    def test_a_changed_source_response_needs_review_not_a_false_existing_success(self):
        self.assignment.status = "Pending"
        with self.assertRaisesRegex(ValueError, "response"):
            self.loader.validate_assignment(self.row, "ASSIGN")

    def test_existing_record_validation_failure_is_reported_before_reuse(self):
        self.loader.run_id = "test"
        with patch("onerc_vmms_migrate.historical.converted", return_value="ASSIGN"):
            result = self.loader.one(
                "Personnel Deployment Request", self.row, "VMMS Deployment Assignment",
                lambda row: self.fail("Existing participation must not be recreated"),
                validate_existing=lambda row, name: (_ for _ in ()).throw(ValueError("wrong offered TOR")),
            )
        self.assertIsNone(result)
        self.assertEqual(len(self.loader.failures), 1)
        self.assertFalse(self.loader.results)


class TestProjectReview(TestCase):
    def test_review_runs_after_completed_people_without_starting_an_import(self):
        from copy import deepcopy
        from onerc_vmms_migrate import browser, batches
        from onerc_vmms_migrate import date_repair_control

        checkpoint = {"scope": "people", "status": "finished", "session_id": "completed-people"}
        before = deepcopy(checkpoint)
        with (
            patch.object(browser, "authorize"),
            patch.object(frappe, "session", frappe._dict(user="Administrator")),
            patch.object(frappe, "conf", frappe._dict()),
            patch.object(frappe, "get_single", return_value=frappe._dict(browser_capture="fresh")),
            patch.object(batches, "checkpoint", return_value=checkpoint),
            patch.object(date_repair_control, "state", return_value={}),
            patch.object(frappe, "cache", MagicMock()),
            patch.object(browser, "state", return_value={}),
            patch.object(browser, "save_state"),
            patch.object(browser, "now_datetime", return_value="2026-11-15 16:00:00"),
            patch.object(frappe, "enqueue") as enqueue,
        ):
            browser.start("project_plan")
        self.assertEqual(checkpoint, before)
        self.assertEqual(enqueue.call_args.kwargs["action"], "project_plan")

    def test_review_endpoint_exists_separately_from_a_business_import(self):
        from onerc_vmms_migrate import project_mapping
        self.assertTrue(callable(project_mapping.preview))

    def review_fixture(self, *, collision=False, mapped=False):
        from onerc_vmms_migrate import browser, crosswalk, extract, project_mapping, run, source, workspace
        snapshot = {
            "source": {"source_id": "live-source", "capture_id": "fresh-capture", "profile_mapping_version": 2,
                       "continuity": {"version": 1, "tables": {}, "naming": {
                           "series": [{"name": "PROJ-", "current": 805}]
                       }}},
            "history": {"p": {"doctype": "Project", "record": {
                "name": "PROJ-0093", "company": "Old branch"
            }}}, "schemas": {"Project": {}},
        }
        meta = SimpleNamespace(has_field=lambda name: name == "vmms_geo_node",
                               autoname="naming_series:", get_field=lambda name: SimpleNamespace(
                                   options="PROJ-.####", default="PROJ-.####"))
        db = MagicMock()
        db.exists.side_effect = lambda dt, name: dt in ("Company", "Geo Node") or (dt == "Project" and collision)
        def get_value(doctype, name, field, **kwargs):
            if doctype == "Series":
                # Core Series has name/current but no creation column.
                self.assertEqual(kwargs.get("order_by"), "name")
                return 900
            return None
        db.get_value.side_effect = get_value
        cw = MagicMock()
        cw.lookup.return_value = "GEO-00030"
        with (
            patch.object(browser, "authorize"),
            patch.object(workspace, "config", return_value={}),
            patch.object(source, "Source"),
            patch.object(extract, "extract", return_value=snapshot),
            patch.object(frappe, "get_meta", return_value=meta),
            patch.object(frappe, "db", db),
            patch.object(frappe, "get_all", side_effect=lambda dt, **kw:
                ["PROJ-0400"] if dt == "Project" else
                [frappe._dict(source_doctype="Project", source_name="PROJ-0093", target_doctype="Project", target_name="PROJ-0093")]
                if dt == "Legacy Migration Record" and mapped else []),
            patch.object(frappe, "get_doc", return_value=frappe._dict(company="Legal company", vmms_geo_node="WRONG-GEO")),
            patch.object(crosswalk.Crosswalks, "read", return_value=cw),
            patch("vmmsx.deployment.services.project.default_company", return_value="Legal company"),
            patch.object(workspace, "report_dir", return_value="/tmp/project-review"),
            patch.object(workspace, "write_json") as write,
            patch.object(run, "_run_id", return_value="project-review"),
            patch.object(run, "_write_csv"),
        ):
            result = project_mapping.preview()
        return result, write.call_args.args[1], db

    def test_selected_capture_and_higher_destination_counter_are_reviewed_without_business_writes(self):
        result, review, db = self.review_fixture()
        self.assertEqual((result["source_id"], result["capture_id"]), ("live-source", "fresh-capture"))
        self.assertEqual(result["numbering"]["series_floors"]["PROJ-"], 900)
        self.assertEqual(review["project_mappings"][0]["geo_node"], "GEO-00030")
        self.assertEqual(result["mode"], "review_only")
        db.sql.assert_not_called()
        db.set_value.assert_not_called()
        db.set_next_sequence_val.assert_not_called()
        db.commit.assert_not_called()

    def test_review_requires_manager_authorization_before_opening_the_capture(self):
        from onerc_vmms_migrate import browser, project_mapping, source
        with patch.object(browser, "authorize", side_effect=PermissionError), patch.object(source, "Source") as open_source:
            with self.assertRaises(PermissionError):
                project_mapping.preview()
        open_source.assert_not_called()

    def test_review_reports_occupied_project_ids_instead_of_claiming_a_clear_mapping(self):
        result, review, _ = self.review_fixture(collision=True)
        self.assertEqual(result["source_link_problems"], 1)
        self.assertEqual(result["project_mappings"], 0)
        self.assertIn("occupied", review["problems"][0]["problem"])

    def test_review_reports_existing_source_mapped_project_placement_conflicts(self):
        result, review, _ = self.review_fixture(mapped=True)
        self.assertEqual(result["source_link_problems"], 1)
        self.assertIn("placement changed", review["problems"][0]["problem"])
