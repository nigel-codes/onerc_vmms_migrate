"""Person facts must reach native fields without the legacy question table."""
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from onerc_vmms_migrate import load, transform
from onerc_vmms_migrate.tests.test_transform import CONF, TODAY, crosswalks, snapshot, volunteer_employee


class TestNativeProfileFacts(TestCase):
	def build(self, user=None, applicant=None, employee=None):
		user={"name":"amina@example.org","email":"amina@example.org","first_name":"Amina","last_name":"Otieno",**(user or {})}
		snap=snapshot(users={user["name"]:user},employees={"E":employee or volunteer_employee("E",user_id=user["name"])},
			applicants={"A":{"name":"A","email_id":user["name"],"is_volunteer":1,**applicant}} if applicant else {})
		before=deepcopy(snap)
		result=transform.build(snap,crosswalks(question={"*":"OLD-QUESTION"}),CONF,TODAY)
		self.assertEqual(snap,before,"Mapping must not mutate original source evidence")
		return result["bundles"][0]

	def test_personal_facts_use_current_profile_fields(self):
		bundle=self.build({"blood_group":"Don't Know","number_of_dependants":3,"access_to_internet":"Sometimes",
			"linkedin":"https://www.linkedin.com/in/example","github":"https://github.com/example",
			"county":"Nairobi","sub_county":"Westlands","ward":"Parklands","administrative_location":"Westlands","sub_location":"Parklands"},
			{"mpesa_mobile_phone":"0712345678","place_of_work":"Recorded workplace"})
		profile=bundle["profile"]
		for field,value in {"vmms_blood_group":"Don't Know","vmms_number_of_dependants":3,"vmms_dependants_answered":1,
			"vmms_access_to_internet":"Sometimes","vmms_mpesa_mobile_phone":"+254712345678","vmms_place_of_work":"Recorded workplace",
			"vmms_linkedin":"https://www.linkedin.com/in/example","vmms_github":"https://github.com/example",
			"vmms_residence_county":"Nairobi","vmms_residence_sub_county":"Westlands","vmms_residence_ward":"Parklands",
			"vmms_residence_location":"Westlands","vmms_residence_sub_location":"Parklands"}.items():
			self.assertEqual(profile.get(field),value,field)
		self.assertEqual(bundle["application"]["custom_answers"],[])

	def test_insurance_and_biodata_consent_are_excluded_even_with_old_crosswalks(self):
		bundle=self.build({"has_insurance":"Yes","consent_to_use_of_bio_data":1},
			{"has_insurance":"No","reason_to_join_krcs":"Original motivation"})
		self.assertNotIn("vmms_has_insurance",bundle["profile"])
		self.assertFalse(bundle["application"]["custom_answers"])
		self.assertTrue(any("reason_to_join_krcs" in warning for warning in bundle["warnings"]))

	def test_default_zero_is_preserved_without_inventing_answered_evidence(self):
		profile=self.build({"number_of_dependants":0})["profile"]
		self.assertEqual(profile.get("vmms_number_of_dependants"),0)
		self.assertEqual(profile.get("vmms_dependants_answered"),0)

	def test_invalid_count_or_url_is_reported_without_guessing(self):
		bundle=self.build({"number_of_dependants":-1,"github":"javascript:alert(1)"})
		self.assertTrue(any("number_of_dependants" in error for error in bundle["errors"]))
		self.assertTrue(any("github" in error for error in bundle["errors"]))
		self.assertNotIn("vmms_github",bundle["profile"])

	def test_case_variants_are_normalized_to_supported_choices(self):
		profile=self.build({"marital_status":"single","citizenship":"Non-Citizen","access_to_internet":"sometimes"})["profile"]
		self.assertEqual(profile.get("marital_status"),"Single")
		self.assertEqual(profile.get("citizenship_status"),"Non-citizen")
		self.assertEqual(profile.get("vmms_access_to_internet"),"Sometimes")

	def test_employee_profession_and_blood_group_are_real_fallbacks(self):
		employee=volunteer_employee("E",user_id="amina@example.org",custom_profession="Recorded profession",blood_group="O+")
		snap=snapshot(users={"amina@example.org":{"name":"amina@example.org"}},employees={"E":employee})
		cw=crosswalks(profession={"Recorded profession":"Other"})
		profile=transform.build(snap,cw,CONF,TODAY)["bundles"][0]["profile"]
		self.assertEqual(profile.get("vmms_profession"),"Other")
		self.assertEqual(profile.get("vmms_other_profession"),"Recorded profession")
		self.assertEqual(profile.get("vmms_blood_group"),"O+")

	def test_pending_applicant_gets_person_facts_without_volunteer_creation(self):
		app={"name":"A","email_id":"applicant@example.org","applicant_name":"Amina Otieno","is_volunteer":1,"status":"Hold","access_to_internet":"No","number_of_dependants":2}
		bundle=transform.build(snapshot(applicants={"A":app}),crosswalks(),CONF,TODAY)["bundles"][0]
		self.assertEqual(bundle["profile"].get("vmms_access_to_internet"),"No")
		self.assertEqual(bundle["profile"].get("vmms_number_of_dependants"),2)
		self.assertIsNone(bundle["volunteer"])


class ProfileDocument(dict):
	name="PROFILE"
	def insert(self,**kwargs):return self
	def save(self,**kwargs):return self
	def set(self,key,value):self[key]=value


class TestProfileLoaderFacts(TestCase):
	def write(self,data,existing=None):
		doc=ProfileDocument(existing or {})
		ctx={"bundle":{"profile":data,"key":"amina@example.org"},"warnings":[],"conf":{}}
		meta=SimpleNamespace(has_field=lambda field:True)
		database=SimpleNamespace(get_value=lambda *a,**k: "PROFILE" if existing else None)
		def get_doc(values,*args):
			if isinstance(values,dict):doc.update(values)
			return doc
		with patch.object(frappe,"db",database),patch.object(frappe,"get_meta",return_value=meta),patch.object(frappe,"get_doc",side_effect=get_doc),patch.object(load,"_ledger"),patch.object(load,"_review_profile_phones"):
			load._profile(ctx,None)
		return doc

	def test_create_keeps_zero_and_all_new_native_facts(self):
		doc=self.write({"email":"amina@example.org","vmms_number_of_dependants":0,"vmms_dependants_answered":0,"vmms_access_to_internet":"No","vmms_blood_group":"O+"})
		self.assertIn("vmms_number_of_dependants",doc)
		self.assertEqual(doc["vmms_number_of_dependants"],0)
		self.assertEqual(doc.get("vmms_access_to_internet"),"No")
		self.assertEqual(doc.get("vmms_blood_group"),"O+")

	def test_existing_answered_zero_and_current_answer_are_not_overwritten(self):
		doc=self.write({"email":"amina@example.org","vmms_number_of_dependants":3,"vmms_dependants_answered":1,"vmms_access_to_internet":"No"},
			{"vmms_number_of_dependants":0,"vmms_dependants_answered":1,"vmms_access_to_internet":"Yes"})
		self.assertEqual(doc["vmms_number_of_dependants"],0)
		self.assertEqual(doc["vmms_access_to_internet"],"Yes")
