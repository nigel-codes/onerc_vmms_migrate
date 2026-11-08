"""Reference preparation must agree with the native person mapping contract."""

from contextlib import nullcontext
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate import reference, trees
from onerc_vmms_migrate.crosswalk import Crosswalks
from onerc_vmms_migrate.tests.test_transform import snapshot


class TestNativeReferencePreparation(TestCase):
	def test_employee_profession_is_prepared_without_recreating_legacy_questions(self):
		cw = Crosswalks()
		db = MagicMock()
		db.exists.return_value = None
		snap = snapshot(employees={"E": {"name": "E", "custom_profession": "Recorded profession"}})
		with (
			patch.object(Crosswalks, "read", return_value=cw),
			patch.object(Crosswalks, "write"),
			patch.object(frappe, "db", db),
			patch.object(frappe, "get_doc") as create,
			patch.object(reference, "ensure", side_effect=lambda dt, label, value, *args, **kwargs: value),
			patch.object(reference, "geography"),
			patch.object(trees, "batched_trees", return_value=nullcontext()),
		):
			reference.prepare(snap)
		self.assertEqual(cw.target("profession", "recorded profession"), "Recorded profession")
		self.assertFalse(cw.entries.get("question"))
		create.assert_not_called()
