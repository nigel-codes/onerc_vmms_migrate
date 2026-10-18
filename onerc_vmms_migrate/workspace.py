"""Where a migration keeps its configuration, crosswalks and reports.

Everything lives under the target site's `private/onerc_vmms_migrate/`, which
the web server does not serve (only `private/files/` is routed, and only through
a permission check). Nothing about a migration is stored in this repository:
the crosswalks are a society's decisions and the reports name real people.

Configuration is one key in the target site's `site_config.json`:

    bench --site <target> set-config -p onerc_vmms_migrate '{
        "source_site_path": "/home/nigel/frappe/frappe-bench/sites/oldprod.localhost",
        "default_country_code": "254"
    }'

`source_site_path` is a site directory on the *old* bench. Its
`site_config.json` gives the database credentials, and its `private/files` and
`public/files` hold the attachments. The old bench is never run: the database
is read with plain SQL, which is why a v15 source and a develop target do not
have to agree on anything.
"""

import json
import os

import frappe

CONFIG_KEY = "onerc_vmms_migrate"

DEFAULTS = {
	# Where the old site lives on disk (required).
	"source_site_path": None,
	# Overrides for the source database connection. Normally read from the
	# source site's own config and the old bench's common_site_config.
	"source_db_host": None,
	"source_db_port": None,
	# Used to turn local numbers such as 0712 345 678 into +254712345678.
	"default_country_code": None,
	# A phone the society's pattern refuses: reject the person (strict) or
	# load them without a phone and list them in warnings.csv.
	"drop_invalid_phones": False,
	# HRMS kept one emergency contact on the Employee. Whether the person agreed
	# to that contact being called is not recorded there, so it is a decision.
	"emergency_contact_may_call": False,
	# Values people typed to mean "nothing" (compared case-insensitively). A
	# placeholder is treated as an empty field: it is not looked up, not
	# carried, and cannot make two people look like one. Counted per kind in
	# plan.json so the choice stays visible.
	"placeholder_values": [
		"none",
		"nil",
		"null",
		"n/a",
		"na",
		"no",
		"unknown",
		"not applicable",
		"not available",
		"-",
		"--",
		".",
		"0",
	],
}


def config() -> frappe._dict:
	conf = frappe._dict(DEFAULTS)
	conf.update(frappe.conf.get(CONFIG_KEY) or {})
	if frappe.db.exists("DocType", "VMMS Migration Settings"):
		settings = frappe.get_single("VMMS Migration Settings")
		if settings.browser_capture:
			conf.update(
				{
					"browser_capture": settings.browser_capture,
					"default_country_code": settings.default_country_code,
					"drop_invalid_phones": bool(settings.drop_invalid_phones),
					"emergency_contact_may_call": bool(settings.emergency_contact_may_call),
					"preserve_passwords": bool(settings.get("preserve_passwords")),
					"allow_missing_files": bool(settings.get("allow_missing_files")),
					"allow_source_exclusions": bool(settings.get("allow_source_exclusions")),
					"restore_email_link_login": bool(settings.get("restore_email_link_login")),
				}
			)
	return conf


def root() -> str:
	path = frappe.get_site_path("private", "onerc_vmms_migrate")
	os.makedirs(path, exist_ok=True)
	os.chmod(path,0o700)
	return path


def path(*parts: str) -> str:
	full = os.path.join(root(), *parts)
	os.makedirs(os.path.dirname(full), exist_ok=True)
	return full


def crosswalk_dir() -> str:
	full = os.path.join(root(), "crosswalks")
	os.makedirs(full, exist_ok=True)
	os.chmod(full,0o700)
	return full


def report_dir(run_id: str) -> str:
	full = os.path.join(root(), "reports", run_id)
	os.makedirs(full, exist_ok=True)
	os.chmod(full,0o700)
	return full


def write_json(target: str, data) -> str:
	with open(target, "w") as handle:
		os.chmod(target,0o600)
		json.dump(data, handle, indent=1, default=str, sort_keys=True)
	return target
