"""Crosswalks: the society's own answer to "this old value means that new record".

One CSV per kind in `private/onerc_vmms_migrate/crosswalks/`, with columns

    source_value, occurrences, target, target_label, note

* `target` is the **name** of a record on the target site (a Geo Node's name, a
  membership type's key, ...), `-` for "deliberately not carried", or one of a
  kind's special words (see `KINDS`).
* A **blank target is unreviewed**, and an unreviewed value that a person uses
  stops that person from loading. Nothing is guessed at load time.
* `crosswalks()` writes these files. It keeps every target already filled in,
  adds values it has not seen before, and pre-fills a target only on an exact,
  unambiguous name match, noting that it did so, so a reviewer can see which
  rows a human decided and which ones a match filled.
* `target_label` is rewritten on every generation, to show the reviewer what
  the chosen target is called. It is never read back.
* **The `*` row is a default a human writes.** Every file starts with one, and
  its target is blank. Fill it in, and it answers for every value that has no
  target of its own: `-` ("carry nothing else"), `Other` for professions, and
  so on. Left blank, every value still needs its own answer. Rows with their
  own target always win over `*`.
"""

import csv
import os
from collections import Counter

import frappe

from onerc_vmms_migrate import normalise, workspace

DROP = "-"
DEFAULT = "*"
UNMAPPED = object()

# kind -> (target doctype, the field that labels it, special targets, what it is)
KINDS: dict[str, dict] = {
	"branch": {
		"doctype": "Geo Node",
		"label": "geo_node_name",
		"about": "Old Company (the branch) -> the Geo Node a volunteer or membership is anchored to",
	},
	"residence": {
		"doctype": "Geo Node",
		"label": "geo_node_name",
		"about": "Old county/sub_county/ward/administrative_location/sub_location, as 'field:value'"
		" -> the Geo Node for where the person lives. The deepest mapped one is used",
	},
	"membership_type": {
		"doctype": "VMMS Membership Type",
		"label": "membership_type_name",
		"about": "Old VM Membership Type -> VMMS Membership Type",
	},
	"volunteer_status": {
		"choices": ("Active", "Suspended", "Exited"),
		"about": "Old Employee.status -> VMMS Volunteer status",
	},
	"gender": {"doctype": "Gender", "label": "gender", "about": "Gender"},
	"identification_type": {
		"doctype": "Identification Type",
		"label": "identification_type_name",
		"about": "Old Identification Document Type -> core Identification Type."
		" '__passport_number__' is the old passport_number field",
	},
	"supporting_document": {
		"doctype": "Identification Type",
		"label": "identification_type_name",
		"special": ("file",),
		"about": "Old Supporting Document Type -> an Identification Type (the scan goes on that ID"
		" row), 'file' (kept as a private file on the profile) or '-'",
	},
	"disability": {
		"doctype": "Disability",
		"label": "disability_name",
		"about": "Old Disability -> core Disability",
	},
	"language": {"doctype": "Language", "label": "language_name", "about": "Language"},
	"skill": {"doctype": "VMMS Skill", "label": "skill_name", "about": "Old free-text skills -> VMMS Skill"},
	"profession": {
		"doctype": "Profession",
		"label": None,
		"about": "Old profession -> Profession. Choosing Other keeps the old text as the description",
	},
	"education_level": {
		"doctype": "VMMS Education Level",
		"label": "level_name",
		"about": "Old education level / degree type -> VMMS Education Level",
	},
	"driving_licence_class": {
		"doctype": "VMMS Driving Licence Class",
		"label": "class_name",
		"about": "Old Driving Licence Class -> VMMS Driving Licence Class",
	},
	"licence_type": {
		"doctype": "Personnel License Type",
		"label": None,
		"about": "Old Personnel License Type -> Personnel License Type",
	},
	"question": {
		"doctype": "VMMS Application Question",
		"label": "question_label",
		"about": "Old field with no first-class home -> the organization-specific question its"
		" answer is stored under, or '-' to leave it in the archive",
	},
}

COLUMNS = ["source_value", "occurrences", "target", "target_label", "note"]

VOLUNTEER_STATUS_DEFAULTS = {
	"active": "Active",
	"inactive": "Suspended",
	"suspended": "Suspended",
	"left": "Exited",
}


class Crosswalks:
	def __init__(self, entries: dict[str, dict[str, dict]] | None = None):
		# kind -> normalised source key -> row
		self.entries: dict[str, dict[str, dict]] = entries or {}
		self.seen: dict[str, Counter] = {kind: Counter() for kind in KINDS}
		self.display: dict[tuple[str, str], str] = {}
		# Set by transform.build from the config. See workspace.DEFAULTS.
		self.placeholders: set[str] = set()
		self.placeholder_hits: Counter = Counter()

	def is_placeholder(self, kind: str, value) -> bool:
		if normalise.key(value) in self.placeholders:
			self.placeholder_hits[kind] += 1
			return True
		return False

	# --- lookups -------------------------------------------------------------

	def lookup(self, kind: str, value):
		"""The target for an old value: a name, DROP, or UNMAPPED."""
		shown = normalise.text(value)
		if not shown or self.is_placeholder(kind, shown):
			return None

		k = normalise.key(shown)
		self.seen[kind][k] += 1
		self.display.setdefault((kind, k), shown)

		target = self.target(kind, k)

		if not target:
			return UNMAPPED

		return DROP if target == DROP else target

	def target(self, kind: str, k: str) -> str:
		"""A value's own target, else the kind's `*` default, else ''."""
		rows = self.entries.get(kind, {})
		own = normalise.text((rows.get(k) or {}).get("target"))
		return own or normalise.text((rows.get(DEFAULT) or {}).get("target"))

	# --- files ---------------------------------------------------------------

	@classmethod
	def read(cls, directory: str | None = None) -> "Crosswalks":
		directory = directory or workspace.crosswalk_dir()
		entries: dict[str, dict[str, dict]] = {}

		for kind in KINDS:
			path = os.path.join(directory, f"{kind}.csv")
			if not os.path.isfile(path):
				continue
			with open(path, newline="") as handle:
				# The first line is a "# what this file is" comment for the reviewer.
				lines = (line for line in handle if not line.startswith("# "))
				for row in csv.DictReader(lines):
					source_value = normalise.text(row.get("source_value"))
					if source_value:
						entries.setdefault(kind, {})[normalise.key(source_value)] = row

		return cls(entries)

	def write(self, directory: str | None = None, suggest: bool = True) -> list[str]:
		"""Merge what was seen into the CSVs. Returns the files written."""
		directory = directory or workspace.crosswalk_dir()
		written = []

		for kind, spec in KINDS.items():
			existing = self.entries.get(kind, {})
			keys = (set(existing) | set(self.seen[kind])) - {DEFAULT}

			if not keys:
				continue

			default = dict(existing.get(DEFAULT) or {})
			default.update(
				{
					"source_value": DEFAULT,
					"occurrences": "",
					"target_label": label_of(kind, normalise.text(default.get("target"))),
					"note": default.get("note")
					or "default for every value below with no target of its own; blank = decide each one",
				}
			)
			rows = [{c: default.get(c, "") for c in COLUMNS}]
			for k in sorted(keys, key=lambda x: (-self.seen[kind][x], x)):
				row = dict(existing.get(k) or {})
				row.setdefault("source_value", self.display.get((kind, k), k))
				row["occurrences"] = self.seen[kind][k]
				target = normalise.text(row.get("target"))

				if not target and suggest:
					target, note = suggest_target(kind, row["source_value"])
					if target:
						row["target"] = target
						row["note"] = note

				row["target_label"] = label_of(kind, normalise.text(row.get("target")))
				rows.append({c: row.get(c, "") for c in COLUMNS})

			path = os.path.join(directory, f"{kind}.csv")
			with open(path, "w", newline="") as handle:
				handle.write(f"# {spec['about']}\n")
				writer = csv.DictWriter(handle, fieldnames=COLUMNS)
				writer.writeheader()
				writer.writerows(rows)
			written.append(path)

		return written

	# --- review --------------------------------------------------------------

	def problems(self) -> list[dict]:
		"""Rows a person needs whose target is blank or does not exist."""
		found = []
		for kind in KINDS:
			for k, count in self.seen[kind].items():
				target = self.target(kind, k)
				value = self.display.get((kind, k), k)

				if not target:
					found.append(
						{"kind": kind, "source_value": value, "occurrences": count, "problem": "unmapped"}
					)
				elif target != DROP and not target_exists(kind, target):
					found.append(
						{
							"kind": kind,
							"source_value": value,
							"occurrences": count,
							"problem": f"target {target!r} does not exist on this site",
						}
					)
		return found


def target_exists(kind: str, target: str) -> bool:
	spec = KINDS[kind]
	if target in spec.get("special", ()):
		return True
	if "choices" in spec:
		return target in spec["choices"]
	return bool(frappe.db.exists(spec["doctype"], target))


def label_of(kind: str, target: str) -> str:
	spec = KINDS[kind]
	if not target or target == DROP or target in spec.get("special", ()) or "choices" in spec:
		return ""
	if not spec.get("label"):
		return target if frappe.db.exists(spec["doctype"], target) else "(does not exist)"
	value = frappe.db.get_value(spec["doctype"], target, spec["label"])
	return value or "(does not exist)"


def suggest_target(kind: str, source_value: str) -> tuple[str, str]:
	"""An exact, unambiguous name match on the target site, or ('', '')."""
	spec = KINDS[kind]

	if kind == "volunteer_status":
		default = VOLUNTEER_STATUS_DEFAULTS.get(normalise.key(source_value))
		return (default, "suggested default: confirm") if default else ("", "")

	if "doctype" not in spec:
		return "", ""

	value = source_value.split(":", 1)[1] if kind == "residence" and ":" in source_value else source_value
	doctype = spec["doctype"]

	candidates = set()
	if frappe.db.exists(doctype, value):
		candidates.add(value)
	if spec.get("label"):
		for name in frappe.get_all(doctype, filters={spec["label"]: value}, pluck="name"):
			candidates.add(name)

	if len(candidates) == 1:
		return candidates.pop(), "auto: exact name match, confirm"
	if len(candidates) > 1:
		return "", f"ambiguous: {len(candidates)} records called {value!r}"
	return "", ""
