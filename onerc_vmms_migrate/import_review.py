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


def prepare_dates(bundle, schemas, pairs, ends_before):
    """Use the installed product's date rules; preserve rejected ends for review."""
    for table, heading, start, start_label, end, end_label, open_flag, years in pairs:
        child_type = schemas.get("Red Profile", {}).get(table, {}).get("options", table)
        for index, row in enumerate(bundle["profile"].get(table) or [], 1):
            if open_flag and row.get(open_flag):
                continue
            if table == "vmms_certifications" and "expire" in schemas.get(child_type, {}) and not row.get("expire"):
                continue
            if ends_before(row.get(start), row.get(end), years):
                record(bundle, f"{table}[{index}].{end}", row[end],
                    f"{end_label} precedes recorded {start_label} ({row[start]})")
                # Preserve both recorded dates; the narrow imported-profile validator
                # below allows only these exact, provenance-backed pairs.


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
            elif meta.get("fieldtype")=="Data" and len(str(value)) > int(meta.get("length") or 140) or name == "maj_opt_subj" and len(str(value)) > int(meta.get("length") or 140):
                record(bundle, path, value, "text exceeds the destination field length")
                values[name] = None
            elif meta.get("fieldtype")=="Select" and str(value) not in (meta.get("options") or "").splitlines():
                record(bundle,path,value,"Value is not in the installed destination vocabulary")
                values[name]=None
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
    from vmmsx.registration.services.dates import PAIRS, ends_before

    schemas, pending = {}, ["Red Profile"]
    while pending:
        doctype = pending.pop()
        if doctype in schemas:
            continue
        schemas[doctype] = {f.fieldname: f.as_dict() for f in frappe.get_meta(doctype).fields}
        pending.extend(f["options"] for f in schemas[doctype].values()
            if f.get("fieldtype") in ("Table", "Table MultiSelect") and f.get("options") and f["options"] not in schemas)
    for bundle in result["bundles"]:
        prepare_dates(bundle, schemas, PAIRS, ends_before)
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


class ImportedContactMixin:
    def _get_missing_mandatory_fields(self):
        missing=super()._get_missing_mandatory_fields()
        import frappe
        fields=("contact_name","relationship","primary_phone")
        if frappe.flags.vmms_migration_import:
            return [row for row in missing if row[0] not in fields]
        if self.is_new() or not frappe.db.exists("Legacy Migration Record",{
                "target_doctype":self.parenttype,"target_name":self.parent}):
            return missing
        previous=frappe.db.get_value(self.doctype,self.name,list(fields),as_dict=True) or {}
        return [row for row in missing if row[0] not in fields or previous.get(row[0])]


class ImportedEducationMixin:
    def _get_missing_mandatory_fields(self):
        missing=super()._get_missing_mandatory_fields()
        import frappe
        if not self.get("legacy_institution"):
            return missing
        proven=frappe.flags.vmms_migration_import or (not self.is_new() and frappe.db.exists(
            "Legacy Migration Record",{"target_doctype":"Red Profile","target_name":self.parent}))
        return [row for row in missing if row[0]!="institution"] if proven else missing


class ImportedDatesMixin:
    def run_method(self, method, *args, **kwargs):
        if method != "validate":return super().run_method(method,*args,**kwargs)
        import frappe
        from vmmsx.registration.services import dates
        allowed=set()
        proven=frappe.flags.vmms_migration_import
        if not proven and not self.is_new():
            proven=frappe.db.exists("Legacy Migration Record",{"target_doctype":"Red Profile","target_name":self.name,"outcome":"created"})
        raw=self.get(REVIEW_FIELD)
        try:review=json.loads(raw or "[]")
        except (ValueError,TypeError):review=[]
        original=dates.ends_before
        for table,_heading,start,_sl,end,_el,_open,years in dates.PAIRS:
            for index,row in enumerate(self.get(table) or [],1):
                if proven and any(r.get("field")==f"{table}[{index}].{end}" and str(r.get("original_value"))==str(row.get(end))
                        and f"({row.get(start)})" in r.get("reason","") for r in review):
                    persisted=frappe.db.get_value(row.doctype,row.name,[start,end]) if not frappe.flags.vmms_migration_import else None
                    if frappe.flags.vmms_migration_import or persisted and tuple(map(str,persisted)) == (str(row.get(start)),str(row.get(end))):
                        allowed.add((str(row.get(start)),str(row.get(end)),years))
        def checked(start,end,years=False):
            return False if (str(start),str(end),years) in allowed else original(start,end,years)
        try:
            dates.ends_before=checked
            return super().run_method(method,*args,**kwargs)
        finally:dates.ends_before=original


class ImportedOrphanApplicationMixin:
    def on_update(self):
        import frappe
        # A persisted historical Approved application must not allocate a volunteer
        # on an unrelated later save. No request flag can grant this exception.
        if (not self.get("volunteer") and self.get("legacy_orphan_application") and
                frappe.db.get_value(self.doctype,self.name,"legacy_orphan_application") and
                frappe.db.exists("Legacy Migration Record",{"source_doctype":"Job Applicant",
                    "target_doctype":self.doctype,"target_name":self.name})):
            return
        return super().on_update()


class ImportedDecisionMixin:
    def _get_missing_mandatory_fields(self):
        missing=super()._get_missing_mandatory_fields()
        import frappe
        if not self.get("legacy_source_decision"):return missing
        proven=frappe.flags.vmms_migration_import or (not self.is_new() and
            frappe.db.get_value(self.doctype,self.name,"legacy_source_decision") and
            frappe.db.exists("Legacy Migration Record",{"target_doctype":self.parenttype,"target_name":self.parent}))
        return [row for row in missing if row[0] not in ("stage","approver","decided_on")] if proven else missing


class ImportedProfileChildMixin:
    def _get_missing_mandatory_fields(self):
        missing=super()._get_missing_mandatory_fields()
        import frappe
        if self.get("parenttype")!="Red Profile":return missing
        if frappe.flags.vmms_migration_import:return []
        if self.is_new() or not frappe.db.exists("Legacy Migration Record",{
                "target_doctype":"Red Profile","target_name":self.parent,"outcome":"created"}):return missing
        previous=frappe.db.get_value(self.doctype,self.name,[row[0] for row in missing],as_dict=True) if missing else {}
        return [row for row in missing if previous and previous.get(row[0])]


class ImportedAnchorMixin:
    def validate_anchor(self):
        import frappe
        field='home_geo_node' if self.doctype=='VMMS Volunteer' else 'geo_node'
        value=self.get(field)
        trusted=frappe.flags.vmms_migration_import
        if not trusted and not self.is_new():
            trusted=(frappe.db.get_value(self.doctype,self.name,field)==value and
                frappe.db.exists('Legacy Migration Record',{'target_doctype':self.doctype,'target_name':self.name}))
        # Crosswalked historical national/region anchors may differ from today's
        # county-only signup policy. A missing/invalid node is never exempted.
        if trusted and value and frappe.db.exists('Geo Node',value):return
        return super().validate_anchor()


class ImportedDisabilityMixin:
    def get_invalid_links(self,*args,**kwargs):
        import frappe
        proven=frappe.flags.vmms_migration_import or (not self.is_new() and self.parenttype=='Red Profile'
            and frappe.db.exists('Legacy Migration Record',{'target_doctype':'Red Profile','target_name':self.parent}))
        if not proven or not self.get('disability_name'):return super().get_invalid_links(*args,**kwargs)
        original=self.meta
        try:
            self.meta=deepcopy(original)
            self.meta.get_field('disability_name').fetch_if_empty=1
            return super().get_invalid_links(*args,**kwargs)
        finally:self.meta=original
