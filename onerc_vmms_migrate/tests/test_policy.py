from unittest import TestCase
from onerc_vmms_migrate.policy import source_exclusions_accepted


class TestReviewedSourceExceptions(TestCase):
    def test_empty_exclusions_need_no_exception(self):
        self.assertTrue(source_exclusions_accepted(0,100,0,100,False))

    def test_review_and_each_independent_ceiling_required(self):
        self.assertFalse(source_exclusions_accepted(1,100,0,100,False))
        self.assertTrue(source_exclusions_accepted(1,100,1,100,True))
        self.assertFalse(source_exclusions_accepted(2,100,0,10000,True))
        self.assertFalse(source_exclusions_accepted(0,10000,2,100,True))
        self.assertFalse(source_exclusions_accepted(1,0,0,100,True))
