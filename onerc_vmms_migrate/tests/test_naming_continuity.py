from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe


class TestNamingContinuity(TestCase):
    def test_keeps_deleted_source_numbers_and_other_years_in_the_same_series(self):
        from onerc_vmms_migrate.naming_continuity import NamingContinuity

        naming = NamingContinuity({
            "source": {"continuity": {"naming": {"series": [
                {"name": "TASK-2025-", "current": 18},
                {"name": "TASK-2026-", "current": 56},
                {"name": "PROJ-", "current": 106},
            ]}}}
        })
        meta = SimpleNamespace(autoname="TASK-.YYYY.-.#####")
        with patch.object(frappe, "get_meta", return_value=meta), patch.object(frappe, "get_all", return_value=[]), patch.object(frappe, "db", MagicMock()) as db:
            naming.reserve("Task", "Task", ["TASK-2026-00042"])
        self.assertEqual({call.args[1] for call in db.sql.call_args_list}, {("TASK-2025-", 18), ("TASK-2026-", 56)})
        self.assertTrue(all("GREATEST" in call.args[0] for call in db.sql.call_args_list))

    def test_numeric_sequence_uses_preserved_id_and_avoids_rollback_side_effect(self):
        from onerc_vmms_migrate.naming_continuity import NamingContinuity

        meta = SimpleNamespace(autoname="autoincrement")
        with patch.object(frappe, "get_meta", return_value=meta), patch.object(frappe, "db", MagicMock(db_type="mariadb")) as db:
            NamingContinuity({}, dry_run=True).reserve("Buzz Event", "Buzz Event", [123])
            db.set_next_sequence_val.assert_not_called()
            NamingContinuity({}).reserve("Buzz Event", "Buzz Event", [123, 122])
            db.set_next_sequence_val.assert_called_once_with("Buzz Event", 123, is_val_used=True)

    def test_matching_destination_rule_uses_source_counter_and_keeps_other_rules_out(self):
        from onerc_vmms_migrate.naming_continuity import NamingContinuity

        source_rules = [
            {"document_type": "Employee", "prefix": "VOL.-.YY.-", "prefix_digits": 6, "counter": 31709},
            {"document_type": "Other", "prefix": "VOL.-.YY.-", "prefix_digits": 6, "counter": 99999},
        ]
        target_rules = [{"name": "volunteer rule", "prefix": "VOL-.YY.-", "prefix_digits": 6}]
        naming = NamingContinuity({"source": {"continuity": {"naming": {"rules": source_rules}}}})
        with patch.object(frappe, "get_meta", return_value=SimpleNamespace(autoname="hash")), patch.object(
            frappe, "get_all", return_value=target_rules
        ), patch.object(frappe, "db", MagicMock()) as db:
            naming.reserve("Employee", "VMMS Volunteer", ["VOL-26-031702"])
        self.assertEqual(db.sql.call_args.args[1], (31709, "volunteer rule"))
