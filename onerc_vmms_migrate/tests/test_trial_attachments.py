import inspect
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate import trial_attachments as files


class TestTrialAttachments(TestCase):
	def test_only_successful_test_and_same_capture_qualify(self):
		proof = {
			"mode": "test",
			"status": "finished",
			"passed": True,
			"session_id": "session",
			"capture_id": "capture",
		}
		with patch.object(files.batches, "checkpoint", return_value=proof):
			self.assertEqual(files.qualified({"browser_capture": "capture"}), proof)
			with self.assertRaises(ValueError):
				files.qualified({"browser_capture": "different"})
			proof["mode"] = "import"
			with self.assertRaises(ValueError):
				files.qualified({"browser_capture": "capture"})

	def test_attachment_link_never_overwrites_a_different_file(self):
		link = {
			"doctype": "Red Profile",
			"name": "P1",
			"field": "profile_photo",
			"original": "/files/source.jpg",
			"parent": None,
			"kind": "Attach Image",
		}
		doc = frappe._dict(doctype="Red Profile", name="P1", profile_photo="/private/files/someone-else.jpg")
		meta = MagicMock()
		meta.get_field.return_value = frappe._dict(fieldtype="Attach Image")
		with (
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(frappe, "get_meta", return_value=meta),
			patch.object(frappe, "db", MagicMock()) as db,
		):
			with self.assertRaisesRegex(ValueError, "changed"):
				files.apply_binding(link, "/private/files/new.jpg")
			db.set_value.assert_not_called()

	def test_child_attachment_must_still_belong_to_the_verified_parent(self):
		link = {
			"doctype": "Identification",
			"name": "child",
			"field": "attachment",
			"original": "/private/files/source.pdf",
			"kind": "Attach",
			"parent": {"doctype": "Red Profile", "name": "P1", "field": "identifications"},
		}
		doc = frappe._dict(parent="P2", parenttype="Red Profile", parentfield="identifications")
		with patch.object(frappe, "get_doc", return_value=doc), patch.object(frappe, "db", MagicMock()) as db:
			with self.assertRaisesRegex(ValueError, "parent"):
				files.apply_binding(link, "/private/files/new.pdf")
			db.set_value.assert_not_called()

	def test_checkpoint_uses_independent_field_not_completed_batch(self):
		with (
			patch.object(frappe, "db", MagicMock()) as db,
			patch.object(frappe.utils, "now_datetime", return_value="now"),
		):
			files.commit({"status": "review", "plan": {"tasks": []}})
			self.assertEqual(db.set_single_value.call_args.args[1], "attachment_checkpoint")

	def test_unapproved_plan_cannot_be_resumed_as_a_copy(self):
		with (
			patch.object(files.browser, "authorize"),
			patch.object(files, "state", return_value={"status": "review"}),
		):
			with self.assertRaisesRegex(ValueError, "approved"):
				inspect.unwrap(files.resume)()

	def test_existing_verified_file_is_reused_for_the_same_parent(self):
		import hashlib
		import tempfile
		from pathlib import Path

		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "source.pdf"
			path.write_bytes(b"private attachment")
			digest = hashlib.sha256(path.read_bytes()).hexdigest()
			url = "/private/files/vmms-" + digest + ".pdf"
			task = {
				"url": "/files/source.pdf",
				"doctype": "Red Profile",
				"name": "P1",
				"sha256": digest,
				"owner_chain": ["verified owner"],
				"bindings": [],
				"file_records": [],
			}
			source = MagicMock()
			source.data = {"all_file_manifest": {task["url"]: {"status": "ready", "sha256": digest}}}
			source.file_path.return_value = str(path)
			file = frappe._dict(name="FILE-1", file_url=url)
			doc = MagicMock()
			doc.get_full_path.return_value = str(path)
			with (
				patch.object(frappe, "db", MagicMock()) as db,
				patch.object(frappe, "get_all", return_value=[file]),
				patch.object(frappe, "get_doc", return_value=doc),
				patch.object(files.attachment_plan, "owner_chain", return_value=["verified owner"]),
				patch.object(files, "copy_verified") as copy,
			):
				db.exists.return_value = True
				db.get_value.return_value = file
				for _ in range(2):
					self.assertEqual(files.copy_task(task, source, "run")["file"], "FILE-1")
				copy.assert_not_called()
				self.assertEqual(db.get_value.call_args.args[1]["attached_to_name"], "P1")
				self.assertEqual(db.get_value.call_args.args[1]["is_private"], 1)

	def test_owner_change_stops_before_reading_or_copying_bytes(self):
		task = {"doctype": "Red Profile", "name": "P1", "owner_chain": ["old owner"]}
		source = MagicMock()
		with (
			patch.object(frappe, "db", MagicMock()) as db,
			patch.object(files.attachment_plan, "owner_chain", return_value=["different owner"]),
			patch.object(files, "copy_verified") as copy,
		):
			db.exists.return_value = True
			with self.assertRaisesRegex(ValueError, "owner links changed"):
				files.copy_task(task, source, "run")
			copy.assert_not_called()
			source.file_path.assert_not_called()

	def test_failed_copy_does_not_advance_or_commit_attachment_cursor(self):
		from contextlib import nullcontext

		value = {
			"offset": 0,
			"total": 2,
			"batch_size": 100,
			"run_id": "run",
			"batch_number": 0,
		}
		with (
			patch.object(files, "Guard"),
			patch.object(files, "silenced", return_value=nullcontext()),
			patch.object(files, "read_tasks", return_value=[{}, {}]),
			patch.object(files, "copy_task", side_effect=[{}, ValueError("failed")]),
			patch.object(files, "commit") as commit,
			patch.object(frappe, "cache", MagicMock(get=MagicMock(return_value=False))),
		):
			with self.assertRaisesRegex(ValueError, "failed"):
				files.step.__wrapped__(value, None)
			self.assertEqual(value["offset"], 0)
			commit.assert_not_called()

	def test_successful_copy_commits_its_own_cursor_and_keeps_access_checks_pending(self):
		from contextlib import nullcontext

		value = {
			"offset": 0,
			"total": 1,
			"batch_size": 100,
			"run_id": "run",
			"batch_number": 0,
			"summary": {},
		}
		with (
			patch.object(files, "Guard"),
			patch.object(files, "silenced", return_value=nullcontext()),
			patch.object(files, "read_tasks", return_value=[{}]),
			patch.object(files, "copy_task", return_value={}),
			patch.object(files, "commit") as commit,
			patch.object(files.workspace, "report_dir", return_value="/tmp"),
			patch.object(files.workspace, "write_json"),
			patch.object(files.batches, "commit_checkpoint") as test_commit,
		):
			files.step.__wrapped__(value, None)
			self.assertEqual(value["status"], "copied")
			self.assertEqual(value["offset"], 1)
			self.assertTrue(value["summary"]["access_checks_pending"])
			commit.assert_called_once_with(value)
			test_commit.assert_not_called()

	def test_copy_start_needs_explicit_review_and_backup(self):
		from contextlib import nullcontext

		value = {"status": "review", "plan_digest": "digest"}
		with (
			patch.object(files.browser, "authorize"),
			patch.object(files, "control_lock", return_value=nullcontext("token")),
			patch.object(files, "state", return_value=value),
			patch.object(files, "commit") as commit,
		):
			with self.assertRaisesRegex(ValueError, "Confirm COPY FILES"):
				inspect.unwrap(files.start)(
					"digest", "COPY FILES", backup_confirmed=True, test_destination=True
				)
			commit.assert_not_called()

	def test_plan_tampering_or_input_change_stops_copy(self):
		value = {"plan_digest": "plan", "total": 0, "review_summary": {}, "review_digest": "incorrect"}
		with self.assertRaisesRegex(ValueError, "reviewed digest"):
			files.ensure_current(value)
		value.update(
			review_digest=files.review_digest(value),
			source_test_session="session",
			fingerprint="old",
		)
		with (
			patch.object(files, "qualified", return_value={"session_id": "session"}),
			patch.object(files.workspace, "config", return_value={}),
			patch.object(files.browser, "fingerprint", return_value="changed"),
		):
			with self.assertRaisesRegex(ValueError, "changed"):
				files.ensure_current(value)

	def test_reviewed_code_update_can_resume_but_input_changes_cannot(self):
		value = {
			"plan_digest": "plan",
			"total": 0,
			"review_summary": {},
			"source_test_session": "session",
			"fingerprint": "old-code",
			"inputs_fingerprint": "same-inputs",
		}
		value["review_digest"] = files.review_digest(value)
		with (
			patch.object(files, "qualified", return_value={"session_id": "session"}),
			patch.object(files.workspace, "config", return_value={}),
			patch.object(
				files.browser,
				"fingerprint",
				side_effect=lambda include_code=True: "new-code" if include_code else "same-inputs",
			),
		):
			with self.assertRaisesRegex(ValueError, "Code changed"):
				files.ensure_current(value)
			files.ensure_current(value, True)
			self.assertEqual(value["fingerprint"], "new-code")
			self.assertEqual(value["versions_reviewed"], ["old-code"])
		value["inputs_fingerprint"] = "changed-inputs"
		with (
			patch.object(files, "qualified", return_value={"session_id": "session"}),
			patch.object(files.workspace, "config", return_value={}),
			patch.object(files.browser, "fingerprint", return_value="new-code"),
		):
			with self.assertRaisesRegex(ValueError, "Capture, mappings, settings"):
				files.ensure_current(value, True)

	def test_task_pages_are_bounded_and_tampering_is_rejected(self):
		import json

		task = {"url": "/private/files/source.pdf", "doctype": "Red Profile", "name": "P1"}
		value = {"session_id": "plan", "offset": 2, "total": 4, "batch_size": 1}
		row = frappe._dict(
			name="plan:00000002:" + files.attachment_plan.digest(task),
			task_index=2,
			task_payload=json.dumps(task),
		)
		with patch.object(frappe, "get_all", return_value=[row]) as get_all:
			self.assertEqual(files.read_tasks(value), [task])
			self.assertEqual(get_all.call_args.kwargs["limit_page_length"], 1)
			self.assertEqual(get_all.call_args.kwargs["order_by"], "name asc")
			row.task_payload = json.dumps({**task, "name": "P2"})
			with self.assertRaisesRegex(ValueError, "reviewed identity"):
				files.read_tasks(value)

	def test_missing_task_page_does_not_skip_ahead(self):
		with patch.object(frappe, "get_all", return_value=[]):
			with self.assertRaisesRegex(ValueError, "tasks are missing"):
				files.read_tasks({"session_id": "plan", "offset": 0, "total": 1, "batch_size": 100})
