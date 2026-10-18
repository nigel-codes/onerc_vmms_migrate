from unittest import TestCase
from onerc_vmms_migrate import normalise


class TestInvalidLegacyLoginAddresses(TestCase):
    def test_malformed_addresses_are_not_silently_repaired(self):
        for value in ('person@example.org.','a..b@example.org','a@example..org','two@example.org,other@example.org'):
            self.assertIsNone(normalise.email(value))
        self.assertEqual(normalise.email(' Person+tag@Example.org '),'person+tag@example.org')
