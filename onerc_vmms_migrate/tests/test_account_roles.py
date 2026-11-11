from unittest import TestCase
from unittest.mock import MagicMock, patch
import frappe
from onerc_vmms_migrate.account_roles import apply


class TestApprovedAccountRoles(TestCase):
	def test_coordinator_gets_native_profile_and_other_roles_survive(self):
		user = MagicMock()
		db = MagicMock()
		db.exists.return_value = "County Coordinator"
		with patch.object(frappe, "db", db):
			apply(user, {"old_roles": ["Volunteer", "County Coordinator", "HR Manager"]})
		self.assertEqual(user.role_profile_name, "County Coordinator")
		user.add_roles.assert_called_once_with("HR Manager")

	def test_plain_volunteer_does_not_inherit_legacy_desk_roles(self):
		user = MagicMock()
		with patch.object(frappe, "db", MagicMock()):
			apply(user, {"old_roles": ["Volunteer", "Employee", "Desk User"]})
		user.add_roles.assert_not_called()
		user.save.assert_not_called()

	def test_absent_coordinator_profile_is_reported_without_creating_an_incorrect_profile(self):
		db = MagicMock()
		db.exists.return_value = None
		with patch.object(frappe, "db", db), self.assertRaisesRegex(ValueError, "profile is absent"):
			apply(MagicMock(), {"old_roles": ["County Coordinator"]})
