from unittest import TestCase
from onerc_vmms_migrate.file_transfer import required_privacy


class TestHistoricalAttachmentPrivacy(TestCase):
    def test_personal_documents_stay_private_even_if_old_upload_was_public(self):
        self.assertTrue(required_privacy('User',False))
        self.assertTrue(required_privacy('VMMS Membership',False))
        self.assertTrue(required_privacy('Event Ticket',False))
        self.assertFalse(required_privacy('Buzz Event',False))

    def test_avatar_exception_cannot_publicise_a_source_private_file(self):
        self.assertFalse(required_privacy('User',False,False))
        self.assertTrue(required_privacy('User',True,False))
        self.assertTrue(required_privacy('Legacy VMMS Document',False,False))
