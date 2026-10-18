from unittest import TestCase
from onerc_vmms_migrate.login_policy import decision


class TestLegacyEmailLogin(TestCase):
    def test_source_email_links_preserved_without_loosening_limits(self):
        plan=decision({'login_with_email_link':1,'login_with_email_link_expiry':30,'rate_limit_email_link_login':20},
            {'login_with_email_link':0,'login_with_email_link_expiry':10,'rate_limit_email_link_login':5})
        self.assertTrue(plan['enable_after_import'])
        self.assertEqual(plan['login_with_email_link_expiry'],10)
        self.assertEqual(plan['rate_limit_email_link_login'],5)

    def test_destination_mfa_is_never_disabled_or_bypassed(self):
        with self.assertRaises(ValueError):decision({'login_with_email_link':1},{'enable_two_factor_auth':1})
        with self.assertRaises(ValueError):decision({'login_with_email_link':1},{},False)
        with self.assertRaises(ValueError):decision({}, {})
        self.assertFalse(decision({'login_with_email_link':1},{'login_with_email_link':1})['enable_after_import'])
