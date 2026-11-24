"""The history import stands aside for the server's 4-minute marks (0.10.3),
and the job after it carries on with the same pages and the same report."""

import calendar
import datetime
import os
import signal
import tempfile
import threading
from collections import Counter
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate import browser, browser_source, bulk_source, history_import, history_progress, marks
from onerc_vmms_migrate.history_load import BatchBoundary


def utc(*parts):
	return calendar.timegm(datetime.datetime(*parts).timetuple())


class TestMarks(TestCase):
	def test_every_stopped_job_died_just_after_a_mark(self):
		# The RQ jobs' end times, East Africa Time (UTC+3).
		stops = [utc(2026, 11, 17, 8, 16, 3), utc(2026, 11, 23, 11, 16, 2), utc(2026, 11, 23, 12, 0, 2),
			utc(2026, 11, 23, 12, 12, 1), utc(2026, 11, 23, 14, 0, 3)]
		self.assertEqual([stop % marks.PERIOD for stop in stops], [3, 2, 2, 1, 3])

	def test_a_run_waits_for_a_stretch_with_room_for_its_work(self):
		mark = utc(2026, 11, 24, 9, 0, 0)
		slept = []

		def wait(since, need):
			slept.clear()
			return marks.wait(need, clock=lambda: mark + since, sleep=slept.append)

		self.assertEqual(wait(10, 120), 35)  # the mark has just passed: let it settle
		self.assertEqual(slept, [35])
		self.assertEqual(wait(60, 120), 0)  # 170 s left
		self.assertEqual(slept, [])
		self.assertEqual(wait(150, 120), 135)  # 80 s left: after the next mark
		self.assertEqual(wait(marks.SETTLE, 10**6), 0)  # nothing gets more than a whole stretch

	def test_a_window_closes_before_the_mark_with_room_for_the_longest_unit(self):
		mark = utc(2026, 11, 24, 9, 0, 0)
		clock = MagicMock(return_value=mark + 50)
		window = marks.Window(clock=clock)
		self.assertEqual(window.ends, mark + marks.PERIOD - marks.CLEAR)
		self.assertTrue(window.allows())
		clock.return_value = mark + 90  # a 40 s unit: the next needs 60 s
		self.assertTrue(window.allows())
		clock.return_value = mark + 175  # 55 s left
		self.assertFalse(window.allows())
		clock.return_value = mark + 100
		self.assertFalse(window.allows(need=150))
		window.longest, window.since = 0, mark + 100
		window.stop_requested = True
		self.assertFalse(window.allows())

	def test_a_sigterm_asks_for_a_stop_at_the_next_safe_point(self):
		if threading.current_thread() is not threading.main_thread():
			self.skipTest("signals reach the main thread only")
		window = marks.Window()
		before = signal.signal(signal.SIGTERM, signal.SIG_DFL)  # as in RQ's work-horse
		try:
			with marks.stop_on_term(window):
				os.kill(os.getpid(), signal.SIGTERM)
			self.assertTrue(window.stop_requested)
			self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)
		finally:
			signal.signal(signal.SIGTERM, before)

	def test_a_worker_that_runs_jobs_itself_keeps_its_shutdown_handler(self):
		if threading.current_thread() is not threading.main_thread():
			self.skipTest("signals reach the main thread only")

		def shutdown(signum, frame):
			pass

		before = signal.signal(signal.SIGTERM, shutdown)
		try:
			with marks.stop_on_term(marks.Window()):
				self.assertIs(signal.getsignal(signal.SIGTERM), shutdown)
		finally:
			signal.signal(signal.SIGTERM, before)


class Workspace(TestCase):
	def setUp(self):
		self.root = tempfile.TemporaryDirectory()
		self.addCleanup(self.root.cleanup)

		def path(*parts):
			full = os.path.join(self.root.name, *parts)
			os.makedirs(os.path.dirname(full), exist_ok=True)
			return full

		item = patch.object(history_progress.workspace, "path", side_effect=path)
		item.start()
		self.addCleanup(item.stop)


class TestPlace(Workspace):
	def test_a_run_carries_on_only_with_its_own_capture_inputs_and_kind(self):
		first = history_progress.begin("history-import", "c1", "k1", "run-1")
		history_progress.save(first)
		again = history_progress.begin("history-import", "c1", "k1", "run-2")
		self.assertEqual((again["run_id"], again["slices"]), ("run-1", 2))
		self.assertEqual(history_progress.begin("history-import", "c1", "k2", "run-3")["run_id"], "run-3")
		self.assertEqual(history_progress.begin("history-import", "c2", "k1", "run-4")["run_id"], "run-4")
		self.assertEqual(history_progress.begin("history-preview", "c1", "k1", "run-5")["run_id"], "run-5")
		history_progress.forget()
		self.assertIsNone(history_progress.load())

	def test_the_reports_lists_come_back_as_they_were_written(self):
		loader = MagicMock(spec=[])
		loader.failures = [{"source_doctype": "Project", "source_name": 7, "error": "x"}]
		loader.warnings = [{"source_doctype": "Task", "source_name": "T-1", "warning": "w",
			"creation": datetime.datetime(2025, 1, 2, 3, 4, 5)}]
		loader.excluded = [{"source_doctype": "Employee", "source_name": "VOL-2", "reason": "r"}]
		loader.validated = {("Project", "P-1")}
		loader.helpdesk_drops = Counter({("Communication", "email_account"): 3})
		loader.helpdesk_collisions = {"12": {"ticket": "12", "old_created": datetime.datetime(2024, 5, 6)}}
		loader._helpdesk_warned = {("HD Ticket", "12", "m")}
		place = history_progress.begin("history-import", "c1", "k1", "run-1")
		place["loader"] = history_progress.loader_state(loader, {"terms": "broken"}, 4)
		history_progress.save(place)

		fresh = MagicMock(spec=[])
		fresh.failures, fresh.warnings, fresh.excluded = [], [], []
		broken, pages = history_progress.restore(fresh, history_progress.load()["loader"])
		self.assertEqual((broken, pages), ({"terms": "broken"}, 4))
		self.assertEqual(fresh.failures, loader.failures)
		# The report's columns follow the first row's keys.
		self.assertEqual(list(fresh.failures[0]), ["source_doctype", "source_name", "error"])
		self.assertEqual([history_import._row(r) for r in fresh.warnings], [history_import._row(r) for r in loader.warnings])
		self.assertEqual([list(r) for r in fresh.warnings], [list(r) for r in loader.warnings])
		self.assertEqual(fresh.excluded, loader.excluded)
		self.assertEqual(fresh.validated, loader.validated)
		self.assertEqual(fresh.helpdesk_drops, loader.helpdesk_drops)
		self.assertEqual(fresh._helpdesk_warned, loader._helpdesk_warned)
		self.assertEqual(str(fresh.helpdesk_collisions["12"]["old_created"]), "2024-05-06 00:00:00")


class Window:
	"""Allows the first `units` checks, then asks for the mark to pass."""

	def __init__(self, units):
		self.units = units

	def allows(self, need=0):
		self.units -= 1
		return self.units >= 0


class TestArchiveCarriesOn(TestCase):
	snapshot = {"schemas": {"Buzz Event": {"fields": []}, "Event Booking": {"fields": []}}}
	rows = {"Buzz Event": [{"name": i} for i in range(2500)], "Event Booking": [{"name": i} for i in range(1500)]}

	def run_archive(self, steps):
		history = MagicMock()
		history.rows.side_effect = lambda dt: self.rows.get(dt, [])
		with (
			patch("onerc_vmms_migrate.history_model.History", return_value=history),
			patch.object(history_import.historical, "archive",
				side_effect=lambda snapshot, run_id, records: {"created": len(records), "reused": 0}) as archive,
			patch.object(frappe, "db", MagicMock()),
			patch.object(history_progress, "save"),
		):
			try:
				return history_import.archive(self.snapshot, "run", lambda: None, None, steps), archive
			except history_import.StepAside:
				return None, archive

	def pages(self, archive):
		return [(call.kwargs["records"][0]["doctype"], call.kwargs["records"][0]["record"]["name"])
			for call in archive.call_args_list]

	def test_the_next_job_archives_the_pages_after_the_last_commit_and_the_totals_add_up(self):
		whole, archive = self.run_archive(None)
		expected = self.pages(archive)

		place = history_progress.begin("history-import", "c1", "k1", "run")
		first, archive = self.run_archive(history_import.Steps(place, Window(1)))
		self.assertIsNone(first)
		done = self.pages(archive)
		self.assertEqual(done, expected[:2])
		self.assertEqual(place["archive"]["index"], 0)
		self.assertEqual(place["archive"]["start"], 2000)

		second, archive = self.run_archive(history_import.Steps(place, Window(2)))
		self.assertIsNone(second)
		done += self.pages(archive)
		self.assertEqual(place["archive"]["index"], 1)
		last, archive = self.run_archive(history_import.Steps(place, Window(10)))
		done += self.pages(archive)
		self.assertEqual(done, expected)  # every page once, in the same order
		self.assertEqual(last, whole)
		self.assertEqual(last["Buzz Event"], 2500)
		self.assertEqual(last["created"], 4000)


class Adapters:
	"""A terms adapter that is broken and a deployment that fails, both on the
	first page; deployments take three pages (the ledger moves on with each)."""

	def __init__(self, boundaries):
		self.naming = MagicMock()
		self.failures, self.warnings, self.excluded = [], [], []
		self.calls = []
		self.boundaries = boundaries

	def projects(self):
		self.calls.append("projects")

	def terms(self):
		self.calls.append("terms")
		raise ValueError("Set the destination legal Company")

	def deployments(self):
		self.calls.append("deployments")
		if ("Deployment Request Tool", "PDR-1") not in self.skip_identities:
			self.failures.append({"source_doctype": "Deployment Request Tool", "source_name": "PDR-1", "error": "x"})
		self.warnings.append({"source_doctype": "Deployment Request Tool", "source_name": "PDR-2", "warning": "page"})
		if self.boundaries:
			self.boundaries -= 1
			raise BatchBoundary()

	events = applications = availability = time_logs = helpdesk = lambda self: None


class TestConvertCarriesOn(TestCase):
	def setUp(self):
		for item in (
			patch.object(history_import, "converted_index", return_value={}),
			patch.object(frappe, "db", MagicMock()),
			patch.object(frappe, "clear_messages"),
			patch.object(history_progress, "save"),
		):
			item.start()
			self.addCleanup(item.stop)

	def test_the_next_job_skips_what_the_run_skipped_and_reports_the_same(self):
		whole = Adapters(2)
		commits = []
		broken = history_import.convert(whole, {}, True, lambda: commits.append(1), None)

		place = history_progress.begin("history-import", "c1", "k1", "run")
		first = Adapters(2)
		with self.assertRaises(history_import.StepAside):
			history_import.convert(first, {}, True, lambda: commits.append(2), None,
				history_import.Steps(place, Window(0)))
		self.assertEqual(place["phase"], "convert")
		second = Adapters(1)  # the first page is in the ledger
		saved_broken, pages = history_progress.restore(second, place["loader"])
		again = history_import.convert(second, {}, True, lambda: commits.append(2), None,
			history_import.Steps(place, Window(10)), saved_broken, pages)

		self.assertEqual(again, broken)
		self.assertEqual(commits.count(1), 3)
		self.assertEqual(commits.count(2), 3)  # the same pages
		self.assertEqual(second.failures, whole.failures)  # PDR-1 and the broken terms, once each
		self.assertEqual(len(second.warnings), 3)
		self.assertEqual(second.warnings, whole.warnings)
		self.assertEqual(first.calls.count("terms"), 1)
		self.assertNotIn("terms", second.calls)  # broken on the first job's page, not retried
		self.assertEqual(place["phase"], "files")
		self.assertEqual(place["loader"]["pages"], 3)


class TestRunCarriesOn(TestCase):
	"""A job resumes at the phase its run reached and writes the one report."""

	def setUp(self):
		snapshot = {"source": {"capture_id": "c1"}, "schemas": {}}
		source = MagicMock()
		source.__enter__.return_value = source
		self.calls = {}
		for name, item in (
			("config", patch.object(history_import.workspace, "config", return_value={"browser_capture": "c1"})),
			("wait", patch.object(marks, "wait")),
			("window", patch.object(marks, "Window", side_effect=lambda: Window(100))),
			("exists", patch.object(history_import.history_inputs, "exists", return_value=True)),
			("load", patch.object(history_import.history_inputs, "load", return_value=snapshot)),
			("key", patch.object(history_import.history_inputs, "key", return_value="k1")),
			("source", patch.object(history_import, "SnapshotSource", return_value=source)),
			("extract", patch("onerc_vmms_migrate.extract.extract", return_value=snapshot)),
			("capture", patch("onerc_vmms_migrate.continuity.require_capture")),
			("people", patch.object(history_import, "keep_imported_people")),
			("silenced", patch("onerc_vmms_migrate.silence.silenced", MagicMock())),
			("guard", patch("onerc_vmms_migrate.silence.Guard")),
			("loader", patch("onerc_vmms_migrate.history_load.Loader")),
			("report_dir", patch.object(history_import.workspace, "report_dir", return_value="/tmp/unused")),
			("archive", patch.object(history_import, "archive", return_value={"created": 1})),
			("convert", patch.object(history_import, "convert", return_value={})),
			("files", patch.object(history_import, "files", return_value={"available": 1})),
			("reconcile", patch.object(history_import, "reconcile", return_value={})),
			("statuses", patch.object(history_import, "statuses", return_value={})),
			("fields", patch.object(history_import, "fields_not_imported", return_value=[])),
			("drops", patch.object(history_import.helpdesk_load, "field_drops", return_value=[])),
			("write", patch.object(history_import, "_write")),
			("save", patch.object(history_progress, "save")),
			("forget", patch.object(history_progress, "forget")),
			("db", patch.object(frappe, "db", MagicMock())),
		):
			self.calls[name] = item.start()
			self.addCleanup(item.stop)

	def place(self, phase):
		place = history_progress.begin("history-import", "c1", "k1", "20261124-090000-history-import")
		empty = SimpleNamespace(failures=[], warnings=[], excluded=[], validated=set())
		place.update(phase=phase, archive_result={"created": 1}, files={"available": 1},
			loader=history_progress.loader_state(empty, {}, 3))
		place["slices"] = 4
		return place

	def test_a_run_that_reached_the_report_only_writes_it(self):
		with patch.object(history_progress, "load", return_value=self.place("report")):
			result = history_import._run("history-import", True, None)
		for name in ("archive", "convert", "files"):
			self.calls[name].assert_not_called()
		self.calls["write"].assert_called_once()
		report = self.calls["write"].call_args.args[1]
		self.assertEqual(report["archive"], {"created": 1})
		self.assertEqual(report["files"], {"available": 1})
		self.assertEqual(report["jobs"], 5)
		self.calls["forget"].assert_called_once()
		self.assertEqual(result["report"], "unused/history-summary.json")

	def test_a_run_at_the_files_does_not_archive_or_convert_again(self):
		with patch.object(history_progress, "load", return_value=self.place("files")):
			history_import._run("history-import", True, None)
		self.calls["archive"].assert_not_called()
		self.calls["convert"].assert_not_called()
		self.calls["files"].assert_called_once()

	def test_a_run_that_stands_aside_writes_no_report_and_keeps_its_place(self):
		self.calls["convert"].side_effect = history_import.StepAside
		with patch.object(history_progress, "load", return_value=None):
			result = history_import._run("history-import", True, None)
		self.assertTrue(result["stepped_aside"])
		self.assertEqual(result["phase"], "convert")
		self.calls["write"].assert_not_called()
		self.calls["forget"].assert_not_called()
		self.calls["archive"].assert_called_once()

	def test_a_preview_keeps_no_place(self):
		with patch.object(history_progress, "load", return_value=self.place("report")):
			history_import._run("history-preview", False, None)
		self.calls["archive"].assert_called_once()
		self.calls["save"].assert_not_called()
		self.calls["forget"].assert_not_called()


class TestJobs(TestCase):
	def test_a_run_that_stands_aside_is_queued_again_until_it_stops_moving(self):
		value = {}
		for marker in ([1], [2], [3]):
			self.assertTrue(browser._carry_on(value, {"stepped_aside": True, "marker": marker, "phase": "convert"}))
		self.assertEqual((value["jobs"], value["stalls"]), (3, 0))
		self.assertTrue(browser._carry_on(value, {"stepped_aside": True, "marker": [3]}))
		self.assertTrue(browser._carry_on(value, {"stepped_aside": True, "marker": [3]}))
		with self.assertRaises(frappe.ValidationError):
			browser._carry_on(value, {"stepped_aside": True, "marker": [3], "phase": "files"})

	def test_inputs_are_prepared_once_per_request(self):
		value = {}
		self.assertTrue(browser._carry_on(value, {"prepared": True}))
		self.assertFalse(browser._carry_on(value, {"prepared": True}))
		self.assertFalse(browser._carry_on({}, {"report": "x"}))
		self.assertFalse(browser._carry_on({}, None))

	def test_a_job_that_stood_aside_queues_the_next(self):
		with (
			patch.object(browser, "authorize"),
			patch.object(browser, "state", return_value={}),
			patch.object(browser, "save_state"),
			patch.object(browser, "_progress_reporter", return_value=None),
			patch.object(frappe, "set_user"),
			patch.object(browser.frappe, "cache", MagicMock(get=MagicMock(return_value=b"t1"))),
			patch.object(history_import, "apply", return_value={"stepped_aside": True, "marker": [1]}),
			patch.object(browser, "_continue") as again,
		):
			browser.execute("t1", "history_import", "Administrator")
		again.assert_called_once_with("history_import", "Administrator")


class TestVerified(TestCase):
	def test_the_history_store_is_hashed_again_only_when_it_changes(self):
		import hashlib

		with tempfile.TemporaryDirectory() as root:
			path = os.path.join(root, "transfer.sqlite")
			with open(path, "wb") as handle:
				handle.write(b"history")
			digest = hashlib.sha256(b"history").hexdigest()
			with patch.object(bulk_source, "sha256_file", wraps=bulk_source.sha256_file) as hashed:
				self.assertFalse(browser_source.verified(path, "0" * 64))
				self.assertFalse(os.path.exists(path + ".verified"))
				self.assertTrue(browser_source.verified(path, digest))
				self.assertTrue(browser_source.verified(path, digest))
				self.assertEqual(hashed.call_count, 2)
				with open(path, "ab") as handle:
					handle.write(b"!")
				self.assertFalse(browser_source.verified(path, digest))
				self.assertEqual(hashed.call_count, 3)
