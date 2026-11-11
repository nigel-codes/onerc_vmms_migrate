import sqlite3
import re
from unittest import TestCase
from unittest.mock import MagicMock, patch
import frappe
from frappe.query_builder.builder import MariaDB
from onerc_vmms_migrate import membership_import, silence
from vmmsx.finance.services import membership_posting


class TestPeopleIsolation(TestCase):
	def test_actual_product_outstanding_query_excludes_historical_rows(self):
		with sqlite3.connect(":memory:") as db:
			db.execute(
				"create table `tabVMMS Membership` (name text,paid_on text,amount real,accounting_status text,membership_source text)"
			)
			for name, accounting in [
				("old-paid", membership_import.values({"source_name": "old-paid"})["accounting_status"]),
				("new-unposted", None),
				("new-failed", "Failed"),
			]:
				db.execute(
					"insert into `tabVMMS Membership` values (?,?,?,?,?)",
					(name, "2026-01-01", 100, accounting, None),
				)

			def sql(query, values=None, **kwargs):
				query = re.sub(r"%\((\w+)\)s", r":\1", str(query))
				return db.execute(query, values or {}).fetchall()

			database = MagicMock()
			database.get_single_value.return_value = None
			database.sql.side_effect = sql

			def run(query):
				return db.execute(query.get_sql()).fetchall()

			with (
				patch.object(frappe, "db", database),
				patch.object(frappe, "qb", MariaDB),
				patch("frappe.query_builder.builder.RecursiveMySQLQueryBuilder.run", run),
			):
				self.assertEqual(membership_posting.outstanding(), ["new-unposted", "new-failed"])

	def test_resolved_jobs_and_manual_posting_are_guarded_then_restored(self):
		from vmmsx.approvals.services import repair, engine

		# These references are resolved before Frappe invokes before_job.
		queued = repair.resync_pending
		original = repair._repair_one
		with (
			patch.object(repair.config, "governed_doctypes", return_value=["VMMS Membership"]),
			patch.object(repair, "_pending", return_value=["M1"]),
			patch.object(frappe, "db", MagicMock()),
			patch.object(membership_posting, "is_enabled", return_value=True),
			patch.object(frappe, "flags", frappe._dict()),
		):
			with silence.runtime_isolation(True):
				self.assertEqual(queued(), {})
				self.assertFalse(repair._repair_one("VMMS Membership", "M1"))
				self.assertEqual(
					membership_posting.why_not(
						{"legacy_membership_id": "M1", "accounting_status": "Historical"}
					),
					"Historical fee retained from the source; do not post again.",
				)
			self.assertIs(repair._repair_one, original)
			with silence.runtime_isolation(False):
				self.assertIn(
					"Historical fee",
					membership_posting.why_not(
						{"legacy_membership_id": "M1", "accounting_status": "Historical"}
					),
				)

	def test_known_notification_and_routing_enqueues_do_not_grow_queue(self):
		native = MagicMock()
		with patch.object(frappe, "enqueue", native):
			with silence.synchronous_contacts():
				frappe.enqueue("vmmsx.approvals.services.repair.resync_pending", enqueue_after_commit=True)
				frappe.enqueue("vmmsx.notifications.services.sms.tell", queue="short")
		native.assert_not_called()
