from unittest import TestCase

from onerc_vmms_migrate.attachment_plan import Plan


class TestAttachmentPlan(TestCase):
	def plan(self):
		return Plan({"/private/files/cv.pdf": {"status": "ready", "sha256": "a" * 64}})

	def test_different_people_sharing_a_url_are_held(self):
		plan = self.plan()
		plan.claim("/private/files/cv.pdf", "person-one")
		plan.claim("/private/files/cv.pdf", "person-two")
		plan.add("/private/files/cv.pdf", "Red Profile", "PROFILE-1")
		result = plan.finish()
		self.assertFalse(result["tasks"])
		self.assertIn("Multiple personal identities", result["held"][0]["reason"])

	def test_same_person_in_multiple_source_forms_is_not_a_conflict(self):
		plan = self.plan()
		plan.claim("/private/files/cv.pdf", "person-one")
		plan.claim("/private/files/cv.pdf", "person-one")
		plan.add("/private/files/cv.pdf", "Red Profile", "PROFILE-1")
		plan.add("/private/files/cv.pdf", "Red Profile", "PROFILE-1")
		result = plan.finish()
		self.assertEqual(len(result["tasks"]), 1)
		self.assertFalse(result["held"])
		self.assertTrue(result["tasks"][0]["private"])

	def test_shared_bytes_under_different_urls_are_also_held(self):
		plan = self.plan()
		plan.manifest["/files/other.pdf"] = {"status": "ready", "sha256": "a" * 64}
		plan.claim("/private/files/cv.pdf", "person-one")
		plan.claim("/files/other.pdf", "person-two")
		plan.add("/private/files/cv.pdf", "Red Profile", "PROFILE-1")
		self.assertFalse(plan.finish()["tasks"])

	def test_unresolved_personal_owner_and_missing_bytes_are_not_copied(self):
		plan = self.plan()
		plan.claim("/private/files/cv.pdf", None)
		plan.add("/private/files/cv.pdf", "Red Profile", "PROFILE-1")
		plan.add("/files/missing.pdf", "Red Profile", "PROFILE-1")
		result = plan.finish()
		self.assertFalse(result["tasks"])
		self.assertEqual(len(result["held"]), 2)

	def test_profile_child_binding_checks_identity_before_using_its_row_number(self):
		import frappe

		from onerc_vmms_migrate.attachment_plan import profile_binding

		bundle = {"profile": {"identifications": [{"id_type": "National ID", "id_number": "expected"}]}}
		item = {"purpose": "identification", "index": 0, "url": "/private/files/id.pdf"}
		child = frappe._dict(
			doctype="Identification", name="child", id_type="National ID", id_number="different"
		)
		profile = frappe._dict(name="P1", identifications=[child])
		with self.assertRaisesRegex(ValueError, "differs"):
			profile_binding(bundle, item, profile)
		child.id_number = "expected"
		link = profile_binding(bundle, item, profile)
		self.assertEqual(link["name"], "child")
		self.assertEqual(link["parent"]["name"], "P1")
		item["index"] = -1
		with self.assertRaises(ValueError):
			profile_binding(bundle, item, profile)

	def test_two_different_files_for_one_native_slot_are_held_for_review(self):
		plan = self.plan()
		plan.manifest["/files/second.pdf"] = {"status": "ready", "sha256": "b" * 64}
		link = {"doctype": "Identification", "name": "child", "field": "attachment", "kind": "Attach"}
		plan.add("/private/files/cv.pdf", "Red Profile", "P1", link)
		plan.add("/files/second.pdf", "Red Profile", "P1", link)
		self.assertFalse(plan.finish()["tasks"])

	def test_plan_resolves_transformed_profile_slot_and_same_user_file_reference(self):
		from unittest.mock import MagicMock, patch

		import frappe

		from onerc_vmms_migrate import attachment_plan, historical, load

		url = "/private/files/id.pdf"
		person = "person@example.test"
		bundle = {
			"key": person,
			"errors": [],
			"source_hash": "source-hash",
			"sources": [("User", person)],
			"profile": {"email": person, "identifications": [{"id_type": "ID", "id_number": "123"}]},
			"files": [{"url": url, "purpose": "identification", "index": 0}],
		}
		child = frappe._dict(doctype="Identification", name="child", id_type="ID", id_number="123")
		profile = frappe._dict(
			name="P1", email=person, user=person, owner="Administrator", identifications=[child]
		)
		file = {
			"name": "source-file",
			"file_url": url,
			"attached_to_doctype": "User",
			"attached_to_name": person,
		}
		snapshot = {
			"source": {"source_id": "source", "capture_id": "capture"},
			"users": {person: {"name": person}},
			"schemas": {},
			"history": {"file": {"doctype": "File", "record": file}},
			"all_file_manifest": {url: {"status": "ready", "sha256": "a" * 64}},
		}
		archives = [
			frappe._dict(
				name="archive-user",
				source_doctype="User",
				source_name=person,
				conversion_status="Converted",
				target_doctype="User",
				target_name=person,
			),
			frappe._dict(
				name="archive-file",
				source_doctype="File",
				source_name="source-file",
				conversion_status="Retained",
				target_doctype=None,
				target_name=None,
			),
		]

		def rows(doctype, **kwargs):
			if doctype == historical.ARCHIVE:
				return archives
			self.assertEqual(doctype, load.LEDGER)
			return [frappe._dict(person_key=person, source_hash="source-hash", target_name="P1")]

		with (
			patch.object(attachment_plan.batches, "build", return_value={"bundles": [bundle]}),
			patch.object(frappe, "get_all", side_effect=rows),
			patch.object(frappe, "get_doc", return_value=profile),
			patch.object(frappe, "get_meta", return_value=MagicMock()),
			patch.object(frappe, "db", MagicMock()) as db,
		):
			db.get_value.return_value = frappe._dict(owner="Administrator")
			plan = attachment_plan.build(snapshot, {})
			self.assertFalse(plan["held"])
			self.assertEqual(len(plan["tasks"]), 2)
			task = next(task for task in plan["tasks"] if task["doctype"] == "Red Profile")
			self.assertEqual(task["name"], "P1")
			self.assertEqual(task["bindings"][0]["name"], "child")
			db.set_value.assert_not_called()
			db.commit.assert_not_called()
