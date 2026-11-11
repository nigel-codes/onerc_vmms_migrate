"""Coordinator profiles must survive Frappe's native profile/role synchronisation."""

from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from frappe.core.doctype.user.user import User
from onerc_vmms_migrate import account_roles, load


class Account:
	name = "coordinator@example.test"
	role_profile_name = None

	def __init__(self, roles=()):
		self.roles = [frappe._dict(role=r) for r in roles]
		self.role_profiles = []

	def append(self, field, row):
		getattr(self, field).append(frappe._dict(row))

	def get(self, field):
		return getattr(self, field)

	append_roles = User.append_roles
	move_role_profile_name_to_role_profiles = User.move_role_profile_name_to_role_profiles
	populate_role_profile_roles = User.populate_role_profile_roles
	sync_role_profile_name = User.sync_role_profile_name

	def save(self, **kwargs):
		self.move_role_profile_name_to_role_profiles()
		self.populate_role_profile_roles()
		self.sync_role_profile_name()
		return self

	def add_roles(self, *roles):
		self.append_roles(*roles)
		self.save()


class TestApprovedAccountRoles(TestCase):
	def setUp(self):
		self.profiles = {}
		self.roles = {"County Coordinator", "HR Manager", "VMMS Volunteer", "Buzz User"}
		self.disabled = set()
		self.database = MagicMock()
		self.database.exists.side_effect = lambda dt, name: (
			name if name in (self.profiles if dt == "Role Profile" else self.roles) else None
		)
		self.database.get_value.side_effect = lambda *args, **kwargs: int(args[1] in self.disabled)
		self.enterContext(patch.object(frappe, "db", self.database))
		self.enterContext(patch.object(frappe, "get_doc", side_effect=self.document))
		self.enterContext(patch.object(frappe, "get_cached_doc", side_effect=self.document))

	def document(self, doctype, name=None):
		if isinstance(doctype, dict):
			self.assertEqual(doctype["doctype"], "Role Profile")
			doc = frappe._dict(doctype)
			doc.name = doc.role_profile
			doc.roles = [frappe._dict(row) for row in doc.roles]

			def insert(**kwargs):
				self.profiles[doc.name] = doc
				return doc

			doc.insert = insert
			return doc
		self.assertEqual(doctype, "Role Profile")
		return self.profiles[name]

	def profile(self, name, roles):
		self.document(
			{"doctype": "Role Profile", "role_profile": name, "roles": [{"role": role} for role in roles]}
		).insert()

	def test_missing_profile_is_created_from_existing_native_coordinator_role(self):
		user = Account()
		account_roles.apply(user, {"old_roles": ["Volunteer", "County Coordinator"]})
		self.assertEqual([p.role_profile for p in user.role_profiles], ["County Coordinator"])
		self.assertEqual({r.role for r in user.roles}, {"County Coordinator"})
		self.assertEqual({r.role for r in self.profiles["County Coordinator"].roles}, {"County Coordinator"})
		user.save()
		self.assertEqual(user.role_profile_name, "County Coordinator")

	def test_existing_profile_and_approved_extra_and_native_portal_roles_survive_resave_and_retry(self):
		self.profile("County Coordinator", ["County Coordinator"])
		user = Account(["VMMS Volunteer", "Buzz User"])
		login = {"old_roles": ["Volunteer", "County Coordinator", "HR Manager"]}
		account_roles.apply(user, login)
		expected = {"VMMS Volunteer", "Buzz User", "County Coordinator", "HR Manager"}
		self.assertEqual({r.role for r in user.roles}, expected)
		self.assertIn("County Coordinator", [p.role_profile for p in user.role_profiles])
		before = set(self.profiles)
		user.save()
		account_roles.apply(user, login)
		self.assertEqual({r.role for r in user.roles}, expected)
		self.assertEqual(set(self.profiles), before)
		self.assertEqual(len(user.role_profiles), 2)

	def test_missing_or_disabled_native_coordinator_role_requires_destination_setup(self):
		for disabled in (False, True):
			with self.subTest(disabled=disabled):
				self.roles.discard("County Coordinator")
				if disabled:
					self.roles.add("County Coordinator")
					self.disabled.add("County Coordinator")
				with self.assertRaisesRegex(ValueError, "County Coordinator.*role"):
					account_roles.apply(Account(), {"old_roles": ["County Coordinator"]})
				self.assertFalse(self.profiles)

	def test_existing_incorrect_profile_is_reported_without_modifying_it(self):
		self.profile("County Coordinator", ["HR Manager"])
		with self.assertRaisesRegex(ValueError, "profile.*County Coordinator"):
			account_roles.apply(Account(), {"old_roles": ["County Coordinator"]})
		self.assertEqual({r.role for r in self.profiles["County Coordinator"].roles}, {"HR Manager"})

	def test_plain_volunteer_does_not_inherit_legacy_desk_roles_or_profiles(self):
		user = Account(["VMMS Volunteer"])
		account_roles.apply(user, {"old_roles": ["Volunteer", "Employee", "Desk User"]})
		self.assertEqual({r.role for r in user.roles}, {"VMMS Volunteer"})
		self.assertFalse(user.role_profiles)
		self.assertFalse(self.profiles)

	def test_other_coordinators_do_not_inherit_somebody_elses_extra_staff_roles(self):
		first = Account()
		account_roles.apply(first, {"old_roles": ["County Coordinator", "HR Manager"]})
		second = Account()
		account_roles.apply(second, {"old_roles": ["County Coordinator"]})
		self.assertEqual({r.role for r in second.roles}, {"County Coordinator"})
		self.assertEqual({r.role for r in self.profiles["County Coordinator"].roles}, {"County Coordinator"})

	def test_modified_additional_profile_is_not_silently_reused(self):
		account_roles.apply(Account(), {"old_roles": ["County Coordinator", "HR Manager"]})
		extra = next(doc for name, doc in self.profiles.items() if name != "County Coordinator")
		extra.roles.append(frappe._dict(role="System Manager"))
		with self.assertRaisesRegex(ValueError, "differs from its approved role set"):
			account_roles.apply(Account(), {"old_roles": ["County Coordinator", "HR Manager"]})

	def test_source_coordinator_profile_alone_also_maps_to_the_native_profile(self):
		user = Account()
		account_roles.apply(user, {"old_roles": ["Volunteer"], "role_profile_name": "County Coordinator"})
		self.assertEqual(user.role_profile_name, "County Coordinator")
		self.assertEqual({r.role for r in user.roles}, {"County Coordinator"})

	def test_non_coordinator_staff_roles_are_retained_without_a_coordinator_profile(self):
		user = Account()
		account_roles.apply(user, {"old_roles": ["HR Manager"]})
		self.assertEqual({r.role for r in user.roles}, {"HR Manager"})
		self.assertFalse(user.role_profiles)

	def test_role_profiles_are_applied_after_native_affiliation_roles_are_derived(self):
		user = Account(["Buzz User"])

		def create_user(ctx):
			self.assertTrue(ctx.get("defer_account_roles"))
			ctx["account_roles_pending"] = True
			return user.name

		def get_document(dt, name=None):
			if dt == "VMMS Volunteer":
				return frappe._dict(name=name)
			return user if dt == "User" else self.document(dt, name)

		bundle = {
			"user": {"old_roles": ["County Coordinator"]},
			"profile": {},
			"volunteer": True,
			"member": None,
		}
		with (
			patch.object(load, "Guard"),
			patch.object(load, "_user", side_effect=create_user),
			patch.object(load, "_profile", return_value="profile"),
			patch.object(load, "_files"),
			patch.object(load, "_volunteer", return_value="volunteer"),
			patch(
				"vmmsx.volunteer.services.volunteer.refresh",
				side_effect=lambda _: user.add_roles("VMMS Volunteer"),
			),
			patch("vmmsx.cards.services.token.ensure"),
			patch("onerc_vmms_migrate.import_review.finalise"),
			patch.object(frappe, "get_doc", side_effect=get_document),
		):
			load.load_bundle(bundle, None, {}, "run")
		self.assertEqual({r.role for r in user.roles}, {"Buzz User", "VMMS Volunteer", "County Coordinator"})
		self.assertEqual(user.role_profile_name, "County Coordinator")

	def test_adopted_users_keep_their_existing_role_policy(self):
		with (
			patch.object(load, "Guard"),
			patch.object(load, "_user", return_value="existing@example.test"),
			patch.object(load, "_profile", return_value="profile"),
			patch.object(load, "_files"),
			patch("onerc_vmms_migrate.import_review.finalise"),
			patch.object(account_roles, "apply") as roles,
		):
			load.load_bundle({"user": {}, "profile": {}, "volunteer": None, "member": None}, None, {}, "run")
		roles.assert_not_called()
