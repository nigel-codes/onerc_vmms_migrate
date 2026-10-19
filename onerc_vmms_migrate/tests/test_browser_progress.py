"""Worker progress must survive transaction rollback without exposing record values."""
import copy
from contextlib import ExitStack
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
import frappe.installer
import frappe.utils.scheduler
from onerc_vmms_migrate import browser, full_run


class TestWorkerProgress(TestCase):
    def exercise(self, action):
        state = {'action': action, 'transfer': {'phase': 'files', 'processed': 21900},
                 'rehearsal': {'passed': True, 'fingerprint': 'current'},
                 'cutover': {'scheduler_enabled': False}}
        writes = []
        cache = MagicMock()
        cache.get.return_value = b'token'
        def execute(**kwargs):
            callback = kwargs.get('progress')
            if callback:
                callback({'phase':'people','processed':100,'total':24877,
                          'source_user':'must-not-be-written@example.org'})
            return {'passed': True}
        with ExitStack() as stack:
            for target, name, value in [
                (frappe, 'cache', cache), (frappe, 'set_user', MagicMock()),
                (frappe, 'db', MagicMock()), (frappe, 'conf', frappe._dict()),
                (frappe.utils.scheduler, 'disable_scheduler', MagicMock()),
                (frappe.installer, 'update_site_config', MagicMock()),
                (browser, 'authorize', MagicMock()),
                (browser, 'lock_key', MagicMock(return_value='lock')),
                (browser, 'state', MagicMock(return_value=state)),
                (browser, 'save_state', MagicMock(side_effect=lambda v:writes.append(copy.deepcopy(v)))),
                (browser, 'fingerprint', MagicMock(return_value='current')),
                (browser, 'now_datetime', MagicMock(return_value='2026-10-19 20:00:00')),
                (full_run, 'execute', MagicMock(side_effect=execute))]:
                stack.enter_context(patch.object(target, name, value))
            browser.execute('token', action, 'Administrator')
        return state, writes

    def test_rehearsal_reports_actual_stage_and_clears_old_capture_totals(self):
        state, writes = self.exercise('rehearsal')
        self.assertEqual(state.get('progress'), {'phase':'people','processed':100,'total':24877})
        self.assertNotIn('transfer', state)
        self.assertEqual(state['progress_updated_at'], '2026-10-19 20:00:00')
        self.assertTrue(any(row.get('progress', {}).get('processed') == 100 for row in writes))
        self.assertNotIn('must-not-be-written', str(writes))

    def test_live_load_uses_the_same_progress_callback(self):
        state, writes = self.exercise('import')
        self.assertEqual(state.get('progress', {}).get('processed'), 100)
        self.assertEqual(state['status'], 'finished')
