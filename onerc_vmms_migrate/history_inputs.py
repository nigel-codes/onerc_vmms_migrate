"""What the history import needs from a capture, worked out once and kept small.

Reading a whole capture holds every account, volunteer, applicant and file of
the old site in memory, about a gigabyte, and then builds every person once
more to learn who the people session held back. On a hosted worker that is
enough to have the job stopped part-way, and every restart pays it again.

The history needs little of it: the held-back people, the old site's
description and schemas, its file manifest and the names of its accounts and
volunteers. The first run works that out from the whole capture and writes it
next to the capture; later runs read only that. It is keyed to everything the
held-back list depends on (the capture, the settings, the crosswalks, the
placeholder emails, the migration code and the people ledger), so a change to
any of them works it out again.
"""

import hashlib
import json
import os

import frappe

from onerc_vmms_migrate import workspace

VERSION = 1
FILE = "history-inputs.json"
# People data only the people session reads. The history reads accounts and
# volunteers by name only (`Loader.target`), never their fields.
LEFT_OUT = ("users", "employees", "applicants", "files", "password_hashes", "_people_original",
	"_migration_identity_emails", "history")
NAMES_ONLY = ("users", "employees")


def key():
	"""The settings fingerprint (capture, settings, crosswalks, placeholder
	emails, code) and the people ledger the held-back list is cut from."""
	from onerc_vmms_migrate.browser import fingerprint

	count, created, modified = frappe.db.sql(
		"select count(*), max(creation), max(modified) from `tabLegacy Migration Record`")[0]
	ledger = f"{count}\0{created}\0{modified}"
	return hashlib.sha256((fingerprint() + "\0" + ledger).encode()).hexdigest()


def path(capture):
	return workspace.path("captures", capture, FILE)


def exists(capture):
	"""Whether this capture's small inputs were written (perhaps for other inputs)."""
	return bool(capture) and os.path.isfile(path(capture))


def usable(snapshot):
	"""Only a bulk capture keeps its history outside the snapshot; an older one
	carries it inline and is read whole every time."""
	return bool(snapshot.get("history_storage")) and "User" in snapshot.get("schemas", {})


def save(conf, snapshot):
	"""The small inputs, from a snapshot whose held-back list is worked out."""
	from onerc_vmms_migrate.bulk_source import atomic_json

	kept = {name: value for name, value in snapshot.items() if name not in LEFT_OUT}
	for name in NAMES_ONLY:
		kept[name] = sorted(snapshot.get(name) or {})
	kept["excluded_native_identities"] = sorted([dt, str(name)] for dt, name in snapshot["excluded_native_identities"])
	kept["_people_enriched"] = True
	# The captured manifest is the ready part of the whole one, item for item;
	# keep the whole one and the list of ready addresses.
	whole, ready = snapshot.get("all_file_manifest") or {}, snapshot.get("captured_file_manifest") or {}
	if all(whole.get(url) == item for url, item in ready.items()):
		kept.pop("captured_file_manifest", None)
		kept["captured_file_urls"] = sorted(ready)
	payload = {"version": VERSION, "key": key(), "capture": conf.get("browser_capture"), "snapshot": kept}
	atomic_json(path(conf.get("browser_capture")), payload)


def load(conf):
	"""The small snapshot, or None when it is missing or out of date."""
	capture = conf.get("browser_capture")
	target = path(capture) if capture else None
	if not target or not os.path.isfile(target):
		return None
	with open(target) as handle:
		payload = json.load(handle)
	if payload.get("version") != VERSION or payload.get("capture") != capture or payload.get("key") != key():
		return None
	snapshot = payload["snapshot"]
	for name in NAMES_ONLY:
		snapshot[name] = set(snapshot.get(name) or ())
	snapshot["excluded_native_identities"] = {(dt, name) for dt, name in snapshot["excluded_native_identities"]}
	urls = snapshot.pop("captured_file_urls", None)
	if urls is not None:
		whole = snapshot.get("all_file_manifest") or {}
		snapshot["captured_file_manifest"] = {url: whole[url] for url in urls}
	return snapshot


def forget(capture):
	if capture and os.path.isfile(path(capture)):
		os.remove(path(capture))
