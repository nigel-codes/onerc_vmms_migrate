"""Resolve destination geography before creating source reference records."""

import re
import unicodedata

import frappe

from onerc_vmms_migrate import normalise

REGION_PREFIX = re.compile(r"^KRCS\s+", re.IGNORECASE)


def label_key(value, level):
	value = unicodedata.normalize("NFKC", normalise.text(value)).translate(str.maketrans("’‘ʼ`", "''''"))
	if level == "region":
		value = REGION_PREFIX.sub("", value)
	return normalise.key(value)


class Geography:
	def __init__(self, crosswalks):
		self.crosswalks = crosswalks
		self.nodes = {
			row["name"]: dict(row)
			for row in frappe.get_all(
				"Geo Node", fields=["name", "geo_node_name", "geo_level", "parent_geo_node", "geo_code"]
			)
		}
		self.created = set()
		self.reused = set()

	def mapped(self, kind, source):
		# A wildcard/drop is a people-import policy, not a geographic identity.
		row = self.crosswalks.entries.get(kind, {}).get(normalise.key(source), {})
		target = normalise.text(row.get("target"))
		if not target or target == "-":
			return None
		if target not in self.nodes:
			raise ValueError(f"Configured {kind} mapping target is missing: {target}")
		self.reused.add(target)
		return target

	def root(self, name):
		seen = set()
		while name:
			if name in seen or name not in self.nodes:
				raise ValueError("Geography has a missing parent or cycle; resolve it before preparation.")
			seen.add(name)
			row = self.nodes[name]
			if not row.get("parent_geo_node"):
				return name if row["geo_level"] == "national" else None
			name = row["parent_geo_node"]
		return None

	def existing(self, kind, source, label, level, parent, code):
		target = self.mapped(kind, source)
		if target:
			return target
		key = label_key(label, level)
		root = self.root(parent) if level == "county" else None
		matches = [
			row["name"]
			for row in self.nodes.values()
			if row["geo_level"] == level
			and label_key(row["geo_node_name"], level) == key
			and (
				self.root(row["name"]) == root
				if level == "county" and root
				else (row.get("parent_geo_node") or None) == (parent or None)
			)
		]
		if len(matches) > 1:
			raise ValueError("Ambiguous destination geography; map explicitly: " + label)
		if matches:
			self.reused.add(matches[0])
			return matches[0]
		coded = [row for row in self.nodes.values() if row.get("geo_code") == code]
		if len(coded) > 1:
			raise ValueError("Ambiguous source geography code; map explicitly: " + label)
		if coded:
			row = coded[0]
			if row["geo_level"] != level or (row.get("parent_geo_node") or None) != (parent or None):
				raise ValueError(
					"Source geography code has a different level/parent; map explicitly: " + label
				)
			self.reused.add(row["name"])
			return row["name"]
		return None

	def register(self, name, label, level, parent, code):
		self.nodes[name] = dict(
			name=name, geo_node_name=label, geo_level=level, parent_geo_node=parent, geo_code=code
		)
		self.created.add(name)
