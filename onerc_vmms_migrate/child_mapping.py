"""Declared person evidence retains its native table and private file binding."""

import json

from onerc_vmms_migrate import normalise

NATIVE_TABLES = {
	"certification": "vmms_certifications",
	"additional_skills": "vmms_additional_skills",
	"allergies": "vmms_allergies",
	"supporting_documents": "vmms_supporting_documents",
}
BOOKKEEPING = frozenset(
	{
		"name",
		"parent",
		"parenttype",
		"parentfield",
		"idx",
		"creation",
		"modified",
		"owner",
		"modified_by",
		"docstatus",
	}
)


def selected_rows(bundle, field, *records):
	tables = [r.get(field) for r in records if r and r.get(field)]
	if not tables:
		return []

	def signature(row):
		return json.dumps({k: v for k, v in row.items() if k not in BOOKKEEPING}, sort_keys=True, default=str)

	chosen = {signature(row) for row in tables[0]}
	if any({signature(row) for row in table} - chosen for table in tables[1:]):
		bundle["warnings"].append(
			field
			+ ": distinct rows in another source snapshot remain in application history; current profile uses the recorded source precedence"
		)
	return tables[0]


def details(bundle, profile, user, applicant):
	for source, field in (("additional_skills", "additional_skill"), ("allergies", "allergy")):
		profile[NATIVE_TABLES[source]] = [
			{field: normalise.text(row.get(field))}
			for row in selected_rows(bundle, source, user, applicant)
			if normalise.text(row.get(field))
		]
	certifications = []
	for row in selected_rows(bundle, "certification", user, applicant):
		name = normalise.text(row.get("certification_name"))
		if not name:
			bundle["errors"].append("certification row has no certification_name; original retained")
			continue
		item = {
			field: normalise.text(row.get(field))
			for field in ("certification_name", "organization", "description")
		}
		item["expire"] = int(str(row.get("expire") or 0) == "1")
		for field in ("issue_date", "expiration_date"):
			item[field] = normalise.date(row.get(field))
			if row.get(field) and item[field] is None:
				bundle["errors"].append("certification " + field + " is invalid; original retained")
		certifications.append(item)
	profile["vmms_certifications"] = certifications
	profile["vmms_supporting_documents"] = []
	for row in selected_rows(bundle, "supporting_documents", user, applicant):
		item = {
			field: normalise.text(row.get(field)) for field in ("type", "document_name") if row.get(field)
		}
		if not item and not row.get("attachment"):
			continue
		index = len(profile["vmms_supporting_documents"])
		profile["vmms_supporting_documents"].append(item)
		if row.get("attachment"):
			bundle["files"].append(
				{
					"url": row["attachment"],
					"purpose": "table",
					"table": "vmms_supporting_documents",
					"index": index,
				}
			)
