import json
from unittest import TestCase
from onerc_vmms_migrate import transform
from onerc_vmms_migrate.tests.test_transform import CONF, TODAY, crosswalks, snapshot, volunteer_employee


class TestPeoplePolicy(TestCase):
	def build(self, **parts):
		return transform.build(snapshot(**parts), crosswalks(), {**CONF, "import_with_review": True}, TODAY)

	def test_recorded_names_phones_and_missing_relationship_are_not_invented(self):
		b = self.build(
			employees={
				"E": volunteer_employee(
					"E",
					first_name="  AMINA ",
					last_name="",
					person_to_be_contacted=" Aunty ",
					emergency_phone_number="bad",
					relation="",
				)
			}
		)["bundles"][0]
		self.assertEqual(b["profile"]["first_name"], "  AMINA ")
		self.assertEqual(b["profile"]["last_name"], "")
		self.assertEqual(b["application"]["emergency_contacts"][0]["contact_name"], " Aunty ")
		self.assertFalse(b["application"]["emergency_contacts"][0]["relationship"])
		self.assertTrue(b["application"]["created_by_migration"])

	def test_ward_and_skills_are_excluded_even_when_mapped(self):
		result = self.build(
			employees={"E": volunteer_employee("E")},
			users={
				"amina@example.org": {
					"name": "amina@example.org",
					"email": "amina@example.org",
					"first_name": "Amina",
					"last_name": "Otieno",
					"ward": "Ward",
					"skill": [{"skill_name": "First Aid"}],
					"additional_skills": [{"additional_skill": "Swimming"}],
				}
			},
		)
		b = result["bundles"][0]
		self.assertEqual(b["volunteer"]["skills"], [])
		self.assertNotIn("vmms_additional_skills", b["profile"])
		self.assertNotIn("vmms_residence_ward", b["profile"])
		fields = {r.get("field") for r in result["not_migrated"]}
		self.assertTrue({"ward", "skill", "additional_skills"} <= fields)

	def test_employee_dob_precedes_junk_user_dob_and_raw_is_reported(self):
		result = self.build(
			employees={"E": volunteer_employee("E", date_of_birth="1990-05-07", marital_status="Married")},
			users={
				"amina@example.org": {
					"name": "amina@example.org",
					"email": "amina@example.org",
					"birth_date": "9890-01-01",
					"marital_status": "Active",
					"mobile_no": "0712345678",
					"phone": "0799000000",
				}
			},
		)
		b = result["bundles"][0]
		self.assertEqual(str(b["profile"]["date_of_birth"]), "1990-05-07")
		self.assertEqual(b["profile"]["marital_status"], "Married")
		originals = json.dumps(b["_review_values"])
		for value in ("9890-01-01", "Active", "0799000000", "0712345678"):
			self.assertIn(value, originals)
