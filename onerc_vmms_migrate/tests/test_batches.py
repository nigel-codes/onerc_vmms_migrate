"""Resumable batches must never acknowledge work that was rolled back."""

import copy
import json
import sqlite3
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate import batch_browser, batches, browser, historical
from onerc_vmms_migrate.history_load import BatchBoundary, Loader


class TestBatchCheckpoint(TestCase):
	def test_preflight_blocks_missing_lookup_targets_but_allows_reported_residence_fallbacks(self):
		from onerc_vmms_migrate import run
		from onerc_vmms_migrate.crosswalk import Crosswalks

		cw = MagicMock()
		cw.problems.return_value = [
			{
				"kind": "profession",
				"source_value": "Counsellor",
				"problem": "target 'Counsellor' does not exist on this site",
			},
			{"kind": "residence", "source_value": "ward:Legacy", "problem": "unmapped"},
		]
		with (
			patch.object(batches, "build", return_value={"bundles": []}) as build,
			patch.object(Crosswalks, "read", return_value=cw),
			patch.object(run, "_report_plan", return_value={}),
		):
			_, _, failures, summary = batches.work(
				{}, None, {}, {"phase": "preflight", "offset": 0, "batch_size": 1000, "run_id": "test"}, None
			)
		self.assertEqual(len(failures), 1)
		self.assertEqual(failures[0]["kind"], "profession")
		self.assertEqual(summary["missing_reference_targets"], 1)
		self.assertIs(build.call_args.kwargs["crosswalks"], cw)

	def test_preflight_finds_native_phone_failures_before_loading_records(self):
		from onerc_vmms_migrate import run
		from onerc_vmms_migrate.crosswalk import Crosswalks

		built = {
			"bundles": [{"key": "person", "errors": [], "profile": {"phone": "+2542049963400719047340"}}]
		}
		value = {"phase": "preflight", "offset": 0, "batch_size": 1000, "run_id": "test"}
		with (
			patch.object(batches, "build", return_value=built),
			patch.object(Crosswalks, "read"),
			patch.object(run, "_report_plan", side_effect=lambda *args: {}),
		):
			processed, total, failures, summary = batches.work({}, None, {}, value, None)
			self.assertEqual((processed, total, len(failures)), (1, 1, 1))
			self.assertEqual(summary["native_phones_requiring_review"], 1)
			_processed, _total, reviewed, _summary = batches.work(
				{}, None, {"drop_invalid_phones": True}, value, None
			)
			self.assertFalse(reviewed)

	def test_checkpoint_and_records_commit_together(self):
		pending, durable = {}, {}
		db = MagicMock()
		db.set_single_value.side_effect = lambda dt, field, value: pending.update(state=value)
		db.commit.side_effect = lambda: durable.update(copy.deepcopy(pending))
		with (
			patch.object(frappe, "db", db),
			patch.object(frappe.utils, "now_datetime", return_value="2026-10-19 20:00:00"),
		):
			batches.commit_checkpoint({"phase": "people", "offset": 1000})
		self.assertEqual(json.loads(durable["state"])["offset"], 1000)

	def test_failure_keeps_cursor_for_idempotent_retry(self):
		value = {"phase": "people", "offset": 1000, "batch_number": 1}
		batches.advance(value, processed=1000, total=2500, failures=2)
		self.assertEqual(value["offset"], 1000)
		self.assertEqual(value["status"], "blocked")
		self.assertEqual(value["failed"], 2)

	def test_success_moves_to_next_phase_only_at_end(self):
		value = {"phase": "people", "offset": 1000, "batch_number": 1}
		batches.advance(value, processed=1000, total=2500, failures=0)
		self.assertEqual((value["phase"], value["offset"]), ("people", 2000))
		batches.advance(value, processed=500, total=2500, failures=0)
		self.assertEqual((value["phase"], value["offset"]), ("history", 0))

	def test_archive_page_includes_users_after_history_without_loading_entire_store(self):
		class Store:
			def __len__(self):
				return 5

			def page(self, start, size):
				return [
					{"doctype": "Task", "record": {"name": str(i)}}
					for i in range(start, min(5, start + size))
				]

			def values(self):
				raise AssertionError("Must not scan entire history for each page")

		snapshot = {"history": Store(), "users": {"b": {"name": "b"}, "a": {"name": "a"}}}
		page = batches.archive_page(snapshot, 4, 3)
		self.assertEqual(
			[(r["doctype"], r["record"]["name"]) for r in page], [("Task", "4"), ("User", "a"), ("User", "b")]
		)

	def test_trial_cannot_be_used_as_a_live_rehearsal(self):
		self.assertFalse(
			batches.public_state({"mode": "test", "status": "finished", "passed": True}).get("rehearsal")
		)

	def test_batch_size_is_bounded(self):
		for invalid in (0, -1, 1001, True, 1.5):
			with self.subTest(invalid=invalid), self.assertRaises(ValueError):
				batches.validate_size(invalid)
		self.assertEqual(batches.validate_size(1000), 1000)

	def test_inconsistent_source_count_stops_instead_of_queuing_empty_batches_forever(self):
		value = {"phase": "archive", "offset": 1000, "batch_number": 1}
		with self.assertRaisesRegex(ValueError, "no progress"):
			batches.advance(value, processed=0, total=2000, failures=0)


class TransactionDB:
	"""Real transactions; only the Frappe boundary is replaced."""

	MAX_WRITES_PER_TRANSACTION = 100000
	auto_commit_on_many_writes = 0

	def __init__(self, path):
		self.connection = sqlite3.connect(path)
		self.connection.executescript(
			"CREATE TABLE IF NOT EXISTS checkpoint (value TEXT); CREATE TABLE IF NOT EXISTS people (name TEXT PRIMARY KEY);"
		)
		self.connection.commit()

	def set_single_value(self, dt, field, value):
		self.connection.execute("DELETE FROM checkpoint")
		self.connection.execute("INSERT INTO checkpoint VALUES (?)", (value,))

	def get_single_value(self, dt, field):
		row = self.connection.execute("SELECT value FROM checkpoint").fetchone()
		return row[0] if row else None

	def exists(self, *args):
		return True

	def commit(self):
		self.connection.commit()

	def savepoint(self, name):
		self.connection.execute("SAVEPOINT " + name)

	def rollback(self, save_point=None):
		if save_point:
			self.connection.execute("ROLLBACK TO SAVEPOINT " + save_point)
		else:
			self.connection.rollback()


class TestTransactionalBatch(TestCase):
	def exercise(self, failure=False, guard_failure=False):
		directory = tempfile.TemporaryDirectory()
		self.addCleanup(directory.cleanup)
		db = TransactionDB(str(Path(directory.name) / "destination.sqlite"))
		self.addCleanup(db.connection.close)
		stack = self.enterContext(ExitStack())
		stack.enter_context(patch.object(frappe, "db", db))
		stack.enter_context(patch.object(frappe, "conf", frappe._dict()))
		stack.enter_context(patch.object(frappe.utils, "now_datetime", return_value="2026-10-19 20:00:00"))
		stack.enter_context(patch.object(batches.workspace, "report_dir", return_value=directory.name))
		from contextlib import nullcontext

		stack.enter_context(patch.object(batches, "silenced", nullcontext))
		guard = MagicMock()
		if guard_failure:
			from onerc_vmms_migrate.silence import SilenceBreached

			guard.check.side_effect = SilenceBreached("Unexpected notification")
		stack.enter_context(patch.object(batches, "Guard", return_value=guard))
		value = {"phase": "people", "offset": 0, "batch_number": 0, "run_id": "trial", "fingerprint": "fixed"}
		batches.commit_checkpoint(value)

		def work(*args):
			db.connection.execute("INSERT OR IGNORE INTO people VALUES (?)", ("one",))
			if failure:
				raise RuntimeError("worker interrupted before commit")
			return 1, 2, [], {"loaded": 1}

		stack.enter_context(patch.object(batches, "work", side_effect=work))
		return db, value

	def test_interrupt_rolls_back_records_and_keeps_the_previous_cursor(self):
		db, value = self.exercise(failure=True)
		with self.assertRaisesRegex(RuntimeError, "interrupted"):
			batches.step(value, SimpleNamespace(data={}), {})
		db.rollback()
		self.assertEqual(batches.checkpoint()["offset"], 0)
		self.assertEqual(db.connection.execute("SELECT count(*) FROM people").fetchone()[0], 0)

	def test_success_persists_both_records_and_cursor(self):
		db, value = self.exercise()
		batches.step(value, SimpleNamespace(data={}), {})
		db.rollback()  # A later worker/request rollback cannot undo this batch.
		self.assertEqual(batches.checkpoint()["offset"], 1)
		self.assertEqual(db.connection.execute("SELECT count(*) FROM people").fetchone()[0], 1)

	def test_notifications_prevent_any_batch_commit(self):
		db, value = self.exercise(guard_failure=True)
		from onerc_vmms_migrate.silence import SilenceBreached

		with self.assertRaises(SilenceBreached):
			batches.step(value, SimpleNamespace(data={}), {})
		db.rollback()
		self.assertEqual(batches.checkpoint()["offset"], 0)
		self.assertEqual(db.connection.execute("SELECT count(*) FROM people").fetchone()[0], 0)

	def test_validation_failure_commits_successes_but_retry_keeps_the_same_range(self):
		db, value = self.exercise()

		def partial(*args):
			db.connection.execute("INSERT OR IGNORE INTO people VALUES (?)", ("one",))
			return 2, 2, [{"person": "two", "error": "Missing required reference"}], {"loaded": 1}

		with patch.object(batches, "work", side_effect=partial):
			batches.step(value, SimpleNamespace(data={}), {})
		self.assertEqual(batches.checkpoint()["offset"], 0)
		self.assertEqual(batches.checkpoint()["status"], "blocked")
		self.assertEqual(db.connection.execute("SELECT count(*) FROM people").fetchone()[0], 1)

		def retry(*args):
			for name in ("one", "two"):
				db.connection.execute("INSERT OR IGNORE INTO people VALUES (?)", (name,))
			return 2, 2, [], {"loaded": 1, "existing": 1}

		with patch.object(batches, "work", side_effect=retry):
			batches.step(batches.checkpoint(), SimpleNamespace(data={}), {})
		self.assertEqual(batches.checkpoint()["phase"], "history")
		self.assertEqual(db.connection.execute("SELECT count(*) FROM people").fetchone()[0], 2)

	def worker_mocks(self, db, value):
		from contextlib import nullcontext

		cache = MagicMock()
		cache.get.side_effect = lambda key: b"token" if key == "lock" else None
		view = {"rehearsal": {"passed": False}}
		value.update(mode="test", status="ready", batch_size=1000, capture_id="capture")
		batches.commit_checkpoint(value)
		stack = self.enterContext(ExitStack())
		for target, name, replacement in [
			(frappe, "cache", cache),
			(frappe, "set_user", MagicMock()),
			(frappe, "log_error", MagicMock()),
			(browser, "lock_key", lambda: "lock"),
			(browser, "state", lambda: view),
			(browser, "save_state", MagicMock()),
			(browser, "authorize", MagicMock()),
			(browser, "fingerprint", lambda: "fixed"),
			(batch_browser, "pause_key", lambda: "pause"),
			(batch_browser, "begin_cutover", MagicMock()),
			(workspace_module(), "config", lambda: {}),
		]:
			stack.enter_context(patch.object(target, name, replacement))
		stack.enter_context(
			patch("onerc_vmms_migrate.source.Source", return_value=nullcontext(SimpleNamespace(data={})))
		)
		queue = stack.enter_context(patch.object(batch_browser, "queue"))
		return view, queue

	def test_worker_queues_the_next_batch_only_after_a_durable_checkpoint(self):
		db, value = self.exercise()
		view, queue = self.worker_mocks(db, value)
		batch_browser.execute("token", "batch_test", "Administrator", 1000)
		self.assertEqual(batches.checkpoint()["offset"], 1)
		queue.assert_called_once_with("batch_test", "Administrator", 1000)
		self.assertFalse(view["rehearsal"]["passed"])

	def test_worker_failure_reloads_the_durable_cursor(self):
		db, value = self.exercise(failure=True)
		_view, queue = self.worker_mocks(db, value)
		batch_browser.execute("token", "batch_test", "Administrator", 1000)
		self.assertEqual(batches.checkpoint()["offset"], 0)
		self.assertEqual(batches.checkpoint()["status"], "blocked")
		self.assertEqual(db.connection.execute("SELECT count(*) FROM people").fetchone()[0], 0)
		queue.assert_not_called()

	def test_2500_people_resume_in_1000_record_batches_after_a_repaired_failure(self):
		db, value = self.exercise()
		value.update(mode="test", batch_size=1000)
		bundles = [
			{
				"key": f"person-{i:04d}",
				"source_hash": "unchanged",
				"sources": [],
				"memberships": [],
				"errors": [],
			}
			for i in range(2500)
		]
		broken = {"person-1450"}

		def native_load(bundle, *args, **kwargs):
			if bundle["key"] in broken:
				raise ValueError("Missing profession reference")
			db.connection.execute("INSERT INTO people VALUES (?)", (bundle["key"],))
			return {"user": None, "volunteer": None, "member": None}

		def already(key):
			found = db.connection.execute("SELECT name FROM people WHERE name=?", (key,)).fetchone()
			return SimpleNamespace(source_hash="unchanged") if found else None

		# Replace the fake work from exercise() with the real batching engine.
		self.enterContext(patch.object(batches, "work", wraps=REAL_WORK))
		self.enterContext(patch.object(batches, "build", return_value={"bundles": bundles}))
		from onerc_vmms_migrate import load

		self.enterContext(patch.object(load, "load_bundle", side_effect=native_load))
		self.enterContext(patch.object(load, "already_migrated", side_effect=already))
		self.enterContext(patch.object(frappe, "clear_messages"))
		source = SimpleNamespace(data={})
		batches.step(value, source, {})
		self.assertEqual(batches.checkpoint()["offset"], 1000)
		batches.step(batches.checkpoint(), source, {})
		self.assertEqual((batches.checkpoint()["status"], batches.checkpoint()["offset"]), ("blocked", 1000))
		self.assertEqual(db.connection.execute("SELECT count(*) FROM people").fetchone()[0], 1999)
		broken.clear()
		batches.step(batches.checkpoint(), source, {})
		self.assertEqual(batches.checkpoint()["offset"], 2000)
		batches.step(batches.checkpoint(), source, {})
		self.assertEqual((batches.checkpoint()["phase"], batches.checkpoint()["offset"]), ("history", 0))
		self.assertEqual(db.connection.execute("SELECT count(*) FROM people").fetchone()[0], 2500)


def workspace_module():
	from onerc_vmms_migrate import workspace

	return workspace


REAL_WORK = batches.work


class TestHistoryBatches(TestCase):
	def loader(self):
		instance = object.__new__(Loader)
		instance.snapshot = {"source": {"source_id": "source"}}
		instance.run_id = "trial"
		instance.excluded_identities = set()
		instance.targets, instance.by_source = {}, {}
		instance.results, instance.failures, instance.excluded, instance.warnings = [], [], [], []
		instance.batch_limit, instance.batch_deadline = 1, time.monotonic() + 60
		instance.batch_attempted, instance.batch_progress = 0, None
		instance.converted_index = {("Task", "old"): "existing-task"}
		return instance

	def test_existing_records_do_not_consume_the_batch_allowance(self):
		instance = self.loader()
		self.assertEqual(instance.one("Task", {"name": "old"}, "Task", lambda r: {}), "existing-task")
		self.assertEqual(instance.batch_attempted, 0)
		db = MagicMock()
		with (
			patch.object(frappe, "db", db),
			patch.object(historical, "insert", return_value=SimpleNamespace(name="new-task")),
			patch.object(historical, "mark"),
		):
			self.assertEqual(instance.one("Task", {"name": "new"}, "Task", lambda r: {}), "new-task")
			with self.assertRaises(BatchBoundary):
				instance.one("Task", {"name": "third"}, "Task", lambda r: {})
		self.assertEqual(instance.batch_attempted, 1)
		self.assertEqual(instance.converted_index[("Task", "new")], "new-task")


class TestBatchAuthorization(TestCase):
	def setUp(self):
		def throw(message, exc=frappe.ValidationError, **kwargs):
			raise exc(message)

		self.enterContext(patch.object(frappe, "throw", side_effect=throw))
		self.enterContext(patch.object(browser, "authorize"))
		self.enterContext(
			patch.object(frappe, "get_single", return_value=SimpleNamespace(browser_capture="capture"))
		)
		self.enterContext(patch.object(batches, "checkpoint", return_value=None))
		self.enterContext(patch.object(browser, "state", return_value={}))
		self.enterContext(patch.object(browser, "fingerprint", return_value="current"))

	def test_trial_requires_explicit_persistence_and_backup_confirmation(self):
		for kwargs in ({}, {"confirmation": "TEST"}, {"confirmation": "TEST", "test_destination": True}):
			with self.subTest(kwargs=kwargs), self.assertRaises(frappe.ValidationError):
				browser.start("batch_test", **kwargs)

	def test_trial_does_not_bypass_the_live_rehearsal_gate(self):
		with self.assertRaisesRegex(frappe.ValidationError, "successful full rehearsal"):
			browser.start("import", "LOAD", True, True, True, batch_size=1000)

	def test_trial_start_queues_one_batch_with_a_numeric_limit(self):
		with (
			patch.object(batch_browser, "assert_job_idle"),
			patch.object(frappe, "cache", MagicMock()),
			patch.object(frappe, "session", SimpleNamespace(user="Administrator")),
			patch.object(batch_browser, "queue", return_value={"job_id": "job"}) as queue,
		):
			self.assertEqual(
				browser.start(
					"batch_test",
					confirmation="TEST",
					backup_confirmed=True,
					test_destination=True,
					batch_size=1000,
				),
				{"job_id": "job"},
			)
			self.assertEqual(queue.call_args.args, ("batch_test", "Administrator", 1000))

	def test_unknown_worker_state_never_releases_a_lock(self):
		cache = MagicMock()
		cache.get.return_value = b"job"
		with (
			patch.object(frappe, "cache", cache),
			patch("frappe.utils.background_jobs.get_job_status", return_value=None),
		):
			with self.assertRaises(frappe.ValidationError):
				batch_browser.assert_job_idle()
		cache.delete.assert_not_called()

	def test_dead_worker_lock_can_be_recovered_without_waiting_six_hours(self):
		cache = MagicMock()
		cache.get.return_value = b"job"
		with (
			patch.object(frappe, "cache", cache),
			patch("frappe.utils.background_jobs.get_job_status", return_value="failed"),
		):
			batch_browser.assert_job_idle()
		cache.delete.assert_called_once()
