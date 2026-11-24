"""Where a live history import that stood aside for a mark (marks.py) carries on.

Kept after every commit: the phase reached, the place in the archive, and
everything the report adds up along the way (the loader's failures, warnings,
exclusions and helpdesk counts, the phase errors, the archive totals). The
next job reads it and continues with the same pages an uninterrupted run would
take, so its report is that run's report.

It belongs to one run of one capture with one set of inputs (the key of
history_inputs): anything else starts afresh, with the ledger skipping what is
already in.
"""

import json
import os
import time
from collections import Counter

from frappe.utils import now_datetime

from onerc_vmms_migrate import workspace

VERSION = 1
FILE = "history-progress.json"
PHASES = ("archive", "convert", "files", "report")


def path():
	return workspace.path(FILE)


def begin(kind, capture, key, run_id):
	"""The saved place of this run, or a new one (not saved until its first commit)."""
	saved = load()
	if saved and (saved.get("kind"), saved.get("capture"), saved.get("key")) == (kind, capture, key):
		saved["slices"] = int(saved.get("slices") or 1) + 1
		return saved
	return {
		"version": VERSION,
		"kind": kind,
		"capture": capture,
		"key": key,
		"run_id": run_id,
		"started_at": str(now_datetime()),
		"started": time.time(),
		"slices": 1,
		"phase": "archive",
		"archive": {"index": 0, "start": 0, "totals": {}, "problems": []},
		"archive_result": None,
		"loader": None,
		"files": None,
	}


def load():
	target = path()
	if not os.path.isfile(target):
		return None
	try:
		with open(target) as handle:
			value = json.load(handle)
	except ValueError:
		return None
	return value if value.get("version") == VERSION else None


def save(value):
	"""Keys keep their order: a report's columns follow its first row's."""
	target = path()
	temporary = target + ".tmp"
	with open(temporary, "w") as handle:
		os.chmod(temporary, 0o600)
		json.dump(value, handle, default=str)
	os.replace(temporary, target)


def forget():
	if os.path.exists(path()):
		os.remove(path())


def marker(value):
	"""What a stretch moved, for the job that checks the run is getting anywhere."""
	place = value.get("archive") or {}
	loader = value.get("loader") or {}
	return [value.get("phase"), place.get("index"), place.get("start"), loader.get("pages"),
		len(loader.get("failures") or ())]


# The loader's part ------------------------------------------------------------


def loader_state(loader, broken, pages):
	return {
		"pages": pages,
		"failures": loader.failures,
		"warnings": loader.warnings,
		"excluded": loader.excluded,
		"validated": sorted([dt, name] for dt, name in (getattr(loader, "validated", None) or ())),
		"broken": broken,
		"helpdesk_drops": sorted([dt, what, count] for (dt, what), count in getattr(loader, "helpdesk_drops", {}).items()),
		"helpdesk_collisions": getattr(loader, "helpdesk_collisions", {}),
		"helpdesk_warned": sorted(list(item) for item in getattr(loader, "_helpdesk_warned", ())),
	}


def restore(loader, saved):
	"""Puts the lists and counts of the stretches before back on a new loader.
	Returns the phase errors and the number of convert pages done."""
	if not saved:
		return {}, 0
	loader.failures = list(saved["failures"])
	loader.warnings = list(saved["warnings"])
	loader.excluded = list(saved["excluded"])
	loader.validated = {(dt, name) for dt, name in saved["validated"]}
	if saved["helpdesk_drops"]:
		loader.helpdesk_drops = Counter({(dt, what): count for dt, what, count in saved["helpdesk_drops"]})
	if saved["helpdesk_collisions"]:
		loader.helpdesk_collisions = dict(saved["helpdesk_collisions"])
	if saved["helpdesk_warned"]:
		loader._helpdesk_warned = {tuple(item) for item in saved["helpdesk_warned"]}
	return dict(saved["broken"]), int(saved.get("pages") or 0)
