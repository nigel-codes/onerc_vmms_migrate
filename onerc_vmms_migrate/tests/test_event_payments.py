from unittest import TestCase
from onerc_vmms_migrate.event_payments import state


class TestHistoricalBookingSettlement(TestCase):
    def test_submitted_without_evidence_is_not_paid(self):
        self.assertEqual(state({'docstatus':1,'total_amount':100,'currency':'KES'},[]),'Verification Pending')

    def test_only_matching_settled_amounts_support_paid(self):
        booking={'total_amount':100,'currency':'KES'}
        invoice={'docstatus':1,'currency':'KES','grand_total':100,'outstanding_amount':0}
        self.assertEqual(state(booking,[invoice]),'Paid')
        for changes in ({'outstanding_amount':1},{'currency':'USD'},{'docstatus':2},{'is_return':1},{'grand_total':50},{'outstanding_amount':None}):
            self.assertEqual(state(booking,[{**invoice,**changes}]),'Verification Pending')
