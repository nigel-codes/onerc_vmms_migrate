from unittest import TestCase
from onerc_vmms_migrate.history_model import History, membership_state, assignment_state, application_state, split_name


class TestHistoricalMappings(TestCase):
    def test_all_membership_states_preserve_payment_and_approval_distinctions(self):
        expected={'Draft':('Draft','Draft'),'Pending':('Awaiting Payment','Submitted'),
            'Rejected':('Cancelled','Rejected'),'Active':('Active','Approved'),'Expired':('Expired','Approved')}
        for source,target in expected.items():
            self.assertEqual(membership_state({'status':source}),target)
        self.assertEqual(membership_state({'status':'Pending'},paid=True),('Awaiting Approval','Submitted'))
        self.assertEqual(membership_state({'status':'Active','docstatus':2}),('Cancelled','Withdrawn'))
        with self.assertRaises(ValueError):membership_state({'status':'Unknown'})

    def test_assignment_acceptance_never_becomes_attendance(self):
        for source,target in [('Pending','Pending'),('Accepted','Accepted'),('Rejected','Declined'),('Cancelled','Withdrawn')]:
            self.assertEqual(assignment_state({'deployment_status':source}),target)
        with self.assertRaises(ValueError):assignment_state({'deployment_status':'Participated'})

    def test_application_rejection_and_cancellation_are_retained(self):
        self.assertEqual(application_state({'status':'Rejected'}),'Rejected')
        self.assertEqual(application_state({'status':'Open','docstatus':2}),'Withdrawn')

    def test_child_rows_are_scoped_by_parent_type_and_field_and_sorted_by_original_order(self):
        rows=[{'name':'two','parent':'same','parenttype':'A','parentfield':'people','idx':2},
            {'name':'one','parent':'same','parenttype':'A','parentfield':'people','idx':1},
            {'name':'other','parent':'same','parenttype':'B','parentfield':'people','idx':1},
            {'name':'field','parent':'same','parenttype':'A','parentfield':'other','idx':1}]
        index=History({'history':{str(i):{'doctype':'Child','record':row} for i,row in enumerate(rows)}})
        self.assertEqual([r['name'] for r in index.children('A','same','people')],['one','two'])
        self.assertEqual(len(index.rows('Child')),4)

    def test_single_name_is_preserved_without_inventing_a_surname(self):
        self.assertEqual(split_name(' Amina '),('Amina',''))
        self.assertEqual(split_name('Amina Wanjiru Otieno'),('Amina','Wanjiru Otieno'))
