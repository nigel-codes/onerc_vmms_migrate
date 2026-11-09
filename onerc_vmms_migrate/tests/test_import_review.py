import json
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate import import_review, transform
from onerc_vmms_migrate.tests.test_transform import CONF, TODAY, crosswalks, snapshot, volunteer_employee


class TestHistoricalReviewValues(TestCase):
    def build(self, **parts):
        return transform.build(snapshot(**parts), crosswalks(), {**CONF, "import_with_review": True}, TODAY)

    def test_missing_surname_preserves_the_known_name_and_imports_without_inventing_one(self):
        (bundle,) = self.build(employees={"E": volunteer_employee("E", last_name=None)})["bundles"]
        self.assertEqual(bundle["profile"]["first_name"], "Amina")
        self.assertEqual(bundle["profile"]["last_name"], "")
        self.assertEqual(bundle["errors"], [])
        self.assertIn("last_name", bundle["profile"]["data_review_values"])

    def test_invalid_social_and_dependant_values_do_not_reject_the_person(self):
        (bundle,) = self.build(employees={"E": volunteer_employee("E")}, users={"amina@example.org": {
            "name": "amina@example.org", "email": "amina@example.org", "first_name": "Amina", "last_name": "Otieno",
            "linkedin": "typed username", "github": "javascript:alert(1)", "number_of_dependants": "many"}})["bundles"]
        profile = bundle["profile"]
        self.assertEqual(bundle["errors"], [])
        self.assertNotIn("vmms_linkedin", profile)
        self.assertNotIn("vmms_github", profile)
        self.assertNotIn("vmms_number_of_dependants", profile)
        review = json.loads(profile["data_review_values"])
        self.assertEqual({r["original_value"] for r in review}, {"typed username", "javascript:alert(1)", "many"})

    def test_long_education_subject_is_retained_without_silent_truncation(self):
        original = "A subject description " * 30
        (bundle,) = self.build(employees={"E": volunteer_employee("E", education=[{
            "school_univ": "Recorded school", "maj_opt_subj": original}])})["bundles"]
        import_review.prepare_bundle(bundle, {}, phone_check=lambda value: True)
        self.assertEqual(bundle["errors"], [])
        self.assertFalse(bundle["profile"]["vmms_education"][0].get("maj_opt_subj"))
        self.assertIn(original.strip(), bundle["profile"]["data_review_values"])

    def test_county_subcounty_policy_omits_lower_operational_location_fields(self):
        snap = snapshot(employees={"E": volunteer_employee("E")}, users={"amina@example.org": {
            "name": "amina@example.org", "email": "amina@example.org", "county": "Nairobi", "ward": "Old ward",
            "administrative_location": "Old location", "sub_location": "Old sublocation"}})
        result = transform.build(snap, crosswalks(), {**CONF, "residence_levels": ["sub_county", "county"]}, TODAY)
        profile = result["bundles"][0]["profile"]
        self.assertEqual(profile["home_geo_node"], "GEO-NBI-COUNTY")
        self.assertFalse(any(profile.get(f) for f in ("vmms_residence_ward", "vmms_residence_location", "vmms_residence_sub_location")))

    def test_reversed_training_dates_keep_the_row_and_record_the_original_end_date(self):
        bundle = {"profile": {"vmms_training": [{"started_on": "2020-06-01", "completed_on": "2019-06-01"}]}, "warnings": []}
        pairs = [("vmms_training", "Training", "started_on", "Started", "completed_on", "Completed", None, False)]
        import_review.prepare_dates(bundle, {}, pairs, lambda start, end, years: bool(start and end and end < start))
        self.assertEqual(bundle["profile"]["vmms_training"][0]["started_on"], "2020-06-01")
        self.assertIsNone(bundle["profile"]["vmms_training"][0]["completed_on"])
        self.assertEqual(bundle["_review_values"][0]["original_value"], "2019-06-01")
        self.assertEqual(bundle["_review_values"][0]["field"], "vmms_training[1].completed_on")

    def test_valid_and_open_ended_dates_are_preserved(self):
        rows = [{"start": "2020", "end": "2021"}, {"start": "2020", "end": "2019", "is_current": 1}]
        bundle = {"profile": {"experience": rows}, "warnings": []}
        pairs = [("experience", "Experience", "start", "From", "end", "To", "is_current", True)]
        import_review.prepare_dates(bundle, {}, pairs, lambda start, end, years: end < start)
        self.assertEqual([r["end"] for r in rows], ["2021", "2019"])
        self.assertNotIn("_review_values", bundle)


class MandatoryBase:
    def _get_missing_mandatory_fields(self):
        return [("first_name", "missing first"), ("last_name", "missing last"), ("email", "missing email")]


class ImportedProfile(import_review.ImportedNamesMixin, MandatoryBase):
    doctype = "Red Profile"
    name = "RP-TEST"
    def is_new(self):
        return False


class TestMissingNameBoundary(TestCase):
    def test_only_already_blank_names_of_a_proven_import_are_allowed(self):
        database = MagicMock()
        database.exists.return_value = True
        database.get_value.return_value = {"first_name": "Amina", "last_name": ""}
        with patch.object(frappe, "db", database):
            result = ImportedProfile()._get_missing_mandatory_fields()
        self.assertEqual([r[0] for r in result], ["first_name", "email"])

    def test_new_registration_and_unmapped_profiles_keep_every_required_field(self):
        database = MagicMock()
        database.exists.return_value = False
        with patch.object(frappe, "db", database):
            self.assertEqual(len(ImportedProfile()._get_missing_mandatory_fields()), 3)
        row = ImportedProfile()
        row.is_new = lambda: True
        with patch.object(frappe, "db", MagicMock()):
            self.assertEqual(len(row._get_missing_mandatory_fields()), 3)
