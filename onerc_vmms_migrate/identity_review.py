"""Private, per-source identity exceptions; planned candidates are never imports."""

import hashlib
import json
from collections import Counter

from onerc_vmms_migrate import normalise

DATASETS = {"User": "users", "Employee": "employees", "Job Applicant": "applicants", "VM Member": "members", "VM Membership": "memberships"}
NAMES = ("first_name", "middle_name", "last_name", "surname", "other_names", "full_name", "employee_name", "applicant_name", "member_name")
EMAILS = ("email", "email_id", "user_id", "personal_email", "prefered_email", "company_email")


def rows(snapshot, built):
    output = []
    source = snapshot.get("source", {})
    namespace = str(source.get("source_id") or source.get("db_name") or "")
    blocked = {r["person"] for r in built.get("mapping_issues", []) if r["severity"] == "error"}
    aliases = {(r["source_doctype"], r["source_name"]): r for r in snapshot.get("_migration_identity_emails", [])}

    def append(dt, name, code, bundle=None, claim=None):
        bundle = bundle or {}
        original = snapshot.get(DATASETS[dt], {}).get(name, {})
        assigned = aliases.get((dt, name), {})
        email = (bundle.get("profile") or {}).get("email") or assigned.get("email") or ""
        person = bundle.get("key") or ""
        status = "eligible_pending_native_validation"
        if not bundle or bundle.get("errors") or person in blocked:
            status = "blocked_pending_resolution"
        elif dt == "Employee" and bundle.get("volunteer_excluded"):
            status = "volunteer_identity_pending"
        elif bundle.get("account_only"):
            status = "account_only_pending_transfer"
        placeholder = int(bool(assigned.get("is_placeholder")))
        output.append({
            "source_id": namespace, "source_doctype": dt, "source_name": name,
            "person": person, "issue_code": code,
            "conflict_group": hashlib.sha256((namespace + "\0" + code + "\0" + (
                str((claim["id_type"], normalise.key(claim["id_number"]))) if claim else person)).encode()).hexdigest(),
            "original_names": json.dumps({f: original[f] for f in NAMES if f in original}, ensure_ascii=False),
            "original_email_fields": json.dumps({f: original[f] for f in EMAILS if f in original}, ensure_ascii=False),
            "original_identification_fields": json.dumps({f: original[f] for f in
                ("identification_type", "id_number", "passport_number") if f in original}, ensure_ascii=False),
            "conflicting_identification": json.dumps(claim, ensure_ascii=False) if claim else "",
            "original_status": original.get("status", ""), "source_user_enabled": original.get("enabled", ""),
            "destination_email": email, "is_placeholder": placeholder,
            "outbound_email_allowed": int(bool(email) and not placeholder),
            "login_status": "source_login_pending_transfer" if bundle.get("user") else "no_login_created",
            "login_limitation": "email_delivery_blocked_until_real_address_verified" if placeholder else "",
            "source_password_status": "original_hash_captured" if snapshot.get("password_hashes", {}).get(name) and dt == "User" else "not_asserted",
            "native_status": status, "destination_profile": "", "destination_record": "",
            "resolution_status": "unresolved", "source_member": original.get("member", ""),
        })

    for bundle in built["bundles"]:
        sources = bundle.get("sources", [])
        counts = Counter(dt for dt, _ in sources)
        for dt, name in sources:
            codes = []
            if aliases.get((dt, name), {}).get("is_placeholder"):
                codes.append("placeholder_email")
            if any("no first and last name" in e for e in bundle["errors"]) or any(
                    r["field"] in ("first_name", "last_name") for r in bundle.get("_review_values", [])):
                codes.append("missing_name")
            if dt == "Employee" and counts[dt] > 1:
                codes.append("shared_volunteer_email")
            if dt == "VM Member" and counts[dt] > 1:
                codes.append("shared_member_email")
            for code in codes:
                append(dt, name, code, bundle)
            for claim in json.loads((bundle.get("profile") or {}).get("legacy_identity_claims") or "[]"):
                append(dt, name, "conflicting_identification", bundle, claim)
    for skipped in built.get("not_migrated", []):
        dt, name = skipped["source_doctype"], skipped["source_name"]
        if "no usable email" in skipped["reason"]:
            append(dt, name, "missing_email_unassigned")
        elif dt == "VM Membership" and "absent" in skipped["reason"]:
            append(dt, name, "missing_source_member")
    for user in snapshot["users"].values():
        if user["name"] not in ("Administrator", "Guest") and not (
                normalise.email(user.get("email")) or normalise.email(user["name"])):
            assigned = aliases.get(("User", user["name"]))
            linked = next((b for b in built["bundles"] if ("User", user["name"]) in b.get("sources", [])), None)
            if not linked and assigned:
                linked = {"key": assigned["email"], "profile": {"email": assigned["email"]},
                    "user": user, "errors": [], "account_only": True}
            append("User", user["name"], "missing_login_email", linked)
            if assigned and not any(r["source_doctype"] == "User" and r["source_name"] == user["name"]
                    and r["issue_code"] == "placeholder_email" for r in output):
                append("User", user["name"], "placeholder_email", linked)
    return output


def with_targets(review, ledger):
    profiles = {r["person_key"]: r["target_name"] for r in ledger if r["target_doctype"] == "Red Profile"}
    targets = {(r["source_doctype"], r["source_name"]): r for r in ledger
        if r["target_doctype"] != "Red Profile"}
    result = []
    for row in review:
        target = targets.get((row["source_doctype"], row["source_name"]), {})
        result.append({**row, "destination_profile": profiles.get(row["person"], ""),
            "destination_record": target.get("target_name", ""),
            "destination_doctype": target.get("target_doctype", ""),
            "record_status": "recorded_in_migration_ledger" if target else "native_record_not_found"})
    return result
