from unittest import TestCase
from onerc_vmms_migrate.availability_model import windows,day


class TestHistoricalAvailability(TestCase):
    def test_overnight_is_next_day_and_never_worked_hours(self):
        self.assertEqual(windows('23:00:00','7:00:00'),[(0,'23:00:00','23:59:59'),(1,'00:00:00','7:00:00')])
        self.assertEqual(day('Sunday',1),'Monday')

    def test_normal_and_unspecified_windows(self):
        self.assertEqual(windows('07:00:00','12:00:00'),[(0,'07:00:00','12:00:00')])
        self.assertEqual(windows('07:00:00','07:00:00'),[(0,None,None)])
        self.assertEqual(windows(None,None),[(0,None,None)])
        with self.assertRaises(ValueError):windows('25:00:00','07:00:00')
