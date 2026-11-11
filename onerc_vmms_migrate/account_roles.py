"""Approved account policy: native self-service, County Coordinator profile, other roles retained."""

import hashlib
import json

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


def _profile_roles(name):
	return {row.role for row in frappe.get_doc("Role Profile", name).roles}


def _create_profile(name, roles):
	frappe.get_doc(
		{
			"doctype": "Role Profile",
			"role_profile": name,
			"roles": [{"role": role} for role in sorted(roles)],
		}
	).insert(ignore_permissions=True)
	return name


def coordinator_profile():
	# Kenya's site setup installs the role and its permissions, but no Role Profile.
	# A profile bundles existing roles; never invent an unconfigured staff role.
	if not frappe.db.exists("Role", COORDINATOR) or frappe.db.get_value("Role", COORDINATOR, "disabled"):
		raise ValueError(
			"The destination County Coordinator role is absent or disabled; complete Kenya leadership-role setup first."
		)
	if not frappe.db.exists("Role Profile", COORDINATOR):
		return _create_profile(COORDINATOR, {COORDINATOR})
	if COORDINATOR not in _profile_roles(COORDINATOR):
		raise ValueError(
			"The destination County Coordinator profile does not include the County Coordinator role."
		)
	return COORDINATOR


def _assign_profiles(user, profile, retained):
	assigned = {row.role_profile for row in user.role_profiles}
	if profile not in assigned:
		user.append("role_profiles", {"role_profile": profile})
		assigned.add(profile)
	covered = set().union(*(_profile_roles(name) for name in assigned))
	additional = ({row.role for row in user.roles} | set(retained)) - covered - {"All", "Guest"}
	if additional:
		# Frappe synchronises a profiled user's roles from ALL assigned profiles on
		# every save. Keep approved extra staff roles and native self-service roles
		# in a separate, shared bundle rather than broadening every coordinator.
		digest = hashlib.sha256(json.dumps(sorted(additional)).encode()).hexdigest()[:24]
		name = "Additional staff roles " + digest
		if frappe.db.exists("Role Profile", name):
			if _profile_roles(name) != additional:
				raise ValueError(
					"An additional staff role profile differs from its approved role set: " + name
				)
		else:
			_create_profile(name, additional)
		if name not in assigned:
			user.append("role_profiles", {"role_profile": name})
	user.save(ignore_permissions=True)


def apply(user, login):
	source = set(login.get("old_roles") or [])
	is_coordinator = COORDINATOR in source or login.get("role_profile_name") == COORDINATOR
	profile = coordinator_profile() if is_coordinator else None
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
	if profile:
		_assign_profiles(user, profile, retained)
	elif retained:
		user.add_roles(*retained)
