"""Check concrete native values against installed metadata before importing."""

from urllib.parse import urlsplit

from onerc_vmms_migrate import normalise

EMPTY = (None, "", [])


def profile_issues(profile, schemas, lookups, *, files=(), phone_check=None):
	issues = []
	indexes = {
		dt: rows if isinstance(rows, dict) else {str(r["name"]): r for r in rows}
		for dt, rows in lookups.items()
	}

	def issue(field, code, severity="error", **details):
		issues.append({"field": field, "code": code, "severity": severity, **details})

	def walk(doctype, values, path):
		fields = schemas.get(doctype)
		if fields is None:
			issue(path, "missing_target_doctype", target_doctype=doctype)
			return
		for name, value in values.items():
			if name.startswith("_") or value in EMPTY:
				continue
			fieldpath = path + "." + name
			if doctype == "Red Profile" and name in ("vmms_has_insurance", "consent_to_use_of_bio_data"):
				issue(fieldpath, "approved_exclusion_violation")
				continue
			meta = fields.get(name)
			if meta is None:
				issue(fieldpath, "missing_target_field")
				continue
			kind, options = meta["fieldtype"], meta.get("options")
			if kind in ("Table", "Table MultiSelect"):
				for index, row in enumerate(value, 1):
					walk(options, row, fieldpath + "[" + str(index) + "]")
			elif kind == "Select" and str(value) not in (options or "").splitlines():
				issue(fieldpath, "invalid_select")
			elif kind == "Link":
				if options not in indexes:
					issue(fieldpath, "link_lookup_not_checked", "review", target_doctype=options)
				elif str(value) not in indexes[options]:
					issue(fieldpath, "missing_link_target", target_doctype=options, target_name=str(value))
				elif "is_active" in indexes[options][str(value)] and not int(
					indexes[options][str(value)]["is_active"] or 0
				):
					issue(
						fieldpath,
						"inactive_link_target",
						"review",
						target_doctype=options,
						target_name=str(value),
					)
			elif kind in ("Data", "Password", "Link", "Dynamic Link"):
				limit = int(meta.get("length") or 140)
				if len(str(value)) > limit:
					issue(fieldpath, "text_too_long", length=len(str(value)), limit=limit)
			if kind == "Int":
				try:
					if int(value) != value or not -(2**31) <= int(value) < 2**31:
						issue(fieldpath, "integer_out_of_range")
				except ValueError, TypeError, OverflowError:
					issue(fieldpath, "invalid_integer")
			if kind == "Date" and normalise.date(value) is None:
				issue(fieldpath, "invalid_date")
			if kind == "Data" and options == "Phone" and phone_check is not None and not phone_check(value):
				issue(fieldpath, "invalid_phone")
			if kind == "Data" and options == "Email" and normalise.email(value) is None:
				issue(fieldpath, "invalid_email")
			if kind == "Data" and options == "URL":
				try:
					url = urlsplit(str(value))
					valid = (
						url.scheme in ("http", "https")
						and url.hostname
						and not url.username
						and not url.password
					)
				except ValueError:
					valid = False
				if not valid:
					issue(fieldpath, "invalid_url")
		for name, meta in fields.items():
			if meta.get("reqd") and values.get(name) in EMPTY:
				issue(path + "." + name, "missing_required_value")

	walk("Red Profile", profile, "Red Profile")
	planned = {
		f["index"]
		for f in files
		if f.get("purpose") == "table" and f.get("table") == "vmms_supporting_documents"
	}
	for index, row in enumerate(profile.get("vmms_supporting_documents") or []):
		policy = indexes.get("Supporting Document Type", {}).get(row.get("type"), {})
		if (
			policy.get("vmms_requires_document_name")
			and (row.get("attachment") or index in planned)
			and not row.get("document_name")
		):
			issue(
				"Red Profile.vmms_supporting_documents[" + str(index + 1) + "].document_name",
				"missing_document_name",
			)
	return issues


def installed_issues(bundles, conf=None):
	"""Read/cache site metadata and masters once for an entire person preflight."""
	if not bundles:
		return []
	import frappe
	from onerc_vmms_migrate.load import PROFILE_SCALARS, PROFILE_TABLES

	schemas, links = {}, set()
	pending = ["Red Profile"]
	while pending:
		doctype = pending.pop()
		if doctype in schemas or not frappe.db.exists("DocType", doctype):
			continue
		fields = {
			f.fieldname: {key: f.get(key) for key in ("fieldtype", "options", "reqd", "length")}
			for f in frappe.get_meta(doctype).fields
		}
		if doctype == "Red Profile":
			fields = {
				key: value
				for key, value in fields.items()
				if key in (*PROFILE_SCALARS, *PROFILE_TABLES, "email") or value.get("reqd")
			}
		schemas[doctype] = fields
		for meta in fields.values():
			if meta["fieldtype"] in ("Table", "Table MultiSelect") and meta.get("options"):
				pending.append(meta["options"])
			elif meta["fieldtype"] == "Link" and meta.get("options"):
				links.add(meta["options"])
	lookups = {}
	for doctype in sorted(links):
		if not frappe.db.exists("DocType", doctype):
			lookups[doctype] = []
			continue
		meta = frappe.get_meta(doctype)
		fields = ["name"] + [f for f in ("is_active", "vmms_requires_document_name") if meta.has_field(f)]
		lookups[doctype] = {str(row.name): row for row in frappe.get_all(doctype, fields=fields)}
	rows = [
		{"person": bundle["key"], **row}
		for bundle in bundles
		for row in profile_issues(
			bundle["profile"],
			schemas,
			lookups,
			files=bundle.get("files", ()),
			phone_check=frappe.utils.validate_phone_number,
		)
	]

	if (conf or {}).get("drop_invalid_phones"):
		for row in rows:
			if row["code"] == "invalid_phone":
				row["severity"] = "review"
	if (conf or {}).get("import_with_review"):
		for row in rows:
			if row["code"] == "missing_required_value" and row["field"] in ("Red Profile.first_name", "Red Profile.last_name"):
				row["severity"] = "review"
	return rows
