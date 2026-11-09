"""Stable, opt-in aliases for source records without a usable email.

The registry is private migration state, independent of captures and native
transactions. Preserve it with the captured files when moving or restoring a
site. Only explicit source links join records; names and phones never do.
"""

import json
import re
from collections import defaultdict

from onerc_vmms_migrate import normalise

PATTERN = re.compile(r"missingemail[1-9][0-9]*@vmms\.com", re.IGNORECASE)
DATASETS = {"Employee": "employees", "VM Member": "members", "Job Applicant": "applicants", "User": "users"}


def source_id(snapshot):
    source = snapshot.get("source", {})
    value = source.get("source_id") or source.get("db_name")
    if not value:
        raise ValueError("Placeholder emails require a stable source identity.")
    return str(value)


def key(row):
    return row["source_id"], row["source_doctype"], row["source_name"]


def missing_groups(snapshot):
    from onerc_vmms_migrate.transform import employee_email, member_email

    records, anchors = {}, defaultdict(set)
    for dt, dataset in DATASETS.items():
        for row in snapshot[dataset].values():
            if dt == "User" and row["name"] in ("Administrator", "Guest"):
                continue
            if dt in ("Employee", "Job Applicant") and not row.get("is_volunteer"):
                continue
            email = (employee_email(snapshot, row) if dt == "Employee" else
                member_email(snapshot, row) if dt == "VM Member" else
                normalise.email(row.get("email")) or normalise.email(row["name"]) if dt == "User" else
                normalise.email(row.get("email_id")))
            if not email:
                records[dt, row["name"]] = row
    parents = {k: k for k in records}

    def find(k):
        while parents[k] != k:
            parents[k] = parents[parents[k]]
            k = parents[k]
        return k

    def join(left, right):
        if left in parents and right in parents:
            a, b = find(left), find(right)
            parents[max(a, b)] = min(a, b)

    users = {}
    for identity, row in records.items():
        if identity[0] == "VM Member":
            join(identity, ("Employee", row.get("volunteer")))
        field = "user_id" if identity[0] == "Employee" else "email_id"
        user = row.get(field)
        if user and user not in ("Administrator", "Guest") and user in snapshot["users"]:
            join(identity, ("User", user))
            if user in users:
                join(identity, users[user])
            users[user] = identity
    for employee in snapshot["employees"].values():
        applicant = snapshot["applicants"].get(employee.get("job_applicant"))
        if not employee.get("is_volunteer") or not applicant:
            continue
        left, right = ("Employee", employee["name"]), ("Job Applicant", applicant["name"])
        join(left, right)
        for identity, email in ((left, normalise.email(applicant.get("email_id"))),
                (right, employee_email(snapshot, employee))):
            if identity in parents and email:
                anchors[identity].add(email)
    groups = defaultdict(list)
    for identity in sorted(records):
        groups[find(identity)].append(identity)
    return list(groups.values()), anchors


def source_emails(snapshot):
    used = set()
    for dataset in DATASETS.values():
        for row in snapshot[dataset].values():
            for field in ("email", "email_id", "personal_email", "prefered_email", "company_email", "user_id"):
                if email := normalise.email(row.get(field)):
                    used.add(email)
            if dataset == "users" and (email := normalise.email(row.get("name"))):
                used.add(email)
    return used


def allocate(snapshot, existing=(), reserved=()):
    """Return the complete registry, retaining assignments from earlier sources."""
    rows = [dict(row) for row in existing]
    if len({key(row) for row in rows}) != len(rows) or any(
            not PATTERN.fullmatch(str(row.get("email", ""))) for row in rows):
        raise ValueError("The placeholder email registry is invalid; review it before retrying.")
    by_key = {key(row): row["email"].lower() for row in rows}
    claimed = source_emails(snapshot)
    if claimed.intersection(by_key.values()):
        raise ValueError("A placeholder address is now claimed by a source record; reconcile it before retrying.")
    used = claimed | set(by_key.values()) | {normalise.email(v) for v in reserved}
    namespace = source_id(snapshot)
    groups, anchors = missing_groups(snapshot)
    owners = defaultdict(set)
    component = {identity: index for index, group in enumerate(groups) for identity in group}
    for row in rows:
        identity = row["source_doctype"], row["source_name"]
        owner = (row["source_id"], component.get(identity)) if row["source_id"] == namespace else (row["source_id"], None)
        owners[row["email"].lower()].add(owner)
    if any(len(value) > 1 for value in owners.values()):
        raise ValueError("A placeholder address is assigned to unlinked identities; review the registry.")
    number = 1
    for identities in sorted(groups):
        prior = {by_key[namespace, dt, name] for dt, name in identities if (namespace, dt, name) in by_key}
        if len(prior) > 1:
            raise ValueError("New source links join previously distinct placeholder identities; review them first.")
        linked = set().union(*(anchors.get(identity, set()) for identity in identities))
        if linked:
            if len(linked) > 1 or prior:
                raise ValueError("A missing-email identity has conflicting explicit email links; review them first.")
            continue
        if prior:
            email = next(iter(prior))
        else:
            while f"missingemail{number}@vmms.com" in used:
                number += 1
            email = f"missingemail{number}@vmms.com"
            used.add(email)
            number += 1
        for dt, name in identities:
            identity = namespace, dt, name
            if identity not in by_key:
                rows.append({"source_id": namespace, "source_doctype": dt, "source_name": name, "email": email})
                by_key[identity] = email
    return sorted(rows, key=key)


def registry_path():
    from pathlib import Path
    from onerc_vmms_migrate import workspace

    return Path(workspace.path("placeholder-emails.json"))


def read_registry(path):
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    if data.get("version") != 1 or not isinstance(data.get("assignments"), list):
        raise ValueError("Unsupported placeholder email registry; review it before retrying.")
    return data["assignments"]


def blocked_addresses():
    path = registry_path()
    if not path.exists():
        raise ValueError("The placeholder email registry is missing; restore it before sending to reserved aliases.")
    rows = read_registry(path)
    if any(not PATTERN.fullmatch(str(row.get("email", ""))) for row in rows):
        raise ValueError("The placeholder email registry is invalid; email delivery remains blocked.")
    return {row["email"].lower() for row in rows}


def login_email(snapshot, user):
    return normalise.email(user.get("email")) or normalise.email(user["name"]) or next(
        (r["email"] for r in snapshot.get("_migration_identity_emails", [])
            if r["source_doctype"] == "User" and r["source_name"] == user["name"]), None)


def prepare(snapshot, conf, *, reserved=None):
    """Resolve aliases once before transforming; never change captured records."""
    if not conf.get("assign_missing_emails"):
        snapshot.pop("_migration_identity_emails", None)
        return
    import fcntl
    import os
    import tempfile

    if reserved is None:
        import frappe
        reserved = [*frappe.get_all("User", pluck="name"), *frappe.get_all("Red Profile", pluck="email")]
    path = registry_path()
    descriptor = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = allocate(snapshot, read_registry(path), reserved)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
            temporary = handle.name
            try:
                os.chmod(temporary, 0o600)
                json.dump({"version": 1, "assignments": rows}, handle, sort_keys=True, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    namespace = source_id(snapshot)
    groups, anchors = missing_groups(snapshot)
    missing = {identity for group in groups for identity in group}
    active = [dict(row, is_placeholder=1) for row in rows
        if row["source_id"] == namespace and (row["source_doctype"], row["source_name"]) in missing]
    for group in groups:
        linked = set().union(*(anchors.get(identity, set()) for identity in group))
        if linked:
            active.extend({"source_id": namespace, "source_doctype": dt, "source_name": name,
                "email": next(iter(linked)), "is_placeholder": 0} for dt, name in group)
    snapshot["_migration_identity_emails"] = active
