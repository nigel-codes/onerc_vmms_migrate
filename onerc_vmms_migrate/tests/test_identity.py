from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch
import frappe


class TestOriginalRecordIdentity(TestCase):
    def test_new_record_uses_the_exact_source_name_without_consuming_a_series(self):
        from onerc_vmms_migrate.identity import create
        doc = MagicMock()
        with patch.object(frappe, 'db', SimpleNamespace(exists=lambda *a: False)), patch.object(frappe, 'get_doc', return_value=doc):
            create('VMMS Volunteer', 'profile', ['VOL-26-031709'], {'home_geo_node': 'county'})
        doc.insert.assert_called_once_with(ignore_permissions=True, set_name='VOL-26-031709')

    def test_multiple_old_member_numbers_are_not_silently_coalesced(self):
        from onerc_vmms_migrate.identity import create
        with self.assertRaisesRegex(ValueError, 'one original'):
            create('VMMS Member', 'profile', ['VM-MEM-1', 'VM-MEM-2'], {})

    def test_occupied_source_number_is_a_conflict(self):
        from onerc_vmms_migrate.identity import create
        with patch.object(frappe, 'db', SimpleNamespace(exists=lambda *a: True)):
            with self.assertRaisesRegex(ValueError, 'already exists'):
                create('VMMS Volunteer', 'profile', ['VOL-26-031709'], {})

    def test_an_adopted_target_cannot_change_the_public_number(self):
        from onerc_vmms_migrate.identity import assert_same_name
        with self.assertRaisesRegex(ValueError, 'original identifier'):
            assert_same_name('VOL-00001', ['VOL-26-031709'])
