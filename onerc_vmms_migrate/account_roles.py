"""Approved account policy: native self-service, County Coordinator profile, other roles retained."""

import frappe

PORTAL_ROLES = {
	"All",
	"Guest",
	"Volunteer",
	"Member",
	"VMMS Volunteer",
	"VMMS Member",
	"Vmms Guest",
	"Employee",
	"Employee Self Service",
	"LMS Student",
	"Desk User",
	"Website User",
	"Buzz User",
}
COORDINATOR = "County Coordinator"


def apply(user, login):
	source = set(login.get("old_roles") or [])
	if COORDINATOR in source or login.get("role_profile_name") == COORDINATOR:
		profile = frappe.db.exists("Role Profile", COORDINATOR)
		if not profile:
			raise ValueError(
				"The destination County Coordinator role profile is absent; seed it before importing coordinators."
			)
		user.role_profile_name = profile
		user.save(ignore_permissions=True)
	retained = sorted(source - PORTAL_ROLES - {COORDINATOR})
	for role in retained:
		if not frappe.db.exists("Role", role):
			frappe.get_doc(
				{
					"doctype": "Role",
					"role_name": role,
					"desk_access": int(login.get("user_type") == "System User"),
				}
			).insert(ignore_permissions=True)
	if retained:
		user.add_roles(*retained)
