from unittest import TestCase
from onerc_vmms_migrate.continuity import require_capture


class TestFreshCaptureRequirements(TestCase):
    def test_old_captures_remain_readable_but_cannot_pass_new_import_preflight(self):
        with self.assertRaisesRegex(ValueError, 'fresh continuity capture'):
            require_capture({'source': {'source_id': 'old'}, 'schemas': {'Employee': {}}})

    def test_present_source_payments_or_audit_tables_must_have_export_schemas(self):
        data = {'source': {'continuity': {'version': 1, 'tables': {'tabMpesa Express Request': {}, 'tabVersion': {}}}},
                'schemas': {'Version': {}}}
        with self.assertRaisesRegex(ValueError, 'Mpesa Express Request'):
            require_capture(data)
        data['schemas']['Mpesa Express Request'] = {'count': 0}
        self.assertEqual(require_capture(data)['version'], 1)
