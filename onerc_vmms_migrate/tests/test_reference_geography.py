"""Reference preparation must not recreate geography consolidated on the target."""

from copy import deepcopy
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate import normalise, reference
from onerc_vmms_migrate.crosswalk import Crosswalks
from onerc_vmms_migrate.history_model import History


def node(name, label, level, parent=None, code=None):
	return dict(name=name, geo_node_name=label, geo_level=level, parent_geo_node=parent, geo_code=code)


def crosswalks(**groups):
	return Crosswalks(
		{
			kind: {
				normalise.key(source): dict(source_value=source, target=target)
				for source, target in rows.items()
			}
			for kind, rows in groups.items()
		}
	)


class TestPreparationGeography(TestCase):
	def run_geography(self, nodes, companies=(), counties=(), subcounties=(), cw=None):
		cw = cw or Crosswalks()
		history = History(
			{
				"history": {
					f"{dt}:{i}": {"doctype": dt, "record": row}
					for dt, rows in [
						("Company", companies),
						("County", counties),
						("Sub County", subcounties),
					]
					for i, row in enumerate(rows)
				}
			}
		)
		created = []

		def insert(data):
			data = dict(data)
			data["name"] = "created-" + str(len(created) + 1)
			created.append(data)
			nodes.append(data)
			return MagicMock(insert=MagicMock(return_value=frappe._dict(data)))

		def get_value(dt, name, field=None):
			candidates = (
				[r for r in nodes if all(r.get(k) == v for k, v in name.items())]
				if isinstance(name, dict)
				else [r for r in nodes if r["name"] == name]
			)
			return candidates[0].get(field or "name") if candidates else None

		def get_all(dt, filters=None, pluck=None, **kwargs):
			rows = nodes
			if filters:
				rows = [
					r
					for r in rows
					if all(
						(not r.get(k)) if isinstance(v, tuple) else r.get(k) == v for k, v in filters.items()
					)
				]
			return [r[pluck] for r in rows] if pluck else deepcopy(rows)

		def map_value(kind, source, target, **kwargs):
			entries = cw.entries.setdefault(kind, {})
			k = normalise.key(source)
			if not entries.get(k, {}).get("target"):
				entries[k] = dict(source_value=source, target=target)

		db = MagicMock()
		db.exists.return_value = True
		db.get_value.side_effect = get_value
		report = {"errors": []}
		with (
			patch.object(frappe, "db", db),
			patch.object(frappe, "get_all", side_effect=get_all),
			patch.object(frappe, "get_doc", side_effect=insert),
		):
			reference.geography(history, map_value, report, crosswalks=cw)
		return report, created, cw

	def seeds(self):
		return [
			node("root", "Kenya Red Cross Society", "national"),
			node("central", "Central", "region", "root"),
			node("county", "Murang'a", "county", "central"),
			node("north", "Murang'a North", "sub_county", "county"),
		]

	def companies(self):
		return [
			dict(name="Kenya Red Cross Society", organisation_type="Company"),
			dict(name="KRCS Central", organisation_type="Region", parent_company="Kenya Red Cross Society"),
			dict(name="Murang’a", organisation_type="Branch", parent_company="KRCS Central"),
		]

	def test_approved_company_county_and_subcounty_targets_precede_creation(self):
		cw = crosswalks(
			branch={"Murang’a": "county"},
			residence={"county:Murang'a": "county", "sub_county:MURANGA NORTH": "north"},
		)
		report, created, cw = self.run_geography(
			self.seeds(),
			self.companies(),
			[dict(name="Murang'a", county_name="Murang'a")],
			[dict(name="MURANGA NORTH", sub_county_name="MURANGA NORTH", county="Murang'a")],
			cw,
		)
		self.assertEqual(report["errors"], [])
		self.assertEqual(created, [])
		self.assertEqual(cw.target("branch", normalise.key("Murang’a")), "county")
		self.assertEqual(report["geography"]["created_nodes"], 0)

	def test_seeded_county_below_region_is_reused_without_a_crosswalk(self):
		report, created, cw = self.run_geography(
			self.seeds(), self.companies(), [dict(name="Murang'a", county_name="Murang'a")]
		)
		self.assertEqual(report["errors"], [])
		self.assertEqual(created, [])
		self.assertEqual(cw.target("residence", normalise.key("county:Murang'a")), "county")

	def test_missing_approved_target_is_reported_and_not_replaced(self):
		cw = crosswalks(branch={"Murang’a": "removed-county"})
		report, created, _ = self.run_geography(self.seeds(), self.companies(), cw=cw)
		self.assertEqual(created, [])
		self.assertTrue(any("removed-county" in r["error"] for r in report["errors"]))

	def test_explicit_nairobi_branch_assignment_is_not_turned_into_an_unused_test_county(self):
		cw = crosswalks(branch={"krcs branch test": "county"})
		companies = self.companies()[:2] + [
			dict(name="krcs branch test", organisation_type="Branch", parent_company="KRCS Central")
		]
		report, created, result = self.run_geography(self.seeds(), companies, cw=cw)
		self.assertEqual(report["errors"], [])
		self.assertEqual(created, [])
		self.assertEqual(result.target("branch", "krcs branch test"), "county")

	def test_subcounty_names_are_scoped_to_the_resolved_county(self):
		nodes = self.seeds() + [
			node("other", "Other", "county", "central"),
			node("town-other", "Town", "sub_county", "other"),
			node("town", "Town", "sub_county", "county"),
		]
		report, created, cw = self.run_geography(
			nodes, self.companies(), [dict(name="Murang'a")], [dict(name="Town", county="Murang'a")]
		)
		self.assertEqual(report["errors"], [])
		self.assertEqual(created, [])
		self.assertEqual(cw.target("residence", "sub_county:town"), "town")

	def test_mapped_subcounty_does_not_require_a_reconstructed_source_county(self):
		cw = crosswalks(residence={"sub_county:MURANGA NORTH": "north"})
		report, created, _ = self.run_geography(
			self.seeds(), subcounties=[dict(name="MURANGA NORTH", county="missing source master")], cw=cw
		)
		self.assertEqual(report["errors"], [])
		self.assertEqual(created, [])

	def test_company_display_name_can_establish_its_county_level(self):
		companies = [
			self.companies()[0],
			dict(
				name="Branch abbreviation",
				company_name="Murang'a",
				organisation_type="Company",
				parent_company="Kenya Red Cross Society",
			),
		]
		report, created, cw = self.run_geography(self.seeds(), companies, [dict(name="Murang'a")])
		self.assertEqual(report["errors"], [])
		self.assertEqual(created, [])
		self.assertEqual(cw.target("branch", "branch abbreviation"), "county")

	def test_county_matching_does_not_cross_national_roots(self):
		nodes = self.seeds() + [
			node("root2", "Other Nation", "national"),
			node("county2", "Murang'a", "county", "root2"),
		]
		report, created, cw = self.run_geography(nodes, self.companies()[:1], [dict(name="Murang'a")])
		self.assertEqual(report["errors"], [])
		self.assertEqual(created, [])
		self.assertEqual(cw.target("residence", normalise.key("county:Murang'a")), "county")

	def test_ambiguous_counties_require_a_mapping_instead_of_another_node(self):
		nodes = self.seeds() + [node("county2", "Murang’a", "county", "root")]
		report, created, _ = self.run_geography(nodes, self.companies()[:1], [dict(name="Murang'a")])
		self.assertEqual(created, [])
		self.assertTrue(any("Ambiguous" in r["error"] for r in report["errors"]))

	def test_new_locations_still_use_native_insert_and_second_pass_reuses_them(self):
		nodes = self.seeds()
		companies = self.companies()[:1]
		counties = [dict(name="New County")]
		subcounties = [dict(name="New Town", county="New County")]
		report, created, cw = self.run_geography(nodes, companies, counties, subcounties)
		self.assertEqual(report["errors"], [])
		self.assertEqual(len(created), 2)
		self.assertEqual(created[1]["parent_geo_node"], created[0]["name"])
		second, again, _ = self.run_geography(nodes, companies, counties, subcounties, cw)
		self.assertEqual(second["errors"], [])
		self.assertEqual(again, [])

	def test_distinct_source_names_with_extra_whitespace_share_the_existing_node(self):
		nodes = self.seeds() + [node("town", "Webuye East", "sub_county", "county")]
		cw = crosswalks(residence={"sub_county:Webuye East": "town"})
		report, created, _ = self.run_geography(
			nodes,
			self.companies()[:1],
			[dict(name="Murang'a")],
			[dict(name="Webuye East", county="Murang'a"), dict(name="Webuye  East", county="Murang'a")],
			cw,
		)
		self.assertEqual(report["errors"], [])
		self.assertEqual(created, [])
		self.assertEqual(report["geography"]["Sub County"], 2)

	def test_explicit_county_mapping_resolves_an_ambiguous_name(self):
		nodes = self.seeds() + [node("county2", "Murang’a", "county", "root")]
		cw = crosswalks(residence={"county:Murang'a": "county"})
		report, created, _ = self.run_geography(nodes, self.companies()[:1], [dict(name="Murang'a")], cw=cw)
		self.assertEqual(report["errors"], [])
		self.assertEqual(created, [])

	def test_old_source_code_does_not_bypass_level_or_parent_validation(self):
		for level, parent in [("region", "root"), ("county", "central")]:
			with self.subTest(level=level, parent=parent):
				code = "vmms-" + reference.stable_key("County\0New County")
				nodes = self.seeds() + [node("coded", "Old label", level, parent, code)]
				report, created, _ = self.run_geography(
					nodes, self.companies()[:1], [dict(name="New County")]
				)
				self.assertEqual(created, [])
				self.assertTrue(any("different level/parent" in row["error"] for row in report["errors"]))

	def test_drop_and_wildcard_policies_are_not_used_as_geographic_identities(self):
		cw = crosswalks(branch={"*": "north"}, residence={"county:Murang'a": "-"})
		report, created, result = self.run_geography(
			self.seeds(), self.companies()[:1], [dict(name="Murang'a")], cw=cw
		)
		self.assertEqual(report["errors"], [])
		self.assertEqual(created, [])
		self.assertEqual(result.entries["residence"][normalise.key("county:Murang'a")]["target"], "-")
