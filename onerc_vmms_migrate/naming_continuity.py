"""Reserve destination counters for preserved names using captured source counters."""

import re
from functools import lru_cache

import frappe

DATE_PARTS = {"YYYY": 4, "YY": 2, "MM": 2, "DD": 2, "WW": 2, "JJJ": 3}
PEOPLE = {
    "Employee": ("VMMS Volunteer", "employees"),
    "VM Member": ("VMMS Member", "members"),
    "VM Membership": ("VMMS Membership", "memberships"),
}


def prefix_pattern(prefix):
    """Match literal and date parts in the Frappe series used by the adapters."""
    return "".join(
        f"[0-9]{{{DATE_PARTS[part]}}}" if part in DATE_PARTS else re.escape(part)
        for part in prefix.split(".")
    )


def series_pattern(pattern):
    # Standard series finish with a counter. Prompt, field and hash names
    # have no counter here. A different naming convention needs review.
    parts = pattern.split(".")
    if not re.fullmatch(r"#+", parts[-1]) or any(
        not re.fullmatch(r"[A-Za-z0-9_ /-]*", part) for part in parts[:-1]
    ):
        return None
    return prefix_pattern(".".join(parts[:-1])), len(parts[-1])


def match_number(pattern, digits, name):
    # Frappe pads to the configured width but emits more digits if necessary.
    match = re.fullmatch(rf"({pattern})([0-9]{{{digits},}})", str(name), re.IGNORECASE)
    return (match[1], int(match[2])) if match else None


class NamingContinuity:
    def __init__(self, snapshot, *, dry_run=False):
        naming = snapshot.get("source", {}).get("continuity", {}).get("naming") or {}
        self.source_series = {row["name"]: int(row.get("current") or 0) for row in naming.get("series", [])}
        self.source_rules = naming.get("rules", [])
        self.dry_run = dry_run
        self.series_floors = {}
        self.rules_floors = {}
        self.numeric_floors = {}
        # Cache the naming definitions, not applied counters. Transactions
        # can roll back between records, including when an import is retried.
        self.configuration = lru_cache(maxsize=None)(self.configuration)

    def configuration(self, doctype):
        meta = frappe.get_meta(doctype)
        mode = meta.autoname or ""
        if mode == "autoincrement":
            return mode, [], []
        candidates = [mode]
        if mode == "naming_series:":
            field = meta.get_field("naming_series")
            candidates = (field.options or "").splitlines() + [field.default or ""] if field else []
        patterns = {parsed for value in candidates if (parsed := series_pattern(value.strip()))}
        rules = frappe.get_all(
            "Document Naming Rule",
            filters={"document_type": doctype, "disabled": 0},
            fields=["name", "prefix", "prefix_digits"],
        )
        return mode, sorted(patterns), rules

    def reserve(self, source_doctype, target_doctype, names):
        names = tuple(names)
        if not names:
            return
        mode, patterns, rules = self.configuration(target_doctype)
        if mode == "autoincrement":
            highest = max((int(name) for name in names if re.fullmatch(r"[0-9]+", str(name))), default=0)
            if highest and not self.dry_run:
                # MariaDB SETVAL advances a sequence without lowering an
                # already higher counter. It survives transaction rollback.
                if frappe.db.db_type != "mariadb":
                    raise ValueError("Preserved numeric IDs require MariaDB sequence continuity.")
                frappe.db.set_next_sequence_val(target_doctype, highest, is_val_used=True)
            if highest:
                self.numeric_floors[target_doctype] = max(highest, self.numeric_floors.get(target_doctype, 0))
            return
        floors = {}
        for pattern, digits in patterns:
            matches = [matched for name in names if (matched := match_number(pattern, digits, name))]
            if not matches:
                continue
            # Include deleted numbers and year buckets from the source.
            for key, current in self.source_series.items():
                if re.fullmatch(pattern, key, re.IGNORECASE):
                    floors[key] = max(current, floors.get(key, 0))
            for key, number in matches:
                floors[key] = max(number, floors.get(key, 0))
        for key, floor in sorted(floors.items()):
            frappe.db.sql(
                """INSERT INTO `tabSeries` (name, current) VALUES (%s, %s)
                   ON DUPLICATE KEY UPDATE current = GREATEST(COALESCE(current, 0), VALUES(current))""",
                (key, floor),
            )
            self.series_floors[key] = max(floor, self.series_floors.get(key, 0))
        for rule in rules:
            digits = int(rule.get("prefix_digits") or 0)
            if digits <= 0:
                continue
            pattern = prefix_pattern(rule.get("prefix") or "")
            observed = [matched[1] for name in names if (matched := match_number(pattern, digits, name))]
            if not observed:
                continue
            floors = [int(r.get("counter") or 0) for r in self.source_rules
                      if r.get("document_type") == source_doctype and not int(r.get("disabled") or 0)
                      and prefix_pattern(r.get("prefix") or "") == pattern
                      and int(r.get("prefix_digits") or 0) == digits]
            floor = max(observed + floors)
            frappe.db.sql(
                """UPDATE `tabDocument Naming Rule` SET counter = GREATEST(COALESCE(counter, 0), %s)
                   WHERE name = %s""",
                (floor, rule["name"]),
            )
            self.rules_floors[rule["name"]] = max(floor, self.rules_floors.get(rule["name"], 0))

    def report(self):
        return {"series_floors": self.series_floors, "naming_rule_floors": self.rules_floors,
                "numeric_floors": self.numeric_floors, "numeric_sequences_applied": not self.dry_run}


def reserve_people(snapshot, *, dry_run=False):
    naming = NamingContinuity(snapshot, dry_run=dry_run)
    for old, (new, dataset) in PEOPLE.items():
        naming.reserve(old, new, (row["name"] for row in snapshot.get(dataset, {}).values()))
    return naming.report()
