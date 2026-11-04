from decimal import Decimal
from unittest import TestCase
from unittest.mock import MagicMock, patch

from onerc_vmms_migrate import history_model


class TestSourceArchiveStream(TestCase):
    def test_full_user_history_does_not_create_a_second_partial_user_version(self):
        snapshot = {'schemas': {'User': {'count': 1}},
            'history': {'one': {'doctype': 'User', 'record': {'name': 'person', 'headline': 'Original'}}},
            'users': {'person': {'name': 'person'}}}
        self.assertEqual(history_model.archive_users(snapshot), {})

    def test_older_captures_still_include_their_person_user_records(self):
        users = {'person': {'name': 'person'}}
        self.assertEqual(history_model.archive_users({'users': users}), users)


class TestHistoricalPaymentEvidence(TestCase):
    def project(self, row, doctype='Mpesa Express Request'):
        from onerc_vmms_migrate.legacy_payments import project
        return project('source', doctype, row, snapshot_id='capture', run_id='run')

    def test_receipted_request_preserves_exact_evidence_without_assuming_allocation(self):
        row = {'name': 'MEXP-old', 'status': 'Completed', 'result_code': '0',
            'transaction_id': 'RECEIPT', 'amount': Decimal('100.000000001'),
            'reference_doctype': 'Payment Request', 'reference_name': 'missing',
            'owner': 'original-owner', 'creation': '2025-01-01 12:00:00'}
        result = self.project(row)
        self.assertEqual(result['amount'], '100.000000001')
        self.assertEqual(result['receipt_number'], 'RECEIPT')
        self.assertEqual(result['original_status'], 'Completed')
        self.assertEqual(result['source_reference_name'], 'missing')
        self.assertEqual(result['mapping_status'], 'Unresolved')
        self.assertEqual(result['currency'], '')
        self.assertEqual(result['source_owner'], 'original-owner')
        self.assertEqual(row['amount'], Decimal('100.000000001'))

    def test_failed_pending_and_nonfinite_amounts_are_not_paid_transactions(self):
        for state in ['Failed', 'In Progress']:
            self.assertEqual(self.project({'name': 'old', 'status': state, 'amount': 1})['original_status'], state)
        with self.assertRaises(ValueError):
            self.project({'name': 'bad', 'amount': 'NaN'})

    def test_record_identity_is_source_typed_versioned_and_independent_of_run(self):
        a = self.project({'name': 'old', 'amount': 1, 'transaction_id': 'same'})
        b = self.project({'name': 'other', 'amount': 1, 'transaction_id': 'same'})
        c = self.project({'name': 'old', 'amount': 2, 'transaction_id': 'same'})
        self.assertEqual(len({a['name'], b['name'], c['name']}), 3)
        self.assertEqual(a['name'], self.project({'name': 'old', 'amount': 1, 'transaction_id': 'same'})['name'])

    def test_reimport_reuses_evidence_and_never_calls_a_live_gateway_or_commits(self):
        import frappe
        from onerc_vmms_migrate import legacy_payments
        db = MagicMock()
        doc = MagicMock()
        db.exists.side_effect = lambda doctype, *args: doctype == 'DocType'
        item = {'doctype': 'Mpesa Express Request', 'record': {'name': 'old', 'amount': 1}}
        with patch.object(frappe, 'db', db), patch.object(frappe, 'get_doc', return_value=doc):
            first = legacy_payments.retain({'source': {'source_id': 's'}}, 'run', [item])
            db.exists.side_effect = None
            db.exists.return_value = True
            db.get_value.return_value = legacy_payments.project('s', item['doctype'], item['record'])
            second = legacy_payments.retain({'source': {'source_id': 's'}}, 'run', [item])
        self.assertEqual(first, {'created': 1, 'reused': 0})
        self.assertEqual(second, {'created': 0, 'reused': 1})
        doc.insert.assert_called_once()
        db.commit.assert_not_called()

    def test_reused_payment_body_must_still_match_its_original_hash(self):
        import frappe
        from onerc_vmms_migrate import legacy_payments
        db = MagicMock()
        db.exists.return_value = True
        db.get_value.return_value = {'source_hash': 'wrong', 'source_payload': '{}'}
        with patch.object(frappe, 'db', db), self.assertRaisesRegex(ValueError, 'integrity'):
            legacy_payments.retain({'source': {'source_id': 's'}}, 'run',
                [{'doctype': 'Mpesa Express Request', 'record': {'name': 'old', 'amount': 1}}])

    def test_missing_or_changed_payment_projection_fails_reconciliation(self):
        import frappe
        from onerc_vmms_migrate import legacy_payments
        item = {'doctype': 'Mpesa Express Request', 'record': {'name': 'old', 'amount': 1}}
        data = {'source': {'source_id': 's'}, 'history': {'old': item}}
        with patch.object(frappe, 'get_all', return_value=[]):
            self.assertEqual(len(legacy_payments.verify(data)), 1)
        stored = legacy_payments.project('s', item['doctype'], item['record'])
        with patch.object(frappe, 'get_all', return_value=[stored]):
            self.assertEqual(legacy_payments.verify(data), [])
            stored['amount'] = '999'
            self.assertEqual(len(legacy_payments.verify(data)), 1)
