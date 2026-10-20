"""Reviewed private attachment tasks; source ownership is never guessed."""

import hashlib
import html
import json
import re
from collections import Counter, defaultdict

import frappe

from onerc_vmms_migrate import batches, historical, load
from onerc_vmms_migrate.history_model import History
from onerc_vmms_migrate.html_files import LINK

PERSONAL_SOURCE = frozenset(
	{
		"User",
		"Employee",
		"Job Applicant",
		"VM Member",
		"VM Membership",
		"Event Booking",
		"Event Ticket",
		"Personnel Deployment Request",
	}
)


def digest(value):
	return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class Plan:
	def __init__(self, manifest):
		self.manifest = manifest
		self.claims = defaultdict(set)
		self.unknown = set()
		self.tasks = {}
		self.issues = []

	def blob(self, url):
		return self.manifest.get(url, {}).get("sha256") or url

	def claim(self, url, person):
		if person is None:
			self.unknown.add(self.blob(url))
		else:
			self.claims[self.blob(url)].add(person)

	def add(self, url, doctype, name, binding=None, file_record=None):
		if not url:
			return
		key = (url, doctype, str(name))
		task = self.tasks.setdefault(
			key,
			{
				"url": url,
				"doctype": doctype,
				"name": str(name),
				"private": True,
				"bindings": [],
				"file_records": [],
			},
		)
		if binding and binding not in task["bindings"]:
			task["bindings"].append(binding)
		if file_record and file_record not in task["file_records"]:
			task["file_records"].append(file_record)

	def finish(self):
		tasks, held = [], list(self.issues)
		slots = defaultdict(set)
		for task in self.tasks.values():
			for link in task["bindings"]:
				if link["kind"] != "Text Editor":
					slots[(link["doctype"], link["name"], link["field"])].add(task["url"])
		for key in sorted(self.tasks):
			task = self.tasks[key]
			item = self.manifest.get(task["url"], {})
			reason = None
			if item.get("status") != "ready" or not re.fullmatch("[0-9a-f]{64}", item.get("sha256") or ""):
				reason = "Verified captured bytes unavailable"
			elif self.blob(task["url"]) in self.unknown:
				reason = "Personal source owner unresolved"
			elif len(self.claims[self.blob(task["url"])]) > 1:
				reason = "Multiple personal identities reference these bytes"
			elif any(
				link["kind"] != "Text Editor"
				and len(slots[(link["doctype"], link["name"], link["field"])]) > 1
				for link in task["bindings"]
			):
				reason = "Multiple source files compete for one native attachment field"
			if reason:
				held.append({**task, "reason": reason})
			else:
				tasks.append({**task, "sha256": item["sha256"]})
		return {
			"tasks": tasks,
			"held": held,
			"summary": {
				"copy_tasks": len(tasks),
				"held_items": len(held),
				"unique_ready_urls": len({task["url"] for task in tasks}),
				"by_target": dict(Counter(task["doctype"] for task in tasks)),
				"all_new_files_private": True,
				"access_checks_pending": True,
				"capture_missing_entries": sum(
					item.get("status") != "ready" for item in self.manifest.values()
				),
			},
		}


def binding(doctype, name, field, original, parent=None, kind="Attach"):
	return {
		"doctype": doctype,
		"name": str(name),
		"field": field,
		"original": original,
		"parent": parent,
		"kind": kind,
	}


def profile_binding(bundle, item, profile):
	"""Bind to the exact native child identity, not an unchecked row number."""
	if item["purpose"] == "photo":
		return binding("Red Profile", profile.name, "profile_photo", item["url"], kind="Attach Image")
	if item["purpose"] not in ("identification", "table"):
		return None
	table = "identifications" if item["purpose"] == "identification" else item["table"]
	index = item["index"]
	expected = bundle["profile"].get(table) or []
	native = profile.get(table) or []
	if (
		isinstance(index, bool)
		or not isinstance(index, int)
		or index < 0
		or index >= len(expected)
		or index >= len(native)
	):
		raise ValueError("Profile attachment child row is absent")
	row = native[index]
	for field, value in expected[index].items():
		if field.startswith("_") or field == "attachment" or value in (None, ""):
			continue
		if str(row.get(field) if row.get(field) is not None else "") != str(value):
			raise ValueError("Profile attachment child row differs from reviewed source fields")
	return binding(
		row.doctype,
		row.name,
		"attachment",
		item["url"],
		{"doctype": "Red Profile", "name": str(profile.name), "field": table},
	)


def build(snapshot, conf):
	"""Build a read-only plan from qualified native identities and captured bytes."""
	history = History(snapshot)
	plan = Plan(snapshot.get("all_file_manifest", snapshot.get("captured_file_manifest", {})))
	capture = snapshot["source"].get("capture_id") or snapshot["source"].get("snapshot_id")
	source_id = str(snapshot["source"].get("source_id") or snapshot["source"]["db_name"])
	files = history.rows("File")
	parent_types = sorted(
		{"File"} | {row["attached_to_doctype"] for row in files if row.get("attached_to_doctype")}
	)
	archives = frappe.get_all(
		historical.ARCHIVE,
		filters={"source_id": source_id, "snapshot_id": capture},
		or_filters=[["conversion_status", "=", "Converted"], ["source_doctype", "in", parent_types]],
		fields=[
			"name",
			"source_doctype",
			"source_name",
			"conversion_status",
			"target_doctype",
			"target_name",
		],
	)
	archived = {(r.source_doctype, str(r.source_name)): r for r in archives}
	targets = {
		key: (r.target_doctype, str(r.target_name))
		for key, r in archived.items()
		if r.conversion_status == "Converted" and r.target_doctype and r.target_name is not None
	}
	people = batches.build(snapshot, conf)["bundles"]
	roots = {}
	owner_documents = {}
	ledgers = frappe.get_all(
		load.LEDGER,
		filters={"source_doctype": "Person", "target_doctype": "Red Profile"},
		fields=["person_key", "source_hash", "target_name"],
	)
	profiles = {}
	for row in ledgers:
		if row.person_key in profiles and profiles[row.person_key] != (row.target_name, row.source_hash):
			raise ValueError("Person ledger has conflicting profile targets")
		profiles[row.person_key] = (row.target_name, row.source_hash)
	profile_roots = {"person:" + key: value[0] for key, value in profiles.items()}
	for bundle in people:
		root = None if bundle["errors"] or bundle.get("volunteer_excluded") else "person:" + bundle["key"]
		for dt, name in bundle["sources"]:
			roots[(dt, str(name))] = root
		if not bundle["files"]:
			continue
		if root is None or bundle["key"] not in profiles:
			for item in bundle["files"]:
				plan.claim(item["url"], root)
			continue
		profile_name, source_hash = profiles[bundle["key"]]
		if source_hash != bundle["source_hash"]:
			raise ValueError("A profile source hash changed since the completed test")
		profile = frappe.get_doc("Red Profile", profile_name)
		owner_documents[("Red Profile", str(profile_name))] = profile
		if (
			str(profile.get("email") or "").strip().lower()
			!= str(bundle["profile"].get("email") or "").strip().lower()
		):
			raise ValueError("Profile email differs from its qualified source identity")
		for item in bundle["files"]:
			plan.claim(item["url"], root)
			try:
				link = profile_binding(bundle, item, profile)
			except ValueError as exc:
				plan.issues.append(
					{
						"url": item["url"],
						"doctype": "Red Profile",
						"name": str(profile_name),
						"reason": str(exc),
					}
				)
				continue
			plan.add(item["url"], "Red Profile", profile_name, link)
	for user in snapshot["users"].values():
		target = targets.get(("User", str(user["name"])))
		if target and target[0] == "User":
			roots.setdefault(("User", str(user["name"])), "person:" + target[1])
	for membership in snapshot.get("memberships", {}).values():
		roots[("VM Membership", str(membership["name"]))] = roots.get(
			("VM Member", str(membership.get("member")))
		)

	def owner(dt, name):
		identity = (dt, str(name))
		if identity in roots:
			return True, roots[identity]
		row = history.get(dt, name) if dt and name else None
		if dt == "Personnel Deployment Request" and row:
			return True, roots.get(("Employee", str(row.get("employee"))))
		if dt == "Event Booking" and row:
			return True, roots.get(("User", str(row.get("user"))))
		if dt == "Event Ticket" and row:
			booking = history.get("Event Booking", row.get("booking"))
			return True, roots.get(("User", str(booking.get("user")))) if booking else None
		if dt in PERSONAL_SOURCE:
			return True, None
		if row and row.get("parenttype") in PERSONAL_SOURCE:
			return True, roots.get((row["parenttype"], str(row.get("parent"))))
		return False, None

	for file in files:
		url = file.get("file_url")
		if not url:
			continue
		dt, name = file.get("attached_to_doctype"), file.get("attached_to_name")
		personal, root = owner(dt, name)
		if personal:
			plan.claim(url, root)
		target = targets.get((dt, str(name)))
		if dt == "User" and root in profile_roots:
			target = ("Red Profile", profile_roots[root])
		archive = archived.get((dt, str(name))) or archived.get(("File", str(file["name"])))
		if target:
			plan.add(url, *target, file_record=file["name"])
		elif archive:
			plan.add(url, historical.ARCHIVE, archive.name, file_record=file["name"])
	metas = {}
	native_source_types = {doctype for doctype, _name in targets}
	for dt in history.schemas:
		if dt not in native_source_types:
			continue
		fields = [
			f
			for f in history.schemas[dt].get("fields", [])
			if f.get("fieldtype") in ("Attach", "Attach Image", "Text Editor")
		]
		if not fields:
			continue
		for row in history.rows(dt):
			target = targets.get((dt, str(row["name"])))
			if not target:
				continue
			target_dt, target_name = target
			if target_dt not in metas:
				metas[target_dt] = frappe.get_meta(target_dt)
			meta = metas[target_dt]
			personal, root = owner(dt, row["name"])
			for field in fields:
				value = row.get(field["fieldname"])
				if not value:
					continue
				urls = (
					[html.unescape(m["url"]) for m in LINK.finditer(value)]
					if field["fieldtype"] == "Text Editor"
					else [value]
				)
				for url in urls:
					if personal:
						plan.claim(url, root)
					native = meta.get_field(field["fieldname"])
					link = (
						binding(target_dt, target_name, field["fieldname"], url, kind=field["fieldtype"])
						if native and native.fieldtype == field["fieldtype"]
						else None
					)
					plan.add(url, target_dt, target_name, link)
	for user in snapshot["users"].values():
		url = user.get("user_image")
		target = targets.get(("User", str(user["name"])))
		if url and target:
			root = roots.get(("User", str(user["name"])))
			plan.claim(url, root)
			parent = ("Red Profile", profile_roots[root]) if root in profile_roots else target
			plan.add(url, *parent, binding(*target, "user_image", url, kind="Attach Image"))
	for row in history.rows("Deployment Request Tool"):
		url = row.get("tor_attachment")
		target = targets.get(("Deployment Request Tool", str(row["name"])))
		if not url or not target or target[0] != "VMMS Deployment":
			continue
		terms = frappe.db.get_value("VMMS Deployment", target[1], "terms_of_reference")
		if terms and frappe.db.get_value("VMMS Terms of Reference", terms, "legacy_unstructured_terms"):
			plan.add(
				url,
				"VMMS Terms of Reference",
				terms,
				binding("VMMS Terms of Reference", terms, "legacy_terms_attachment", url),
			)
	result = plan.finish()
	accepted = []
	for task in result["tasks"]:
		task["owner_chain"] = owner_chain(task["doctype"], task["name"], documents=owner_documents)
		if not claims_match(plan.claims[plan.blob(task["url"])], task["owner_chain"]):
			result["held"].append(
				{**task, "reason": "Native owner differs from the source personal identity"}
			)
		else:
			accepted.append(task)
	result["tasks"] = accepted
	result["summary"].update(
		copy_tasks=len(accepted),
		held_items=len(result["held"]),
		unique_ready_urls=len({task["url"] for task in accepted}),
		by_target=dict(Counter(task["doctype"] for task in accepted)),
	)
	return result


OWNER_LINKS = {
	"Red Profile": {"user": "User"},
	"VMMS Volunteer": {"red_profile": "Red Profile"},
	"VMMS Member": {"red_profile": "Red Profile"},
	"VMMS Membership": {"member": "VMMS Member"},
	"VMMS Volunteer Application": {"red_profile": "Red Profile", "user": "User"},
	"VMMS Deployment Assignment": {"volunteer": "VMMS Volunteer"},
	"Event Booking": {"user": "User"},
	"Event Ticket": {"booking": "Event Booking"},
}


def claims_match(claims, chain):
	if not claims:
		return True
	for node in chain:
		email = (
			node["name"]
			if node["doctype"] == "User"
			else node["fields"].get("email")
			if node["doctype"] == "Red Profile"
			else None
		)
		if email and "person:" + str(email).strip().lower() not in claims:
			return False
	return True


def owner_chain(doctype, name, seen=None, documents=None, for_update=False):
	seen = set() if seen is None else seen
	documents = {} if documents is None else documents
	identity = (doctype, str(name))
	if identity in seen:
		return []
	seen.add(identity)
	if identity not in documents:
		meta = frappe.get_meta(doctype)
		wanted = ["owner"] + [field for field in OWNER_LINKS.get(doctype, {}) if meta.has_field(field)]
		if doctype == "Red Profile":
			wanted.append("email")
		documents[identity] = frappe.db.get_value(doctype, name, wanted, as_dict=True, for_update=for_update)
	doc = documents[identity]
	if not doc:
		raise ValueError("Native attachment owner no longer exists")
	fields = {"owner": doc.get("owner")}
	if doctype == "Red Profile":
		fields["email"] = doc.get("email")
	links = OWNER_LINKS.get(doctype, {})
	fields.update({field: doc.get(field) for field in links})
	chain = [{"doctype": doctype, "name": str(name), "fields": fields}]
	for field, target in links.items():
		if doc.get(field):
			chain.extend(owner_chain(target, doc.get(field), seen, documents, for_update))
	return chain
