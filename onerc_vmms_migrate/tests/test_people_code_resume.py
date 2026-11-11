"""A reviewed receiver hotfix must preserve a committed people cursor and inputs."""

from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from onerc_vmms_migrate import batch_browser, batches, browser, workspace


class TestReviewedPeopleCodeResume(TestCase):
	def setUp(self):
		self.enterContext(patch.object(frappe, "session", frappe._dict(user="Administrator")))
		self.enterContext(patch.object(frappe, "cache", MagicMock()))
		self.enterContext(patch.object(frappe.utils, "now_datetime", return_value="2026-11-11 12:00:00"))

		def throw(message, *args, **kwargs):
			raise frappe.ValidationError(message)

		self.enterContext(patch.object(frappe, "throw", side_effect=throw))
		self.enterContext(patch.object(workspace, "config", return_value={"browser_capture": "capture"}))
		self.enterContext(
			patch.object(
				browser,
				"fingerprint",
				side_effect=lambda include_code=True: "new-code" if include_code else "same-inputs",
			)
		)

	def value(self, **updates):
		return {
			"scope": "people",
			"mode": "import",
			"phase": "people",
			"status": "blocked",
			"capture_id": "capture",
			"fingerprint": "old-code",
			"inputs_fingerprint": "same-inputs",
			"offset": 0,
			"people_offset": 0,
			"session_id": "original-session",
			"run_id": "original-run",
			"batch_number": 2,
			**updates,
		}

	def test_code_only_update_keeps_the_same_session_and_cursor_after_explicit_review(self):
		value = self.value()
		with (
			patch.object(batches, "checkpoint", return_value=value),
			patch.object(batches, "commit_checkpoint"),
			patch.object(batch_browser, "queue") as queue,
		):
			batch_browser.resume_locked(50, True, "lock", auto_continue=False)
		self.assertEqual(value["fingerprint"], "new-code")
		self.assertEqual(value["inputs_fingerprint"], "same-inputs")
		self.assertEqual(
			(value["session_id"], value["people_offset"], value["offset"], value["batch_number"]),
			("original-session", 0, 0, 2),
		)
		self.assertEqual(value["versions_reviewed"], ["old-code"])
		self.assertFalse(value["auto_continue"])
		self.assertEqual(queue.call_args.args[0], "people_import")

	def test_code_change_without_review_or_changed_inputs_cannot_queue_or_commit(self):
		for accept, updates in (
			(False, {}),
			(True, {"inputs_fingerprint": "different-inputs"}),
			(True, {"capture_id": "other-capture"}),
		):
			with self.subTest(accept=accept, updates=updates):
				value = self.value(**updates)
				with (
					patch.object(batches, "checkpoint", return_value=value),
					patch.object(batches, "commit_checkpoint") as commit,
					patch.object(batch_browser, "queue") as queue,
				):
					with self.assertRaises(frappe.ValidationError):
						batch_browser.resume_locked(50, accept, "lock")
				commit.assert_not_called()
				queue.assert_not_called()
				self.assertEqual(value["fingerprint"], "old-code")

	def test_full_import_mode_keeps_its_existing_unchanged_input_gate(self):
		value = self.value(scope="full")
		with (
			patch.object(batches, "checkpoint", return_value=value),
			patch.object(batches, "commit_checkpoint") as commit,
		):
			with self.assertRaises(frappe.ValidationError):
				batch_browser.resume_locked(50, True, "lock")
		commit.assert_not_called()


class TestLegacyCodeBaseline(TestCase):
	def test_legacy_full_digest_proves_inputs_without_executing_old_code(self):
		import gzip, hashlib
		from onerc_vmms_migrate import code_resume

		raw = gzip.decompress(code_resume.BASELINE.read_bytes())
		previous = hashlib.sha256(b"original inputs" + raw).hexdigest()
		with patch.object(browser, "_input_digest", side_effect=lambda: hashlib.sha256(b"original inputs")):
			self.assertTrue(code_resume.legacy_inputs_match(previous))
		with patch.object(browser, "_input_digest", side_effect=lambda: hashlib.sha256(b"changed mapping")):
			self.assertFalse(code_resume.legacy_inputs_match(previous))

	def test_tampered_or_missing_baseline_is_never_accepted(self):
		import gzip
		from onerc_vmms_migrate import code_resume

		artifact = MagicMock()
		artifact.read_bytes.return_value = gzip.compress(b"not the released code")
		with patch.object(code_resume, "BASELINE", artifact):
			with self.assertRaisesRegex(ValueError, "integrity"):
				code_resume.legacy_inputs_match("a" * 64)
		artifact.read_bytes.side_effect = FileNotFoundError("baseline absent")
		with patch.object(code_resume, "BASELINE", artifact):
			with self.assertRaisesRegex(ValueError, "baseline is unavailable"):
				code_resume.legacy_inputs_match("a" * 64)

	def test_real_legacy_match_can_upgrade_without_changing_the_original_cursor(self):
		import gzip, hashlib
		from onerc_vmms_migrate import code_resume

		raw = gzip.decompress(code_resume.BASELINE.read_bytes())
		value = {
			"scope": "people",
			"mode": "import",
			"phase": "people",
			"status": "blocked",
			"capture_id": "capture",
			"fingerprint": hashlib.sha256(b"original inputs" + raw).hexdigest(),
			"session_id": "original",
			"offset": 0,
			"people_offset": 0,
			"batch_number": 2,
		}
		with (
			patch.object(
				browser,
				"fingerprint",
				side_effect=lambda include_code=True: (
					"new release" if include_code else hashlib.sha256(b"original inputs").hexdigest()
				),
			),
			patch.object(browser, "_input_digest", side_effect=lambda: hashlib.sha256(b"original inputs")),
			patch.object(workspace, "config", return_value={"browser_capture": "capture"}),
			patch.object(
				frappe,
				"throw",
				side_effect=lambda message: (_ for _ in ()).throw(frappe.ValidationError(message)),
			),
			patch.object(frappe, "session", frappe._dict(user="Administrator")),
			patch.object(frappe.utils, "now_datetime", return_value="2026-11-11"),
			patch.object(frappe, "cache", MagicMock()),
			patch.object(batches, "checkpoint", return_value=value),
			patch.object(batches, "commit_checkpoint"),
			patch.object(batch_browser, "queue"),
		):
			batch_browser.resume_locked(50, True, "lock")
		self.assertEqual(value["session_id"], "original")
		self.assertEqual(value["offset"], 0)
		self.assertIn(code_resume.LEGACY_COMMIT, value["code_upgrades"][0]["proof"])
		self.assertEqual(value["code_upgrades"][0]["reviewed_by"], "Administrator")

	def test_unverified_legacy_inputs_do_not_change_or_replace_the_checkpoint(self):
		from copy import deepcopy
		from onerc_vmms_migrate import code_resume

		value = {"scope": "people", "mode": "import", "capture_id": "capture", "fingerprint": "a" * 64}
		original = deepcopy(value)
		with (
			patch.object(workspace, "config", return_value={"browser_capture": "capture"}),
			patch.object(browser, "fingerprint", return_value="new inputs"),
			patch.object(code_resume, "legacy_inputs_match", return_value=False),
		):
			with self.assertRaisesRegex(ValueError, "could not be verified"):
				code_resume.approve_people_code_update(value)
		self.assertEqual(value, original)


class TestRetryPreservesCommittedPeople(TestCase):
	def test_the_same_fifty_retry_only_four_missing_members_and_then_all_fifty_are_skipped(self):
		from onerc_vmms_migrate import people_run, load
		from onerc_vmms_migrate.tests.test_membership_source import imported
		from frappe.model import base_document
		from onerc_vmms_migrate.membership_import import values

		bundles = [
			{
				"key": f"person{i}@example.test",
				"source_hash": "original",
				"errors": [],
				"sources": [],
				"memberships": [],
			}
			for i in range(50)
		]
		ledger = {bundle["key"]: {"source_hash": "original"} for bundle in bundles[:46]}

		def write(bundle, *args, **kwargs):
			membership = imported(**values({"source_name": "VM-MSH-2026-00001"}))
			membership._validate()
			ledger[bundle["key"]] = {"source_hash": bundle["source_hash"]}
			return {"phone_review": []}

		with (
			patch.object(frappe, "flags", frappe._dict(vmms_migration_import=True)),
			patch.object(base_document, "_", side_effect=lambda text, **kwargs: text),
			patch.object(frappe, "db", MagicMock()) as database,
			patch.object(load, "already_migrated", side_effect=lambda key: ledger.get(key)),
			patch.object(load, "load_bundle", side_effect=write) as loader,
			patch.object(people_run, "core_records", return_value=[]),
			patch.object(people_run.historical, "archive"),
			patch.object(people_run.batches, "mark_person"),
		):
			report = people_run.import_people({}, None, {}, {"run_id": "original-run"}, bundles)
			self.assertEqual((report["loaded"], report["existing"], report["failures"]), (4, 46, []))
			self.assertEqual(loader.call_count, 4)
			again = people_run.import_people({}, None, {}, {"run_id": "original-run"}, bundles)
			self.assertEqual((again["loaded"], again["existing"]), (0, 50))
			self.assertEqual(loader.call_count, 4)
			database.rollback.assert_not_called()


class TestLegacyInputsIncludeMappingsAndAliases(TestCase):
	def test_actual_input_digest_checks_configuration_crosswalks_and_placeholder_registry(self):
		import gzip
		import hashlib
		import json
		import tempfile
		from pathlib import Path
		from onerc_vmms_migrate import code_resume, crosswalk

		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			mappings = root / "crosswalks"
			mappings.mkdir()
			branch = mappings / "branch.csv"
			branch.write_text("source_value,target\nBranch,GEO-1\n")
			residence = mappings / "residence.csv"
			residence.write_text("source_value,target\ncounty:County,GEO-2\n")
			registry = root / "placeholder-emails.json"
			registry.write_text('{"assignments": [{"email":"missingemail1@vmms.com"}]}')
			config = {
				"browser_capture": "capture",
				"import_with_review": True,
				"history_branch_overrides": {},
			}
			expected = hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode())
			expected.update(b"placeholder-emails\0" + registry.read_bytes())
			for kind in sorted(crosswalk.KINDS):
				path = mappings / (kind + ".csv")
				if path.is_file():
					expected.update(path.read_bytes())
			expected_inputs = expected.hexdigest()
			expected.update(gzip.decompress(code_resume.BASELINE.read_bytes()))
			previous = expected.hexdigest()
			with (
				patch.object(workspace, "config", side_effect=lambda: dict(config)),
				patch.object(workspace, "crosswalk_dir", return_value=str(mappings)),
			):
				self.assertEqual(browser.fingerprint(False), expected_inputs)
				self.assertTrue(code_resume.legacy_inputs_match(previous))
				original_branch = branch.read_text()
				branch.write_text(original_branch.replace("GEO-1", "GEO-3"))
				self.assertFalse(code_resume.legacy_inputs_match(previous))
				branch.write_text(original_branch)
				original_aliases = registry.read_text()
				registry.write_text("{}")
				self.assertFalse(code_resume.legacy_inputs_match(previous))
				registry.write_text(original_aliases)
				config["import_with_review"] = False
				self.assertFalse(code_resume.legacy_inputs_match(previous))
