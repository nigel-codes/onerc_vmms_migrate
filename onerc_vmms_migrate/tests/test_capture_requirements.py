from unittest import TestCase
from onerc_vmms_migrate.continuity import require_capture


class TestFreshCaptureRequirements(TestCase):
    def test_old_captures_remain_readable_but_cannot_pass_new_import_preflight(self):
        with self.assertRaisesRegex(ValueError, 'fresh continuity capture'):
            require_capture({'source': {'source_id': 'old'}, 'schemas': {'Employee': {}}})

    def test_present_source_payments_or_audit_tables_must_have_export_schemas(self):
        data = {'source': {'profile_mapping_version':2,'continuity': {'version': 1, 'tables': {'tabMpesa Express Request': {}, 'tabVersion': {}}}},
                'schemas': {'Version': {}}}
        with self.assertRaisesRegex(ValueError, 'Mpesa Express Request'):
            require_capture(data)
        data['schemas']['Mpesa Express Request'] = {'count': 0}
        self.assertEqual(require_capture(data)['version'], 1)

    def test_pre_mapping_capture_cannot_be_resumed_as_a_complete_person_import(self):
        data={'source':{'continuity':{'version':1,'tables':{}},'profile_mapping_version':1}}
        with self.assertRaisesRegex(ValueError,'profile mapping'):
            require_capture(data)

    def test_resuming_after_preflight_does_not_bypass_the_fresh_capture_requirement(self):
        from onerc_vmms_migrate import batches
        with self.assertRaisesRegex(ValueError,"profile mapping"):
            batches.work({'source':{'continuity':{'version':1,'tables':{}},'profile_mapping_version':1}},None,{},
                {'phase':'people','offset':0,'batch_size':1,'run_id':'test'},None)
