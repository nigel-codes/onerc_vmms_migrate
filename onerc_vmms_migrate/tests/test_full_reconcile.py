import hashlib
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate import full_reconcile, historical
from onerc_vmms_migrate.history_model import canonical


class TestNativeTargetReconciliation(TestCase):
	def verify_target(self, target_name, native_names, doctype="Buzz Event"):
		record = {"name": "171"}
		digest = hashlib.sha256(canonical(record).encode()).hexdigest()
		archived = frappe._dict(
			record_id=historical.archive_id("source", doctype, record["name"]),
			source_doctype=doctype,
			source_name=record["name"],
			source_hash=digest,
			payload_hash=digest,
			conversion_status="Converted",
			target_doctype=doctype,
			target_name=target_name,
		)
		snapshot = {
			"source": {"source_id": "source"},
			"users": {},
			"history": {"event": {"doctype": doctype, "record": record}},
		}
		with (
			patch.object(frappe, "db", MagicMock()) as db,
			patch.object(frappe, "get_all", return_value=native_names),
			patch("onerc_vmms_migrate.full_run.coverage", return_value={}),
		):
			db.sql.side_effect = [[archived], []]
			db.exists.return_value = True
			db.count.return_value = 0
			return full_reconcile.verify(snapshot)

	def test_numeric_native_ids_match_text_archive_links(self):
		for doctype in ("Buzz Event", "Event Ticket Type"):
			with self.subTest(doctype=doctype):
				summary, failures = self.verify_target("171", [171], doctype)
				self.assertEqual(failures, [])
				self.assertEqual(summary["mismatches"], 0)

	def test_genuinely_missing_native_record_still_fails(self):
		summary, failures = self.verify_target("171", [172])
		self.assertEqual(summary["mismatches"], 1)
		self.assertEqual(failures[0]["problem"], "Native target is missing")

	def test_text_identifiers_are_not_coerced_to_integers(self):
		_, failures = self.verify_target("00171", [171])
		self.assertEqual(len(failures), 1)
		_, failures = self.verify_target("00171", ["00171"])
		self.assertFalse(failures)
