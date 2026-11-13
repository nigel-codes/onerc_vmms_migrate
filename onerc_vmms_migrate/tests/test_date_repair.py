from unittest import TestCase

from onerc_vmms_migrate import date_repair as repair


class TestDatePolicy(TestCase):
	def test_earliest_creation_for_merged_identity_is_not_joining_date(self):
		expected, issues = repair.expected_dates(
			"VMMS Volunteer",
			[{"creation": "2020-02-03 10:20:30"}, {"creation": "2019-04-05 11:00:00"}],
			"2018-01-01",
		)
		self.assertEqual(expected, {"creation": "2019-04-05 11:00:00.000000", "joined_on": "2018-01-01"})
		self.assertEqual(issues, [])

	def test_missing_creation_is_reported_and_not_invented(self):
		expected, issues = repair.expected_dates("Red Profile", [{}])
		self.assertEqual(expected, {})
		self.assertTrue(issues)

	def test_missing_join_date_clears_only_migration_day_fallback(self):
		before = {
			"creation": "2026-10-20 10:00:00",
			"joined_on": "2026-10-20",
			"modified": "2026-10-20 11:00:00",
		}
		changes, held = repair.changes_for(before, {"joined_on": None}, {"2026-10-20"})
		self.assertEqual(changes, {"joined_on": None})
		self.assertEqual(held, [])
		before["joined_on"] = "2024-01-02"
		changes, held = repair.changes_for(before, {"joined_on": None}, {"2026-10-20"})
		self.assertEqual(changes, {})
		self.assertTrue(held)

	def test_invalid_or_future_source_dates_are_held(self):
		expected, issues = repair.expected_dates("VMMS Member", [{"creation": "not-a-date"}], "2999-01-01")
		self.assertEqual(expected, {})
		self.assertEqual(len(issues), 2)

	def test_metadata_comparison_uses_microseconds_and_blank_dates(self):
		before = {"creation": "2020-01-01 10:00:00", "joined_on": ""}
		changes, issues = repair.changes_for(
			before, {"creation": "2020-01-01 10:00:00.000000", "joined_on": None}, set()
		)
		self.assertEqual(changes, {})
		self.assertEqual(issues, [])


class TestDateRepairPlan(TestCase):
	def account_plan(
		self, *, imported_enriched=True, changed=False, missing_creation=False, history_creation=False
	):
		import hashlib
		from unittest.mock import patch

		import frappe

		from onerc_vmms_migrate import batches
		from onerc_vmms_migrate.history_model import canonical

		raw = {"name": "account@example.test", "email": "account@example.test", "enabled": 1}
		if not missing_creation:
			raw["creation"] = "2019-01-02 09:00:00"
		enriched = {**raw, "bio": "Captured biography"}
		if history_creation:
			enriched["creation"] = "2019-01-02 09:00:00"
		fingerprint = hashlib.sha256(canonical(enriched if imported_enriched else raw).encode()).hexdigest()
		snapshot = {
			"users": {raw["name"]: dict(raw)},
			"history": {"user": {"doctype": "User", "record": {**enriched, "enabled": 0 if changed else 1}}},
		}
		if changed:
			snapshot["users"][raw["name"]]["enabled"] = 0
		link = frappe._dict(
			name="account-ledger",
			person_key="account:" + raw["name"],
			source_doctype="User",
			source_name=raw["name"],
			target_doctype="User",
			target_name=raw["name"],
			source_hash=fingerprint,
			outcome="created",
			creation="2026-11-12 10:00:00",
			migrated_on="2026-11-12 10:00:00",
		)
		native = frappe._dict(
			name=raw["name"], creation="2026-11-12 10:00:00", modified="2026-11-12 11:00:00"
		)
		with (
			patch.object(
				frappe,
				"get_all",
				side_effect=lambda dt, **kw: (
					[link] if dt == "Legacy Migration Record" else [native] if dt == "User" else []
				),
			),
			patch.object(batches, "build", return_value={"bundles": []}),
		):
			return repair.build_plan(snapshot, {}, {"run_id": "run"})

	def test_account_hash_uses_the_same_captured_enrichment_as_import(self):
		plan = self.account_plan()
		self.assertEqual(plan["held"], [])
		self.assertEqual(len(plan["tasks"]), 1)
		self.assertEqual(plan["tasks"][0]["after"], {"creation": "2019-01-02 09:00:00.000000"})

	def test_older_account_raw_fingerprint_remains_supported(self):
		plan = self.account_plan(imported_enriched=False)
		self.assertEqual(plan["held"], [])
		self.assertEqual(len(plan["tasks"]), 1)

	def test_genuine_account_changes_are_held_once_without_a_false_missing_date(self):
		for enriched in (True, False):
			with self.subTest(enriched=enriched):
				plan = self.account_plan(imported_enriched=enriched, changed=True)
				self.assertEqual(plan["tasks"], [])
				self.assertEqual([row["reason"] for row in plan["held"]], ["Account source hash changed"])

	def test_missing_source_date_is_reported_when_identity_is_verified(self):
		plan = self.account_plan(missing_creation=True)
		self.assertEqual(plan["tasks"], [])
		self.assertEqual(
			[row["reason"] for row in plan["held"]],
			["Source creation timestamp absent; destination creation unchanged"],
		)

	def test_raw_fingerprint_does_not_authorize_dates_only_in_another_representation(self):
		plan = self.account_plan(imported_enriched=False, missing_creation=True, history_creation=True)
		self.assertEqual(plan["tasks"], [])
		self.assertEqual(
			[row["reason"] for row in plan["held"]],
			["Source creation timestamp absent; destination creation unchanged"],
		)

	def test_ledger_created_only_and_earliest_profile_source(self):
		from unittest.mock import patch

		import frappe

		from onerc_vmms_migrate import batches

		bundle = {
			"key": "person",
			"source_hash": "hash",
			"errors": [],
			"sources": [("User", "u"), ("Employee", "e")],
			"volunteer": {"joined_on": "2017-03-04"},
		}
		snapshot = {
			"users": {"u": {"name": "u", "creation": "2015-01-01 09:00:00"}},
			"employees": {"e": {"name": "e", "creation": "2016-02-02 08:00:00"}},
		}

		def ledger(dt, target, source_dt, source, outcome="created"):
			return frappe._dict(
				name=dt,
				person_key="person",
				source_doctype=source_dt,
				source_name=source,
				target_doctype=dt,
				target_name=target,
				source_hash="hash",
				outcome=outcome,
				creation="2026-10-20 10:00:00",
				migrated_on="2026-10-20 10:00:00",
			)

		links = [
			ledger("Red Profile", "rp", "Person", "person"),
			ledger("VMMS Volunteer", "v", "Employee", "e"),
			ledger("User", "adopted", "User", "u", "adopted"),
		]

		def get_all(dt, **kwargs):
			if dt == "Legacy Migration Record":
				return links
			if dt == "Red Profile":
				return [
					frappe._dict(name="rp", creation="2026-10-20 10:00:00", modified="2026-10-20 10:01:00")
				]
			if dt == "VMMS Volunteer":
				return [
					frappe._dict(
						name="v",
						creation="2026-10-20 10:00:00",
						modified="2026-10-20 10:01:00",
						joined_on="2026-10-20",
					)
				]
			return []

		with (
			patch.object(frappe, "get_all", side_effect=get_all),
			patch.object(batches, "build", return_value={"bundles": [bundle]}),
		):
			plan = repair.build_plan(snapshot, {}, {"run_id": "run"})
		self.assertEqual(plan["summary"]["records_to_change"], 2)
		self.assertEqual(plan["summary"]["adopted_preserved"], 1)
		self.assertEqual(plan["tasks"][0]["after"]["creation"], "2015-01-01 09:00:00.000000")
		self.assertEqual(plan["tasks"][1]["after"]["joined_on"], "2017-03-04")


class TestDateRepairApply(TestCase):
	def task(self):
		return {
			"doctype": "VMMS Volunteer",
			"name": "V1",
			"before": {
				"creation": "2026-10-20 10:00:00",
				"modified": "2026-10-20 10:01:00",
				"joined_on": "2026-10-20",
			},
			"after": {"creation": "2018-01-02 09:00:00.000000", "joined_on": "2017-01-01"},
			"ledger": [
				{
					"name": "L1",
					"target_doctype": "VMMS Volunteer",
					"target_name": "V1",
					"source_hash": "hash",
					"outcome": "created",
				}
			],
		}

	def test_date_changes_preserve_modified_and_never_run_save_hooks(self):
		from unittest.mock import MagicMock, patch

		import frappe

		from onerc_vmms_migrate import date_repair_control as control

		task = self.task()
		with patch.object(frappe, "db", MagicMock()) as db, patch.object(frappe, "clear_document_cache"):
			db.get_value.side_effect = [task["ledger"][0], task["before"]]
			control.apply_task(task)
			db.set_value.assert_called_once_with("VMMS Volunteer", "V1", task["after"], update_modified=False)
			self.assertTrue(db.get_value.call_args.kwargs["for_update"])

	def test_concurrent_native_edits_and_changed_ledger_stop_without_writes(self):
		from unittest.mock import MagicMock, patch

		import frappe

		from onerc_vmms_migrate import date_repair_control as control

		task = self.task()
		for ledger, native in [
			(task["ledger"][0], {**task["before"], "modified": "2026-10-21 10:00:00"}),
			({**task["ledger"][0], "target_name": "someone-else"}, task["before"]),
		]:
			with self.subTest(ledger=ledger), patch.object(frappe, "db", MagicMock()) as db:
				db.get_value.side_effect = [ledger, native]
				with self.assertRaises(ValueError):
					control.apply_task(task)
				db.set_value.assert_not_called()

	def test_retry_is_idempotent_and_other_fields_are_rejected(self):
		from unittest.mock import MagicMock, patch

		import frappe

		from onerc_vmms_migrate import date_repair_control as control

		task = self.task()
		with patch.object(frappe, "db", MagicMock()) as db:
			db.get_value.side_effect = [task["ledger"][0], {**task["before"], **task["after"]}]
			control.apply_task(task)
			db.set_value.assert_not_called()
			task["after"]["owner"] = "someone-else"
			with self.assertRaises(ValueError):
				control.apply_task(task)
			db.set_value.assert_not_called()


class TestDateRepairControl(TestCase):
	def test_repair_requires_matching_completed_capture_and_active_cutover(self):
		from unittest.mock import patch

		from onerc_vmms_migrate import date_repair_control as control

		proof = {"status": "finished", "passed": True, "capture_id": "capture"}
		with (
			patch.object(control.batches, "checkpoint", return_value=proof),
			patch.object(control.workspace, "config", return_value={"browser_capture": "capture"}),
			patch.object(control.browser, "state", return_value={"cutover": {"mute_emails": 0}}),
		):
			self.assertEqual(control.qualified(), proof)
			proof["passed"] = False
			with self.assertRaises(ValueError):
				control.qualified()
		proof["passed"] = True
		with (
			patch.object(control.batches, "checkpoint", return_value=proof),
			patch.object(control.workspace, "config", return_value={"browser_capture": "other"}),
		):
			with self.assertRaises(ValueError):
				control.qualified()

	def test_plan_page_corruption_stops_before_any_native_write(self):
		from unittest.mock import MagicMock, mock_open, patch

		import frappe

		from onerc_vmms_migrate import date_repair_control as control

		value = {"offset": 0, "total": 1, "run_id": "run", "pages": ["wrong-digest"]}
		with (
			patch.object(control, "ensure_current"),
			patch.object(control, "page_path", return_value="/tmp/page"),
			patch("builtins.open", mock_open(read_data="[{}]")),
			patch.object(control, "apply_task") as apply,
			patch.object(frappe, "db", MagicMock()) as db,
			patch.object(frappe, "conf", frappe._dict()),
		):
			with self.assertRaises(ValueError):
				control.step(value)
			apply.assert_not_called()
			db.set_value.assert_not_called()
			db.commit.assert_not_called()

	def test_native_failure_does_not_advance_or_commit_cursor(self):
		import json
		from unittest.mock import MagicMock, mock_open, patch

		import frappe

		from onerc_vmms_migrate import date_repair_control as control

		page = [{"doctype": "VMMS Volunteer", "name": "v"}]
		value = {"offset": 0, "total": 1, "run_id": "run", "pages": [control.digest(page)]}
		with (
			patch.object(control, "ensure_current"),
			patch.object(control, "page_path", return_value="/tmp/page"),
			patch("builtins.open", mock_open(read_data=json.dumps(page))),
			patch.object(control, "apply_task", side_effect=ValueError("changed")),
			patch.object(frappe, "db", MagicMock()) as db,
			patch.object(frappe, "conf", frappe._dict()),
			patch("onerc_vmms_migrate.silence.Guard"),
		):
			with self.assertRaises(ValueError):
				control.step(value)
			self.assertEqual(value["offset"], 0)
			db.commit.assert_not_called()

	def test_success_commits_cursor_only_after_all_native_changes(self):
		import json
		from unittest.mock import MagicMock, mock_open, patch

		import frappe

		from onerc_vmms_migrate import date_repair_control as control

		page = [{"doctype": "VMMS Volunteer", "name": "v"}]
		value = {"offset": 0, "total": 1, "run_id": "run", "pages": [control.digest(page)]}
		with (
			patch.object(control, "ensure_current"),
			patch.object(control, "page_path", return_value="/tmp/page"),
			patch("builtins.open", mock_open(read_data=json.dumps(page))),
			patch.object(control, "apply_task") as apply,
			patch.object(frappe, "db", MagicMock()) as db,
			patch.object(frappe, "conf", frappe._dict()),
			patch("onerc_vmms_migrate.silence.Guard"),
			patch.object(frappe.utils, "now_datetime", return_value="now"),
		):
			control.step(value)
			apply.assert_called_once_with(page[0])
			self.assertEqual(value["offset"], 1)
			self.assertEqual(value["status"], "finished")
			self.assertEqual(json.loads(db.set_single_value.call_args.args[2])["offset"], 1)
			db.commit.assert_called_once()
