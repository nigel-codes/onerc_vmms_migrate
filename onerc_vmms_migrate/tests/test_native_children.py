"""Actual legacy child shapes must retain their own native meaning."""

from unittest import TestCase

from onerc_vmms_migrate import load, transform
from onerc_vmms_migrate.tests.test_transform import CONF, TODAY, crosswalks, snapshot, volunteer_employee


class TestNativeChildMappings(TestCase):
	def build(self, **fields):
		user = {"name": "amina@example.org", "email": "amina@example.org", **fields}
		cw = crosswalks(education_level={"Graduate": "graduate"})
		return transform.build(
			snapshot(
				users={user["name"]: user}, employees={"E": volunteer_employee("E", user_id=user["name"])}
			),
			cw,
			CONF,
			TODAY,
		)["bundles"][0]

	def test_user_hrms_education_keeps_school_grade_major_and_attachment_binding(self):
		bundle = self.build(
			education=[
				{
					"name": "ED1",
					"school_univ": "Recorded school",
					"qualification": "Recorded award",
					"level": "Graduate",
					"year_of_passing": 2019,
					"class_per": "A",
					"maj_opt_subj": "Computing",
					"attachment": "/private/files/award.pdf",
				}
			]
		)
		self.assertEqual(bundle["errors"], [])
		self.assertEqual(
			bundle["profile"]["vmms_education"],
			[
				{
					"institution": "Recorded school",
					"qualification": "Recorded award",
					"level": "graduate",
					"finished_in": 2019,
					"class_per": "A",
					"maj_opt_subj": "Computing",
				}
			],
		)
		self.assertIn(
			{"url": "/private/files/award.pdf", "purpose": "table", "table": "vmms_education", "index": 0},
			bundle["files"],
		)

	def test_lms_education_keeps_its_own_shape(self):
		bundle = self.build(
			education=[
				{
					"institution_name": "LMS school",
					"degree_type": "Graduate",
					"major": "Computing",
					"start_date": "2015-01-01",
					"end_date": "2019-12-31",
				}
			]
		)
		row = bundle["profile"]["vmms_education"][0]
		self.assertEqual(row["institution"], "LMS school")
		self.assertEqual(row["started_in"], 2015)
		self.assertEqual(row["finished_in"], 2019)

	def test_certifications_remain_certifications_with_dates(self):
		bundle = self.build(
			certification=[
				{
					"certification_name": "Recorded certificate",
					"organization": "Recorded issuer",
					"description": "Original wording",
					"expire": 1,
					"issue_date": "2020-01-01",
					"expiration_date": "2027-01-01",
				}
			],
			courses=[{"course_name": "Separate course"}],
		)
		self.assertEqual([r["course_name"] for r in bundle["profile"]["vmms_training"]], ["Separate course"])
		row = bundle["profile"].get("vmms_certifications", [{}])[0]
		self.assertEqual(row.get("certification_name"), "Recorded certificate")
		self.assertEqual(str(row.get("expiration_date")), "2027-01-01")
		self.assertEqual(row.get("expire"), 1)

	def test_documents_skills_and_allergies_use_native_child_tables(self):
		bundle = self.build(
			supporting_documents=[
				{
					"type": "Good Conduct",
					"document_name": "Original name",
					"attachment": "/private/files/good.pdf",
				},
				{"type": "Other", "document_name": "Original reference"},
			],
			additional_skills=[{"additional_skill": "Recorded skill"}],
			allergies=[{"allergy": "Recorded allergy"}],
		)
		profile = bundle["profile"]
		self.assertEqual(
			profile.get("vmms_supporting_documents"),
			[
				{"type": "Good Conduct", "document_name": "Original name"},
				{"type": "Other", "document_name": "Original reference"},
			],
		)
		self.assertEqual(profile.get("vmms_additional_skills"), [{"additional_skill": "Recorded skill"}])
		self.assertEqual(profile.get("vmms_allergies"), [{"allergy": "Recorded allergy"}])
		self.assertIn(
			{
				"url": "/private/files/good.pdf",
				"purpose": "table",
				"table": "vmms_supporting_documents",
				"index": 0,
			},
			bundle["files"],
		)

	def test_work_location_is_carried(self):
		bundle = self.build(
			work_experience=[
				{"company": "Recorded organization", "title": "Role", "location": "Recorded location"}
			]
		)
		self.assertEqual(bundle["profile"]["vmms_work_experience"][0].get("location"), "Recorded location")

	def test_meaningful_education_without_school_is_reported(self):
		bundle = self.build(education=[{"qualification": "Recorded qualification without school"}])
		self.assertTrue(any("education" in error and "institution" in error for error in bundle["errors"]))

	def test_loader_accepts_every_native_child_destination(self):
		for field in [
			"vmms_certifications",
			"vmms_supporting_documents",
			"vmms_additional_skills",
			"vmms_allergies",
		]:
			self.assertIn(field, load.PROFILE_TABLES)
