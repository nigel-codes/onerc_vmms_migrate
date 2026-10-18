"""The transform against a hand-written snapshot. No site, no database.

Plain `unittest`, so it runs without a test site:

    cd sites && ../env/bin/python -m unittest onerc_vmms_migrate.tests.test_transform
"""

import datetime
from unittest import TestCase

from onerc_vmms_migrate import normalise, transform
from onerc_vmms_migrate.crosswalk import Crosswalks

TODAY = datetime.date(2026, 9, 23)
CONF = {"default_country_code": "254", "emergency_contact_may_call": False}


def crosswalks(**overrides) -> Crosswalks:
	table = {
		"branch": {"Nairobi Branch": "GEO-NBI", "Closed Branch": "-"},
		"volunteer_status": {"Active": "Active", "Left": "Exited", "Inactive": "Suspended"},
		"membership_type": {"Annual": "annual", "Life": "life"},
		"gender": {"Female": "Female", "Male": "Male"},
		"identification_type": {"National ID": "national-id", "__passport_number__": "passport"},
		"language": {"en": "en", "sw": "sw"},
		"skill": {"First Aid": "first-aid"},
		"residence": {"county:Nairobi": "GEO-NBI-COUNTY"},
		"question": {"blood_group": "-"},
		"disability": {"Visual": "Seeing"},
	}
	for kind, rows in overrides.items():
		table[kind] = rows
	entries = {
		kind: {normalise.key(k): {"source_value": k, "target": v} for k, v in rows.items()}
		for kind, rows in table.items()
	}
	return Crosswalks(entries)


def snapshot(**parts) -> dict:
	base = {
		"source": {},
		"users": {},
		"password_hashes": {},
		"employees": {},
		"applicants": {},
		"members": {},
		"memberships": {},
		"membership_types": {},
		"invoices": {},
		"files": {},
		"missing_columns": {},
	}
	base.update(parts)
	return base


def volunteer_employee(name="HR-EMP-0001", **fields):
	row = {
		"name": name,
		"is_volunteer": 1,
		"first_name": "Amina",
		"last_name": "Otieno",
		"personal_email": "Amina@Example.org ",
		"company": "Nairobi Branch",
		"status": "Active",
		"date_of_joining": datetime.date(2021, 3, 1),
		"cell_number": "0712 345 678",
		"gender": "Female",
		"person_to_be_contacted": "Jane Otieno",
		"emergency_phone_number": "0722000111",
		"relation": "Mother",
	}
	row.update(fields)
	return row


class TestNormalise(TestCase):
	def test_phone_shapes(self):
		for raw in ("0712 345 678", "712345678", "254712345678", "+254-712-345-678", "00254712345678"):
			self.assertEqual(normalise.phone(raw, "254"), "+254712345678", raw)

	def test_names_split_only_when_there_are_two_words(self):
		self.assertEqual(normalise.names("Amina Wanjiru Otieno", None, None), ("Amina", "Wanjiru", "Otieno"))
		self.assertIsNone(normalise.names("Amina", None, None))

	def test_email_is_lowercased_and_checked(self):
		self.assertEqual(normalise.email("  Amina@Example.ORG "), "amina@example.org")
		self.assertIsNone(normalise.email("not-an-email"))


class TestTransform(TestCase):
	def build(self, snap, cw=None):
		return transform.build(snap, cw or crosswalks(), CONF, TODAY)

	def test_volunteer_bundle_has_an_approved_path_and_keeps_history(self):
		result = self.build(snapshot(employees={"HR-EMP-0001": volunteer_employee()}))
		(b,) = result["bundles"]

		self.assertEqual(b["errors"], [])
		self.assertEqual(b["key"], "amina@example.org")
		self.assertEqual(b["profile"]["phone"], "+254712345678")
		self.assertEqual(b["volunteer"]["home_geo_node"], "GEO-NBI")
		self.assertEqual(b["volunteer"]["joined_on"], datetime.date(2021, 3, 1))
		# The application exists so that derive_status() can find an approval.
		self.assertEqual(b["application"]["geo_node"], "GEO-NBI")
		self.assertEqual(b["application"]["emergency_contacts"][0]["primary_phone"], "+254722000111")
		# Consent to call was never recorded, so it is not invented.
		self.assertEqual(b["application"]["emergency_contacts"][0]["may_contact_in_emergency"], 0)

	def test_unreviewed_crosswalk_value_rejects_the_person(self):
		result = self.build(snapshot(employees={"E": volunteer_employee("E", company="Mombasa Branch")}))
		(b,) = result["bundles"]
		self.assertTrue(any("unmapped branch" in e for e in b["errors"]))

	def test_branch_marked_not_migrated_is_an_error_not_a_silent_drop(self):
		result = self.build(snapshot(employees={"E": volunteer_employee("E", company="Closed Branch")}))
		self.assertTrue(any("marked '-'" in e for e in result["bundles"][0]["errors"]))

	def test_left_becomes_exited_with_the_relieving_date(self):
		employee = volunteer_employee(status="Left", relieving_date=datetime.date(2024, 1, 31))
		(b,) = self.build(snapshot(employees={"E": employee}))["bundles"]
		self.assertEqual(b["volunteer"]["status"], "Exited")
		self.assertEqual(b["volunteer"]["exited_on"], datetime.date(2024, 1, 31))

	def test_no_email_is_reported_not_loaded(self):
		employee = volunteer_employee(personal_email=None)
		result = self.build(snapshot(employees={"E": employee}))
		self.assertEqual(result["bundles"], [])
		self.assertEqual(result["not_migrated"][0]["reason"], "volunteer has no usable email")

	def test_independent_open_application_preserves_a_profile_without_making_a_volunteer(self):
		result=self.build(snapshot(applicants={"A":{"name":"A","is_volunteer":1,"email_id":"applicant@example.org",
			"applicant_name":"Amina Otieno","status":"Open","company":"Nairobi Branch"}}))
		(bundle,)=result['bundles']
		self.assertEqual(bundle['errors'],[])
		self.assertIsNone(bundle['volunteer'])
		self.assertEqual(bundle['sources'],[('Job Applicant','A')])
		self.assertEqual(result['not_migrated'],[])

	def test_rejected_application_is_not_rewritten_as_the_volunteers_approval(self):
		result=self.build(snapshot(employees={'E':volunteer_employee('E')},applicants={'A':{'name':'A',
			'email_id':'amina@example.org','status':'Rejected','is_volunteer':1}}))
		(bundle,)=result['bundles']
		self.assertEqual(bundle['application']['source_doctype'],'Employee')
		self.assertEqual(bundle['application']['source_name'],'E')

	def test_member_without_memberships_keeps_their_profile(self):
		(b,) = self.build(snapshot(members={"M": {"name": "M", "email_id": "member@example.org", "member_name": "Amina Otieno"}}))["bundles"]
		self.assertEqual(b["errors"], [])
		self.assertEqual(b["member"], {"source_names": ["M"], "joined_on": None})
		self.assertEqual(b["memberships"], [])
		self.assertIsNone(b["volunteer"])

	def test_pending_membership_does_not_erase_member_identity_or_invent_activation(self):
		(b,) = self.build(snapshot(members={"M": {"name": "M", "email_id": "member@example.org", "member_name": "Amina Otieno"}},
			memberships={"MS": {"name": "MS", "member": "M", "status": "Pending", "company": "Nairobi Branch", "membership_type": "Annual"}}))["bundles"]
		self.assertEqual(b["errors"], [])
		self.assertEqual(b["member"]["source_names"], ["M"])
		self.assertIsNone(b["member"]["joined_on"])
		self.assertEqual(b["memberships"][0]["membership_status"], "Awaiting Payment")
		self.assertEqual(b["memberships"][0]["approval_state"], "Submitted")
		self.assertIsNone(b["memberships"][0]["valid_from"])

	def test_login_email_wins_and_user_facts_take_precedence(self):
		users = {
			"amina@example.org": {
				"name": "amina@example.org",
				"email": "amina@example.org",
				"first_name": "Amina",
				"last_name": "Otieno-Kamau",
				"enabled": 1,
				"roles": [{"role": "Volunteer"}, {"role": "System Manager"}],
				"social_logins": [
					{"provider": "google", "userid": "123"},
					{"provider": "frappe", "userid": "x"},
				],
				"languages": [{"language": "sw"}],
			}
		}
		employee = volunteer_employee(user_id="amina@example.org", personal_email="other@example.org")
		(b,) = self.build(
			snapshot(users=users, employees={"E": employee}, password_hashes={"amina@example.org": "$pbkdf2"})
		)["bundles"]

		self.assertEqual(b["key"], "amina@example.org")
		self.assertEqual(b["profile"]["last_name"], "Otieno-Kamau")
		self.assertEqual(b["user"]["password_hash"], "$pbkdf2")
		self.assertEqual(
			b["user"]["social_logins"], [{"provider": "google", "userid": "123", "username": None}, {"provider": "frappe", "userid": "x", "username": None}]
		)
		self.assertEqual(b["volunteer"]["languages"], ["sw"])
		self.assertTrue(any("System Manager" in w for w in b["warnings"]))

	def test_memberships_keep_their_dates_and_expire_honestly(self):
		members = {
			"VM-MEM-1": {"name": "VM-MEM-1", "email_id": "amina@example.org", "member_name": "Amina Otieno"}
		}
		memberships = {
			"VM-MSH-1": {
				"name": "VM-MSH-1",
				"member": "VM-MEM-1",
				"membership_type": "Annual",
				"company": "Nairobi Branch",
				"status": "Active",
				"from_date": datetime.date(2025, 1, 1),
				"to_date": datetime.date(2025, 12, 31),
				"paid": 1,
			},
			"VM-MSH-2": {
				"name": "VM-MSH-2",
				"member": "VM-MEM-1",
				"membership_type": "Life",
				"company": "Nairobi Branch",
				"status": "Active",
				"from_date": datetime.date(2026, 2, 1),
				"to_date": datetime.date(2126, 2, 1),
			},
			"VM-MSH-3": {
				"name": "VM-MSH-3",
				"membership_type": "Annual",
				"member": "VM-MEM-1",
				"status": "Pending",
				"company": "Nairobi Branch",
			},
		}
		invoices = {
			"VM-MSH-2": {"name": "SINV-9", "status": "Paid", "posting_date": datetime.date(2026, 2, 1)}
		}
		(b,) = self.build(snapshot(members=members, memberships=memberships, invoices=invoices))["bundles"]

		self.assertEqual(b["errors"], [])
		by_name = {m["source_name"]: m for m in b["memberships"]}
		# Still "Active" on the old site, but its window closed: recorded as Expired.
		self.assertEqual(by_name["VM-MSH-1"]["membership_status"], "Expired")
		self.assertEqual(by_name["VM-MSH-1"]["valid_from"], datetime.date(2025, 1, 1))
		self.assertEqual(by_name["VM-MSH-2"]["membership_status"], "Active")
		self.assertEqual(by_name["VM-MSH-2"]["payment_receipt"], "SINV-9")
		# Pending is retained without inventing approval or payment.
		self.assertEqual(by_name["VM-MSH-3"]["membership_status"], "Awaiting Payment")
		self.assertEqual(by_name["VM-MSH-3"]["approval_state"], "Submitted")
		self.assertEqual(b["member"]["joined_on"], datetime.date(2025, 1, 1))

	def test_conflicting_ids_remain_reviewable_without_merging_accounts(self):
		users = {
			email: {
				"name": email,
				"email": email,
				"identification_type": "National ID",
				"id_number": "12345678",
			}
			for email in ("a@example.org", "b@example.org")
		}
		employees = {
			"E1": volunteer_employee("E1", personal_email="a@example.org"),
			"E2": volunteer_employee("E2", personal_email="b@example.org"),
		}
		result = self.build(snapshot(users=users, employees=employees))
		for b in result["bundles"]:
			self.assertFalse(b["errors"],b["key"])
			self.assertEqual(b["profile"]["identifications"],[])
			self.assertIn("12345678",b["profile"]["legacy_identity_claims"])
			self.assertEqual(b["profile"]["email"],b["key"])

	def test_disability_rows_mean_yes_and_absence_means_nothing(self):
		users = {
			"amina@example.org": {
				"name": "amina@example.org",
				"email": "amina@example.org",
				"disabilities": [{"disability": "Visual"}],
			}
		}
		(b,) = self.build(snapshot(users=users, employees={"E": volunteer_employee()}))["bundles"]
		self.assertEqual(b["profile"]["vmms_disability_status"], "Yes")
		self.assertEqual(b["profile"]["vmms_disabilities"], [{"disability": "Seeing"}])

		(b,) = self.build(snapshot(employees={"E": volunteer_employee()}))["bundles"]
		self.assertNotIn("vmms_disability_status", b["profile"])

	def test_hash_ignores_signing_in(self):
		user = {"name": "amina@example.org", "email": "amina@example.org", "last_login": "2026-01-01"}
		first = self.build(snapshot(users={"u": dict(user)}, employees={"E": volunteer_employee()}))
		user["last_login"] = "2026-09-01"
		second = self.build(snapshot(users={"u": dict(user)}, employees={"E": volunteer_employee()}))
		self.assertEqual(first["bundles"][0]["source_hash"], second["bundles"][0]["source_hash"])


class TestDefaultRow(TestCase):
	def build(self, snap, cw):
		return transform.build(snap, cw, CONF, TODAY)

	def test_star_row_answers_for_unlisted_values_and_own_rows_win(self):
		cw = crosswalks(
			residence={"*": "-", "county:Nairobi": "GEO-NBI-COUNTY"},
			profession={"*": "Other", "Nurse": "Nurse"},
		)
		users = {
			"amina@example.org": {
				"name": "amina@example.org",
				"email": "amina@example.org",
				"ward": "Kilimani",
				"sub_location": "Some Village",
				"county": "Nairobi",
				"profession": "Community Health Promoter",
			}
		}
		(b,) = self.build(snapshot(users=users, employees={"E": volunteer_employee()}), cw)["bundles"]
		self.assertEqual(b["errors"], [])
		# Deeper levels fall to "*" = "-", so the mapped county is where they live.
		self.assertEqual(b["profile"]["home_geo_node"], "GEO-NBI-COUNTY")
		self.assertEqual(b["profile"]["vmms_profession"], "Other")
		self.assertEqual(b["profile"]["vmms_other_profession"], "Community Health Promoter")

	def test_blank_star_row_changes_nothing(self):
		cw = crosswalks(residence={"*": "", "county:Nairobi": "GEO-NBI-COUNTY"})
		users = {
			"amina@example.org": {
				"name": "amina@example.org",
				"email": "amina@example.org",
				"ward": "Kilimani",
				"county": "Nairobi",
			}
		}
		(b,) = self.build(snapshot(users=users, employees={"E": volunteer_employee()}), cw)["bundles"]
		self.assertTrue(any("Unmapped ward" in e for e in b["warnings"]))
		self.assertEqual(b["profile"]["home_geo_node"], "GEO-NBI-COUNTY")

	def test_unmapped_branch_still_collects_every_other_value(self):
		cw = crosswalks(skill={}, language={})
		users = {
			"amina@example.org": {
				"name": "amina@example.org",
				"email": "amina@example.org",
				"languages": [{"language": "fr"}],
				"additional_skills": [{"additional_skill": "Radio"}],
			}
		}
		employee = volunteer_employee(company="Nowhere Branch")
		self.build(snapshot(users=users, employees={"E": employee}), cw)
		self.assertIn("fr", cw.seen["language"])
		self.assertIn("radio", cw.seen["skill"])


PLACEHOLDER_CONF = {**CONF, "placeholder_values": ["none", "unknown"]}


class TestPlaceholders(TestCase):
	def test_placeholder_id_is_blank_and_does_not_join_two_people(self):
		users = {
			email: {
				"name": email,
				"email": email,
				"identification_type": "National ID",
				"id_number": "UNKNOWN",
			}
			for email in ("a@example.org", "b@example.org")
		}
		employees = {
			"E1": volunteer_employee("E1", personal_email="a@example.org"),
			"E2": volunteer_employee("E2", personal_email="b@example.org"),
		}
		cw = crosswalks()
		result = transform.build(snapshot(users=users, employees=employees), cw, PLACEHOLDER_CONF, TODAY)
		for b in result["bundles"]:
			self.assertEqual(b["errors"], [], b["key"])
			self.assertEqual(b["profile"]["identifications"], [])
		self.assertEqual(cw.placeholder_hits["id_number"], 2)

	def test_a_none_disability_row_is_not_a_disability(self):
		users = {
			"amina@example.org": {
				"name": "amina@example.org",
				"email": "amina@example.org",
				"disabilities": [{"disability": "None"}],
			}
		}
		(b,) = transform.build(
			snapshot(users=users, employees={"E": volunteer_employee()}),
			crosswalks(),
			PLACEHOLDER_CONF,
			TODAY,
		)["bundles"]
		self.assertEqual(b["errors"], [])
		self.assertNotIn("vmms_disability_status", b["profile"])


class TestUntypedLegacyIdentifier(TestCase):
	def test_missing_type_does_not_invent_a_typed_identification(self):
		cw = crosswalks()
		data = snapshot(users={"amina@example.org":{"name":"amina@example.org","email":"amina@example.org", "first_name":"Amina","last_name":"Otieno","id_number":"SOURCE-ID-WITHOUT-TYPE"}},employees={"E":volunteer_employee()})
		b = transform.build(data,cw,{},datetime.date(2026,10,18))["bundles"][0]
		self.assertEqual(b["profile"]["legacy_untyped_id_number"],"SOURCE-ID-WITHOUT-TYPE")
		self.assertEqual(b["profile"]["identifications"],[])
		self.assertFalse(any("identification type" in error for error in b["errors"]))


class TestIndependentMemberHistory(TestCase):
    def test_ambiguous_volunteer_does_not_discard_member_history(self):
        data=snapshot(employees={'E1':volunteer_employee('E1'),'E2':volunteer_employee('E2')},
            members={'M':{'name':'M','email_id':'amina@example.org','member_name':'Amina Otieno'}},
            memberships={'MS':{'name':'MS','member':'M','membership_type':'Annual',
                'company':'Nairobi Branch','status':'Pending'}})
        bundle=transform.build(data,crosswalks(),CONF,TODAY)['bundles'][0]
        self.assertEqual(bundle['errors'],[])
        self.assertIsNone(bundle['volunteer'])
        self.assertTrue(bundle['volunteer_excluded'])
        self.assertEqual(bundle['member']['source_names'],['M'])
        self.assertEqual(bundle['memberships'][0]['source_name'],'MS')


class TestLosslessLegacyDetails(TestCase):
    def test_invalid_reference_email_and_long_education_remain_reviewable(self):
        text=('Recorded institution detail. '*12).strip()
        user={'name':'amina@example.org','email':'amina@example.org',
            'education':[{'institution_name':text}],
            'work_references':[{'reference_name':'Synthetic Reference','email':'not-an-email'}]}
        bundle=transform.build(snapshot(users={user['name']:user},employees={'E':volunteer_employee()}),crosswalks(),CONF,TODAY)['bundles'][0]
        education=bundle['profile']['vmms_education'][0]
        self.assertEqual(education['legacy_institution'],text)
        self.assertLessEqual(len(education['institution']),140)
        reference=bundle['profile']['vmms_references'][0]
        self.assertIsNone(reference['email'])
        self.assertEqual(reference['legacy_email'],'not-an-email')
        self.assertEqual(bundle['profile']['legacy_phone'],'0712 345 678')
