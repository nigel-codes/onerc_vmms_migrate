"""Native registration facts. Historical answers never create new consent."""

import re
from urllib.parse import urlsplit

from onerc_vmms_migrate import normalise

VERSION = 2
APPROVED_EXCLUSIONS = frozenset({"has_insurance", "consent_to_use_of_bio_data"})
DIRECT_SCALARS = {
	"blood_group": "vmms_blood_group",
	"number_of_dependants": "vmms_number_of_dependants",
	"access_to_internet": "vmms_access_to_internet",
	"place_of_work": "vmms_place_of_work",
	"mpesa_mobile_phone": "vmms_mpesa_mobile_phone",
	"linkedin": "vmms_linkedin",
	"github": "vmms_github",
	"county": "vmms_residence_county",
	"sub_county": "vmms_residence_sub_county",
	"ward": "vmms_residence_ward",
	"administrative_location": "vmms_residence_location",
	"sub_location": "vmms_residence_sub_location",
}


def first_value(field, *records):
	return next((r[field] for r in records if r and r.get(field) not in (None, "")), None)


def choice(value, options):
	"""Normalize equivalent casing only; never guess a different answer."""
	value = str(value)
	return next((option for option in options if option.casefold() == value.casefold()), None)


def personal_details(bundle, profile, user, employee, applicant, conf, cw=None):
	for source, target in DIRECT_SCALARS.items():
		if source in ("ward","administrative_location","sub_location") or (source in ("county","sub_county") and source not in conf.get("residence_levels", ("county","sub_county"))):
			continue
		# Employee has blood_group; the other registration answers belong to
		# User/Applicant. Preserve this explicit source precedence.
		records = (user, applicant, employee) if source == "blood_group" else (user, applicant)
		value = first_value(source, *records)
		if value is None:
			continue
		if source == "number_of_dependants":
			if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value).strip()):
				if conf.get("import_with_review"):
					from onerc_vmms_migrate.import_review import record
					record(bundle, target, value, "not a non-negative whole number")
					profile["vmms_dependants_answered"] = 0
					continue
				bundle["errors"].append(
					"number_of_dependants is not a non-negative whole number; original retained"
				)
				continue
			profile[target] = int(value)
			# Old Int default 0 does not establish an explicit answer. Preserve
			# the raw value while the new profile DTO continues to show unknown.
			profile["vmms_dependants_answered"] = int(int(value) > 0)
			continue
		if source == "access_to_internet":
			answer=value if value in ("Yes","No","Sometimes") else None
			if answer is None and cw:
				from onerc_vmms_migrate.crosswalk import DROP,UNMAPPED
				mapped=cw.lookup("internet_access",value)
				answer=mapped if mapped and mapped not in (DROP,UNMAPPED) else None
			if answer is None:
				if conf.get("import_with_review"):
					from onerc_vmms_migrate.import_review import record
					record(bundle,target,value,"Unsupported internet-access choice")
				else:bundle["errors"].append("access_to_internet has an unsupported choice; original retained")
				continue
			value = answer
		elif source == "mpesa_mobile_phone":
			original=value
			value = normalise.phone(value, conf.get("default_country_code"))
			if original != value:
				from onerc_vmms_migrate.import_review import record
				record(bundle,target,original,"Original phone form retained","original retained alongside payment phone")
			if not value:
				if conf.get("import_with_review"):
					from onerc_vmms_migrate.import_review import record
					record(bundle,target,original,"No usable payment phone")
				else:bundle["errors"].append("mpesa_mobile_phone has no usable number; original retained")
				continue
		elif source in ("linkedin", "github"):
			value = str(value)
			try:
				parts = urlsplit(value)
				valid = (
					parts.scheme in ("http", "https")
					and parts.hostname
					and not parts.username
					and not parts.password
				)
			except ValueError:
				valid = False
			if not valid:
				if conf.get("import_with_review"):
					from onerc_vmms_migrate.import_review import record
					record(bundle, target, value, "not a supported profile URL")
					continue
				bundle["errors"].append(
					source + " is not a supported http/https profile URL; original retained"
				)
				continue
		else:
			value = str(value)
		if value not in (None, ""):
			profile[target] = value
