from unittest import TestCase


class TestNamingContinuation(TestCase):
    def snapshot(self):
        return {'source': {'continuity': {'version': 1, 'naming': {
            'rules': [{'document_type': 'Employee', 'prefix': 'VOL.-.YY.-', 'prefix_digits': 6,
                       'counter': 31709, 'disabled': 0, 'conditions': []}],
            'series': [{'name': 'VM-MEM-2025-', 'current': 31}],
            'fields': [{'parent': 'VM Member', 'default': 'VM-MEM-.YYYY.-', 'options': 'VM-MEM-.YYYY.-'},
                       {'parent': 'VM Membership', 'default': 'VM-MSH-.YYYY.-', 'options': 'VM-MSH-.YYYY.-'}],
            'properties': []}}}, 'employees': {'one': {'name': 'VOL-26-031709'}},
            'members': {'one': {'name': 'VM-MEM-2025-00015'}}, 'memberships': {}}

    def test_actual_rule_counter_wins_over_employee_series_or_row_counts(self):
        from onerc_vmms_migrate.naming_plan import build
        plan = build(self.snapshot())
        self.assertEqual(plan['rules'][0]['counter'], 31709)
        self.assertEqual(plan['rules'][0]['document_type'], 'VMMS Volunteer')
        self.assertEqual(plan['rules'][0]['prefix_digits'], 6)
        self.assertFalse(plan['applied'])

    def test_deleted_number_gaps_and_destination_high_water_marks_survive(self):
        from onerc_vmms_migrate.naming_plan import build
        plan = build(self.snapshot(), target_names={'VMMS Volunteer': ['VOL-26-050000']},
                     target_series=[{'name': 'VM-MEM-2025-', 'current': 40}])
        self.assertEqual(plan['rules'][0]['counter'], 50000)
        self.assertEqual(plan['series'][0]['current'], 40)

    def test_missing_or_conditional_source_policy_cannot_be_guessed(self):
        from onerc_vmms_migrate.naming_plan import build
        with self.assertRaises(ValueError):
            build({'source': {}})
        data = self.snapshot()
        data['source']['continuity']['naming']['rules'][0]['conditions'] = [{'field': 'company'}]
        with self.assertRaisesRegex(ValueError, 'conditional'):
            build(data)
