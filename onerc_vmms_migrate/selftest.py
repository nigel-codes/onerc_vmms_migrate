"""Prove the loader against THIS site's real configuration, then undo everything.

    bench --site T execute onerc_vmms_migrate.run.selftest

Builds a throwaway geo chain from this site's own Geo Levels, two membership
types and four synthetic people who cover the traps (a volunteer-and-member
with a login and a password, a volunteer who left, a member with a lifetime and
an expired membership, and a member without memberships). It loads them through the real loader and vmmsx's real
validation and hooks, checks what came out, and **rolls the whole transaction
back**, whatever happens. Only synthetic fixture bytes are copied; rollback
removes them. Nothing is sent.

Run it on a rehearsal copy of the target after every change to vmmsx, the
society's settings or this app. A failure here is a failure the real load would
meet.
"""

import datetime

import frappe
from frappe.utils.password import check_password, passlibctx

from onerc_vmms_migrate import load as loader
from onerc_vmms_migrate import normalise, transform
from onerc_vmms_migrate.crosswalk import Crosswalks
from onerc_vmms_migrate.silence import Guard, silenced

PASSWORD = "Old-Site-Password-1"
DOMAIN = "selftest.onerc-vmms-migrate.invalid"


class _NoFiles:
	def file_path(self, url):
		return None


def run() -> dict:
	frappe.set_user("Administrator")
	frappe.db.rollback()
	results: list[tuple[str, bool, str]] = []
	created_files = []

	def expect(what, actual, expected):
		results.append(
			(
				what,
				actual == expected,
				f"{actual!r}" + ("" if actual == expected else f" (expected {expected!r})"),
			)
		)

	try:
		with silenced():
			guard=Guard()
			_exercise(expect)
			created_files = _exercise_files(expect)
			_exercise_account_batch(expect)
			_exercise_email_link(expect)
			guard.check()
			expect("all self-test phases keep the outbox unchanged",True,True)
	except Exception as exc:
		results.append(("no exception", False, f"{type(exc).__name__}: {frappe.utils.strip_html(str(exc))}"))
	finally:
		frappe.db.rollback()
		frappe.clear_messages()

	if created_files:
		expect("synthetic attachment bytes removed on rollback", all(not path.exists() for path in created_files), True)
	for what, ok, detail in results:
		print(("  ok   " if ok else "  FAIL ") + f"{what}: {detail}")
	passed = all(ok for _, ok, _ in results)
	print("SELFTEST", "PASS" if passed else "FAIL", "(everything rolled back)")
	return {"passed": passed, "checks": len(results), "failed": [w for w, ok, _ in results if not ok]}


def _geo_chain() -> tuple[str, str]:
	"""A node per active level, each under the last. Returns (volunteer, membership) anchors."""
	from vmmsx.member.services import society as member_society
	from vmmsx.volunteer.services import society as volunteer_society

	levels = frappe.get_all(
		"Geo Level", {"is_active": 1}, ["name", "geo_level_order"], order_by="geo_level_order"
	)
	by_level, parent = {}, None
	for level in levels:
		parent = (
			frappe.get_doc(
				{
					"doctype": "Geo Node",
					"geo_node_name": f"Selftest {level.name}",
					"geo_level": level.name,
					"parent_geo_node": parent,
					"is_group": 1,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
		by_level[level.name] = parent

	deepest = parent
	return (
		by_level.get(volunteer_society.volunteer_anchor_level()) or deepest,
		by_level.get(member_society.membership_anchor_level()) or deepest,
	)


def _exercise(expect) -> None:
	outbox = frappe.db.count("Email Queue")
	volunteer_node, membership_node = _geo_chain()
	from onerc_vmms_migrate.historical import insert as historical_insert
	tor = historical_insert("VMMS Terms of Reference", {
		"tor_name": "Synthetic historical child validation",
		"tor_key": "selftest-children-" + frappe.generate_hash(length=10),
		"geo_scope": volunteer_node, "approval_mode": "direct",
		"objectives": [{"objective": "Recorded synthetic objective"}],
	})
	expect("historical child rows retain their parent before native validation",
		(tor.objectives[0].parent, tor.objectives[0].parenttype, tor.objectives[0].parentfield),
		(tor.name, tor.doctype, "objectives"))

	for key, lifetime, days in (("selftest-annual", 0, 365), ("selftest-life", 1, 0)):
		frappe.get_doc(
			{
				"doctype": "VMMS Membership Type",
				"membership_type_name": key,
				"membership_type_key": key,
				"is_active": 1,
				"is_lifetime": lifetime,
				"duration_days": days or None,
				"approval_mode": "routed",
			}
		).insert(ignore_permissions=True)

	id_type = frappe.get_all("Identification Type", pluck="name", limit=1)
	table = {
		"branch": {"Volunteer Branch": volunteer_node, "Membership Branch": membership_node},
		"volunteer_status": {"Active": "Active", "Left": "Exited"},
		"membership_type": {"Annual": "selftest-annual", "Life": "selftest-life"},
		"gender": {"Female": "Female", "Male": "Male"},
		"identification_type": {"National ID": id_type[0]} if id_type else {},
	}
	cw = Crosswalks(
		{
			kind: {normalise.key(k): {"source_value": k, "target": v} for k, v in rows.items()}
			for kind, rows in table.items()
		}
	)

	amina, brian, chero, dina, eli = (f"{n}@{DOMAIN}" for n in ("amina", "brian", "chero", "dina", "eli"))
	snapshot = {
		"source": {},
		"users": {
			amina: {
				"name": amina,
				"email": amina,
				"first_name": "Amina",
				"last_name": "Selftest",
				"enabled": 1,
				"roles": [{"role": "Volunteer"}],
				"social_logins": [{"provider": "google", "userid": "selftest-g-1"}],
				"identification_type": "National ID" if id_type else None,
				"id_number": "SELFTEST-1" if id_type else None,
			}
		},
		"password_hashes": {amina: passlibctx.hash(PASSWORD)},
		"employees": {
			"SELFTEST-E1": {
				"name": "SELFTEST-E1",
				"is_volunteer": 1,
				"user_id": amina,
				"gender": "Female",
				"company": "Volunteer Branch",
				"status": "Active",
				"date_of_joining": datetime.date(2021, 3, 1),
				"person_to_be_contacted": "Jane Selftest",
				"emergency_phone_number": "0722000111",
			},
			"SELFTEST-E2": {
				"name": "SELFTEST-E2",
				"is_volunteer": 1,
				"personal_email": brian,
				"first_name": "Brian",
				"last_name": "Selftest",
				"company": "Volunteer Branch",
				"status": "Left",
				"date_of_joining": datetime.date(2019, 6, 1),
				"relieving_date": datetime.date(2023, 12, 31),
			},
		},
		"applicants": {},
		"members": {
			"SELFTEST-M1": {"name": "SELFTEST-M1", "email_id": amina},
			"SELFTEST-M2": {"name": "SELFTEST-M2", "email_id": chero, "member_name": "Chero Selftest"},
			"SELFTEST-M3": {"name": "SELFTEST-M3", "email_id": dina, "member_name": "Dina Selftest"},
			"SELFTEST-M4": {"name": "SELFTEST-M4", "email_id": eli, "member_name": "Eli Selftest"},
		},
		"memberships": {
			**{f"SELFTEST-PENDING-{state}": {"name":f"SELFTEST-PENDING-{state}","member":"SELFTEST-M4",
				"membership_type":"Annual","company":"Membership Branch","status":state}
				for state in ("Draft","Pending","Rejected")},
			"SELFTEST-MS1": {
				"name": "SELFTEST-MS1",
				"member": "SELFTEST-M1",
				"membership_type": "Annual",
				"company": "Membership Branch",
				"status": "Active",
				"from_date": datetime.date.today() - datetime.timedelta(days=30),
				"to_date": datetime.date.today() + datetime.timedelta(days=335),
				"member_since_date": datetime.date(2018, 1, 1),
				"paid": 1,
			},
			"SELFTEST-MS2": {
				"name": "SELFTEST-MS2",
				"member": "SELFTEST-M2",
				"membership_type": "Life",
				"company": "Membership Branch",
				"status": "Active",
				"from_date": datetime.date(2015, 2, 1),
				"to_date": datetime.date(2115, 2, 1),
			},
			"SELFTEST-MS3": {
				"name": "SELFTEST-MS3",
				"member": "SELFTEST-M2",
				"membership_type": "Annual",
				"company": "Membership Branch",
				"status": "Expired",
				"from_date": datetime.date(2013, 2, 1),
				"to_date": datetime.date(2014, 1, 31),
			},
		},
		"membership_types": {},
		"invoices": {
			"SELFTEST-MS1": {
				"name": "SELFTEST-INV-1",
				"status": "Paid",
				"posting_date": datetime.date(2026, 1, 15),
			}
		},
		"files": {},
		"missing_columns": {},
	}

	snapshot["users"][eli]={"name":eli,"email":eli,"first_name":"Eli","last_name":"Selftest",
		"enabled":1,"id_number":"UNSPECIFIED-SELFTEST-ID"}
	if id_type:
		snapshot["users"][brian]={"name":brian,"email":brian,"first_name":"Brian","last_name":"Selftest",
			"enabled":1,"identification_type":"National ID","id_number":"SELFTEST-1"}
	long_school=("Recorded institution detail. "*12).strip()
	snapshot["users"][amina]["education"]=[{"institution_name":long_school}]
	snapshot["users"][amina]["work_references"]=[{"reference_name":"Synthetic Reference","email":"not-an-email"}]
	from onerc_vmms_migrate.history_model import canonical
	immutable_source=canonical(snapshot)
	conf = {"default_country_code": "254","preserve_passwords":True}
	result = transform.build(snapshot, cw, conf, datetime.date.today())
	for b in result["bundles"]:
		expect(f"{b['key']} transforms cleanly", b["errors"], [])

	# Reproduce the full runner's account-first phase before its people phase.
	initial_bundle=next(b for b in result["bundles"] if b["key"]==amina)
	initial_user=loader._user({"bundle":initial_bundle,"conf":conf,"ledger":[],"warnings":[]})
	frappe.get_doc({"doctype":loader.LEDGER,"person_key":"account:"+amina,
		"source_doctype":"User","source_name":amina,"target_doctype":"User","target_name":initial_user,
		"source_hash":"synthetic-account-first","run_id":"selftest","outcome":"created"}).insert(ignore_permissions=True)
	written = {}
	with silenced():
		for b in result["bundles"]:
			written[b["key"]] = loader.load_bundle(b, _NoFiles(), conf, "selftest", dry_run=True)

	expect("native inserts do not mutate source snapshot rows",canonical(snapshot)==immutable_source,True)
	expect("account and person phases share one source login mapping",frappe.db.count(loader.LEDGER,{"source_doctype":"User","source_name":amina,"target_doctype":"User"}),1)
	a = written[amina]
	vol = frappe.get_doc("VMMS Volunteer", a["volunteer"])
	expect("volunteer is Active (derived from the imported approval)", vol.status, "Active")
	expect("volunteer keeps joined_on", str(vol.joined_on), "2021-03-01")
	expect("volunteer has a card token", bool(vol.card_token), True)
	app = frappe.get_all("VMMS Volunteer Application", {"volunteer": vol.name}, ["name", "approval_state"])[0]
	expect("application is Approved", app.approval_state, "Approved")
	stages = frappe.get_all("VMMS Approval Decision", {"parent": app.name}, pluck="stage")
	expect("decision recorded as an import", stages, [loader.IMPORT_STAGE])
	user = frappe.get_doc("User", amina)
	expect("login is a portal user", user.user_type, "Website User")
	expect("old password still works", bool(check_password(amina, PASSWORD)), True)
	expect(
		"google sign-in carried",
		("google", "selftest-g-1") in [(s.provider, s.userid) for s in user.social_logins],
		True,
	)
	profile = frappe.get_doc("Red Profile", a["profile"])
	expect("full legacy education text survives native validation",profile.vmms_education[0].legacy_institution,long_school)
	expect("invalid reference email retained for review",profile.vmms_references[0].legacy_email,"not-an-email")
	expect("profile linked to login", profile.user, amina)
	expect(
		"affiliations written by the services",
		sorted(r.affiliation_type for r in profile.affiliations),
		["member", "volunteer"],
	)
	membership = frappe.get_all(
		"VMMS Membership",
		{"member": a["member"]},
		["membership_status", "valid_from", "payment_receipt", "card_token"],
	)[0]
	expect("membership stays Active", membership.membership_status, "Active")
	expect(
		"membership keeps its start date",
		str(membership.valid_from),
		str(snapshot["memberships"]["SELFTEST-MS1"]["from_date"]),
	)
	expect("membership keeps its receipt", membership.payment_receipt, "SELFTEST-INV-1")
	expect("membership has a card token", bool(membership.card_token), True)
	expect(
		"member joined_on is member-since",
		str(frappe.db.get_value("VMMS Member", a["member"], "joined_on")),
		"2018-01-01",
	)

	b = written[brian]
	vol = frappe.get_doc("VMMS Volunteer", b["volunteer"])
	expect("a volunteer who left stays Exited", vol.status, "Exited")
	expect("and keeps the date they left", str(vol.exited_on), "2023-12-31")

	c = written[chero]
	rows = {
		r.membership_type: r
		for r in frappe.get_all(
			"VMMS Membership", {"member": c["member"]}, ["membership_type", "membership_status", "valid_to"]
		)
	}
	expect("lifetime membership is Active", rows["selftest-life"].membership_status, "Active")
	expect("lifetime membership has no end date", rows["selftest-life"].valid_to, None)
	expect("an old membership arrives Expired", rows["selftest-annual"].membership_status, "Expired")

	d = written[dina]
	expect("a member without memberships is retained", bool(d["member"]), True)
	expect("a member without memberships remains Prospective", frappe.db.get_value("VMMS Member", d["member"], "status"), "Prospective")
	expect("no membership is invented", frappe.db.count("VMMS Membership", {"member": d["member"]}), 0)
	expect("no joining date is invented", frappe.db.get_value("VMMS Member", d["member"], "joined_on"), None)
	expect("member-only source crosswalk is retained", bool(frappe.db.exists(loader.LEDGER, {"source_doctype": "VM Member", "source_name": "SELFTEST-M3", "target_name": d["member"]})), True)

	e = written[eli]
	expect("untyped identifier survives without inventing a type",frappe.db.get_value("Red Profile",e["profile"],"legacy_untyped_id_number"),"UNSPECIFIED-SELFTEST-ID")
	if id_type:
		import json
		profile=frappe.get_doc("Red Profile",a["profile"])
		expect("conflicting identity claim retained for review",json.loads(profile.legacy_identity_claims)[0]["id_number"],"SELFTEST-1")
		expect("conflicting identity is not declared primary",len(profile.identifications),0)
	states = sorted((r.membership_status,r.approval_state) for r in frappe.get_all("VMMS Membership", {"member":e["member"]}, ["membership_status","approval_state"]))
	expect("Draft, Pending and Rejected memberships all survive", states, sorted([("Draft","Draft"),("Awaiting Payment","Submitted"),("Cancelled","Rejected")]))
	expect("pending history does not activate a member",frappe.db.get_value("VMMS Member",e["member"],"status"),"Prospective")
	expect("pending history does not invent payment dates",frappe.db.count("VMMS Membership",{"member":e["member"],"paid_on":("is","set")}),0)
	expect("pending history does not invent joining dates",frappe.db.get_value("VMMS Member",e["member"],"joined_on"),None)

	expect("nothing was queued to be emailed", frappe.db.count("Email Queue"), outbox)


def _exercise_files(expect):
    import hashlib
    import tempfile
    from pathlib import Path
    from onerc_vmms_migrate.file_transfer import copy_verified
    content=b"%PDF-1.4\nHistorical migration attachment test.\n%%EOF\n"
    profile=frappe.get_doc({'doctype':'Red Profile','email':'file-test@'+DOMAIN,'first_name':'File','last_name':'Selftest'}).insert(ignore_permissions=True)
    with tempfile.TemporaryDirectory(prefix='vmms-file-selftest-') as directory:
        path=Path(directory)/'original.pdf';path.write_bytes(content)
        url=copy_verified(path,'Original Attachment.pdf','Red Profile',profile.name,True,hashlib.sha256(content).hexdigest())
    file=frappe.get_doc('File',{'file_url':url,'attached_to_doctype':'Red Profile','attached_to_name':profile.name})
    path=Path(file.get_full_path())
    expect('historical attachment remains private',int(file.is_private),1)
    expect('historical attachment keeps its display name',file.file_name,'Original Attachment.pdf')
    expect('historical attachment bytes match exactly',path.read_bytes(),content)
    return [path]


def _exercise_account_batch(expect):
	from onerc_vmms_migrate.load import _user
	before = frappe.db.count("User")
	previous_import = frappe.flags.in_import
	for number in range(65):
		email = f"batch-{number}@selftest.onerc-vmms-migrate.invalid"
		_user({"bundle":{"user":{"email":email,"source_name":email,"enabled":1,"password_hash":None,"social_logins":[]},
			"profile":{"first_name":"Migration","last_name":"Batch"}},"conf":{},"ledger":[],"warnings":[]})
	expect("native import exceeds normal signup throttle",frappe.db.count("User")-before,65)
	expect("native import flag restored",frappe.flags.in_import,previous_import)


def _exercise_email_link(expect):
	from urllib.parse import urlsplit,parse_qs
	from frappe.www.login import _generate_temporary_login_link
	email=f"batch-0@{DOMAIN}"
	expect("email-link test account has no local password",frappe.db.sql("SELECT COUNT(*) FROM __Auth WHERE doctype='User' AND name=%s AND fieldname='password' AND encrypted=0",(email,))[0][0],0)
	link=_generate_temporary_login_link(email,5)
	key=parse_qs(urlsplit(link).query)['key'][0]
	try:
		expect("native email-link sign-in can target an account without a password",frappe.cache.get_value('one_time_login_key:'+key)==email,True)
		expect("email-link sign-in does not initiate password reset",bool(frappe.db.get_value('User',email,'reset_password_key')),False)
	finally:frappe.cache.delete_value('one_time_login_key:'+key)
