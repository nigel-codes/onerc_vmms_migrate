"""Evidence-based native creation/joining dates, independent of import checkpoints."""

import datetime
import hashlib

TARGETS = frozenset(
	{"User", "Red Profile", "VMMS Volunteer", "VMMS Member", "VMMS Membership", "VMMS Volunteer Application"}
)
JOINED = frozenset({"VMMS Volunteer", "VMMS Member"})


def timestamp(value):
	if not value:
		return None
	parsed = datetime.datetime.fromisoformat(str(value).strip())
	if parsed.tzinfo is not None:
		raise ValueError("Source timestamps must use the captured site-local timezone")
	if parsed.date() > datetime.date.today() or parsed.year < 1900:
		raise ValueError("Source timestamp outside the historical range")
	return parsed.strftime("%Y-%m-%d %H:%M:%S.%f")


def expected_dates(doctype, rows, joined_on=None):
	"""Native creation is source registration, never substituted by joining date."""
	result, issues, created = {}, [], []
	for row in rows:
		try:
			value = timestamp(row.get("creation"))
			if value:
				created.append(value)
		except ValueError, TypeError:
			issues.append("Invalid source creation timestamp")
	if created and not issues:
		result["creation"] = min(created)
	else:
		if not issues:
			issues.append("Source creation timestamp absent; destination creation unchanged")
	if doctype in JOINED:
		try:
			# Missing evidence stays unknown; the caller distinguishes migration fallbacks.
			result["joined_on"] = timestamp(joined_on)[:10] if joined_on else None
		except ValueError, TypeError:
			issues.append("Invalid source joining date; destination joining date unchanged")
	return result, issues


def comparable(field, value):
	if value in (None, ""):
		return None
	if field == "creation":
		return datetime.datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H:%M:%S.%f")
	if field == "joined_on":
		return str(value)[:10]
	return str(value)


def changes_for(before, expected, migration_days):
	result, held = {}, []
	for field, value in expected.items():
		current = comparable(field, before.get(field))
		if current == comparable(field, value):
			continue
		if field == "joined_on" and value is None and current not in migration_days:
			held.append("Unknown source joining date conflicts with a non-migration joining date")
			continue
		result[field] = value
	return result, held


def build_plan(snapshot, conf, proof):
	"""Read captured evidence and qualified ledger identities without business writes."""
	from collections import Counter, defaultdict

	import frappe

	from onerc_vmms_migrate import batches, load
	from onerc_vmms_migrate.history_model import History, canonical

	history = History(snapshot)
	collections = {
		"User": "users",
		"Employee": "employees",
		"Job Applicant": "applicants",
		"VM Member": "members",
		"VM Membership": "memberships",
	}

	def source_row(dt, name):
		return snapshot.get(collections.get(dt, ""), {}).get(str(name)) or history.get(dt, name)

	bundles = {row["key"]: row for row in batches.build(snapshot, conf)["bundles"]}
	ledgers = frappe.get_all(
		load.LEDGER,
		filters={"run_id": proof["run_id"], "target_doctype": ["in", sorted(TARGETS)]},
		fields=[
			"name",
			"person_key",
			"source_doctype",
			"source_name",
			"target_doctype",
			"target_name",
			"source_hash",
			"outcome",
			"creation",
			"migrated_on",
		],
	)
	grouped = defaultdict(list)
	for row in ledgers:
		grouped[(row.target_doctype, str(row.target_name))].append(row)
	native = {}
	for dt in TARGETS:
		fields = ["name", "creation", "modified"] + (["joined_on"] if dt in JOINED else [])
		native[dt] = {str(row.name): row for row in frappe.get_all(dt, fields=fields)}
	tasks, held, unchanged, adopted = [], [], 0, 0
	for (dt, name), links in sorted(grouped.items()):
		if not any(row.outcome == "created" for row in links):
			adopted += 1
			continue
		if dt == "User" and name.lower() in ("administrator", "guest"):
			continue
		before = native[dt].get(name)
		if not before:
			held.append({"doctype": dt, "name": name, "reason": "Native ledger target is missing"})
			continue
		rows, errors, joining, migration_days = [], [], set(), set()
		for link in links:
			for value in (link.creation, link.migrated_on):
				if value:
					migration_days.add(str(value)[:10])
			bundle = bundles.get(link.person_key)
			if str(link.person_key).startswith("account:"):
				account = source_row(link.source_doctype, link.source_name)
				if not account or hashlib.sha256(canonical(account).encode()).hexdigest() != link.source_hash:
					errors.append("Account source hash changed")
					continue
			elif not bundle or bundle["source_hash"] != link.source_hash or bundle["errors"]:
				errors.append("Person source hash or eligibility changed")
				continue
			if link.source_doctype == "Person":
				for source_dt, source_name in bundle["sources"]:
					row = source_row(source_dt, source_name)
					if row:
						rows.append(row)
			else:
				row = source_row(link.source_doctype, link.source_name)
				if not row:
					errors.append("Captured source identity is absent")
					continue
				rows.append(row)
				if dt in JOINED:
					section = "volunteer" if dt == "VMMS Volunteer" else "member"
					if not bundle.get(section):
						errors.append("Source joining evidence is absent")
						continue
					joining.add(bundle[section].get("joined_on"))
		if len(joining) > 1:
			errors.append("Merged identities have conflicting joining dates")
		expected, issues = expected_dates(dt, rows, next(iter(joining), None))
		if dt in JOINED and not joining:
			expected.pop("joined_on", None)
		if expected.get("creation") and expected["creation"] > comparable("creation", before.creation):
			errors.append("Source creation is later than the destination; review identity")
		changes, conflicts = changes_for(before, expected, migration_days)
		for reason in errors + issues + conflicts:
			held.append({"doctype": dt, "name": name, "reason": reason})
		if errors:
			continue
		if not changes:
			unchanged += 1
			continue
		keys = ["creation", "modified"] + (["joined_on"] if dt in JOINED else [])
		tasks.append(
			{
				"doctype": dt,
				"name": name,
				"before": {key: str(before[key]) if before.get(key) is not None else None for key in keys},
				"after": changes,
				"ledger": [
					{
						key: link.get(key)
						for key in ("name", "target_doctype", "target_name", "source_hash", "outcome")
					}
					for link in links
				],
			}
		)
	return {
		"tasks": tasks,
		"held": held,
		"summary": {
			"records_to_change": len(tasks),
			"unchanged": unchanged,
			"adopted_preserved": adopted,
			"held_items": len(held),
			"by_target": dict(Counter(t["doctype"] for t in tasks)),
			"fields": dict(Counter(field for t in tasks for field in t["after"])),
			"held_reasons": dict(Counter(t["reason"] for t in held)),
			"joining_dates_cleared": sum(
				"joined_on" in t["after"] and t["after"]["joined_on"] is None for t in tasks
			),
		},
	}
