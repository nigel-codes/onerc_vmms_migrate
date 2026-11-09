"""Import historical facts with explicit field corrections, without made-up names."""

import json
from copy import deepcopy

NAME_FIELDS = {"Red Profile": ("first_name", "last_name"), "User": ("first_name",)}
REVIEW_FIELD = "data_review_values"


def record(bundle, field, value, reason, action="left blank for correction"):
    row = {"field": field, "original_value": value, "reason": reason, "action": action}
    values = bundle.setdefault("_review_values", [])
    if row not in values:
        values.append(row)
        bundle["warnings"].append(f"{field}: {reason}; {action}; original retained for correction")


def finalise(bundle):
    if bundle.get("_review_values"):
        bundle["profile"][REVIEW_FIELD] = json.dumps(bundle["_review_values"], ensure_ascii=False, sort_keys=True, default=str)


def prepare_bundle(bundle, schemas, phone_check=None):
    def walk(doctype, values, prefix=""):
        fields = schemas.get(doctype, {})
        for name, value in list(values.items()):
            if value in (None, "", []) or name.startswith("_"):
                continue
            path = prefix + name
            meta = fields.get(name, {})
            if isinstance(value, list):
                for index, child in enumerate(value, 1):
                    if isinstance(child, dict):
                        walk(meta.get("options", name), child, path + f"[{index}].")
            elif name == "maj_opt_subj" and len(str(value)) > int(meta.get("length") or 140):
                record(bundle, path, value, "text exceeds the destination field length")
                values[name] = None
            elif meta.get("options") == "Phone" and phone_check and not phone_check(value):
                original = values.get("_original_phone", value)
                record(bundle, path, original, "phone does not pass current validation")
                values[name] = None
    walk("Red Profile", bundle["profile"])
    finalise(bundle)


def prepare_installed(result, conf):
    if not conf.get("import_with_review"):
        return
    import frappe
    from onerc_vmms_migrate import load

    schemas, pending = {}, ["Red Profile"]
    while pending:
        doctype = pending.pop()
        if doctype in schemas:
            continue
        schemas[doctype] = {f.fieldname: f.as_dict() for f in frappe.get_meta(doctype).fields}
        pending.extend(f["options"] for f in schemas[doctype].values()
            if f.get("fieldtype") in ("Table", "Table MultiSelect") and f.get("options") and f["options"] not in schemas)
    for bundle in result["bundles"]:
        prepare_bundle(bundle, schemas, phone_check=frappe.utils.validate_phone_number)
        context = {"conf": {**conf, "drop_invalid_phones": True}, "warnings": []}
        load._review_profile_phones(context, bundle["profile"])
        for row in context.get("phone_review", []):
            record(bundle, row["field"], row["original_value"], "phone does not pass current validation")
        finalise(bundle)


def insert_with_missing_names(doc, enabled):
    """Relax only absent personal-name fields on this imported document instance."""
    if not enabled:
        doc.insert(ignore_permissions=True)
        return []
    missing = [name for name in NAME_FIELDS.get(doc.doctype, ()) if not doc.get(name)]
    if enabled and missing:
        doc.meta = deepcopy(doc.meta)
        for name in missing:
            doc.meta.get_field(name).reqd = 0
    doc.insert(ignore_permissions=True)
    return missing if enabled else []


class ImportedNamesMixin:
    def _get_missing_mandatory_fields(self):
        missing = super()._get_missing_mandatory_fields()
        names = NAME_FIELDS.get(self.doctype, ())
        if not missing or not names or self.is_new():
            return missing
        import frappe
        if not frappe.db.exists("Legacy Migration Record", {
                "source_doctype": "Person" if self.doctype == "Red Profile" else "User",
                "target_doctype": self.doctype, "target_name": self.name, "outcome": "created"}):
            return missing
        # Read persisted values, never a submitted flag. A known name cannot be
        # erased later, and a new/ordinary profile never receives this exception.
        previous = frappe.db.get_value(self.doctype, self.name, list(names), as_dict=True) or {}
        allowed = {name for name in names if not previous.get(name)}
        return [row for row in missing if row[0] not in allowed]
