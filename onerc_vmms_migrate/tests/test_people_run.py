from unittest import TestCase
from unittest.mock import MagicMock, patch
from onerc_vmms_migrate import people_run


class TestPeopleSequence(TestCase):
	def state(self, **kwargs):
		return {
			"phase": "people",
			"offset": 0,
			"people_offset": 0,
			"batch_number": 0,
			"auto_continue": False,
			"next_person_offset": 50,
			"people_total": 120,
			**kwargs,
		}

	def test_first_page_stops_after_files_then_resumes_at_next_person(self):
		state = self.state(phase="profile_files", offset=20, files={"offset": 20, "total": 30})
		people_run.advance(state, 10, 30, 0)
		self.assertEqual((state["phase"], state["people_offset"], state["status"]), ("people", 50, "paused"))
		self.assertEqual(state["files"]["offset"], 30)

	def test_continuous_runs_people_and_files_before_accounts_and_versions(self):
		state = self.state(
			phase="profile_files", auto_continue=True, next_person_offset=120, files={"offset": 0, "total": 0}
		)
		people_run.advance(state, 0, 0, 0)
		self.assertEqual(state["phase"], "accounts")
		people_run.advance(state, 50, 50, 0)
		self.assertEqual(state["phase"], "versions")
		people_run.advance(state, 50, 50, 0)
		self.assertEqual(state["phase"], "counters")

	def test_file_failure_keeps_same_cursor_and_people_commit_is_not_repeated(self):
		state = self.state(phase="profile_files", offset=5, files={"offset": 5, "total": 100})
		people_run.advance(state, 0, 100, 1)
		self.assertEqual(state["status"], "blocked")
		self.assertEqual(state["offset"], 5)
		self.assertEqual(state["people_offset"], 0)

	def test_source_identity_cannot_change_between_captures(self):
		self.assertEqual(people_run.source_identity({"source": {"source_id": "A", "db_name": "old"}}), "A")
		with self.assertRaisesRegex(ValueError, "different source"):
			people_run.check_source("A", {"source": {"source_id": "B"}})

	def test_changed_people_are_reported_and_never_sent_to_loader(self):
		bundle = {"key": "a", "source_hash": "new", "errors": [], "sources": []}
		with (
			patch.object(people_run.load, "already_migrated", return_value={"source_hash": "old"}),
			patch.object(people_run.load, "load_bundle") as loader,
		):
			report = people_run.import_people({}, None, {}, {"run_id": "r"}, [bundle])
		loader.assert_not_called()
		self.assertEqual(report["changed"][0]["person"], "a")

	def test_profiles_and_file_cursors_commit_atomically_and_failed_file_page_keeps_prior_people(self):
		import copy
		import frappe
		from onerc_vmms_migrate import batches, silence
		from contextlib import nullcontext

		durable = {}
		pending = {}
		database = MagicMock()
		database.set_single_value.side_effect = lambda dt, field, raw: pending.update(checkpoint=raw)
		database.commit.side_effect = lambda: durable.update(copy.deepcopy(pending))
		state = self.state(
			scope="people",
			run_id="r",
			session_id="s",
			fingerprint="f",
			phase="profile_files",
			offset=10,
			files={"offset": 10, "total": 100},
			attempt_number=0,
		)
		with (
			patch.object(frappe, "db", database),
			patch.object(frappe.utils, "now_datetime", return_value="2026-11-11"),
			patch.object(batches, "silenced", return_value=nullcontext()),
			patch.object(batches, "Guard"),
			patch.object(batches.workspace, "report_dir", return_value="/tmp"),
			patch.object(batches.workspace, "write_json"),
			patch("onerc_vmms_migrate.run._write_csv"),
			patch.object(batches, "work", return_value=(5, 100, [], {})),
		):
			batches.step.__wrapped__(state, MagicMock(data={}), {})
		import json

		saved = json.loads(durable["checkpoint"])
		self.assertEqual(saved["offset"], 15)
		self.assertEqual(saved["files"]["offset"], 15)
		self.assertEqual(saved["people_offset"], 0)
		database.commit.assert_called_once()
