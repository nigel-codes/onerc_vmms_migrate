from unittest import TestCase
from unittest.mock import Mock, patch

import frappe

from onerc_vmms_migrate.load import _files


class TestDeferredPeopleFiles(TestCase):
    def test_people_preload_retains_source_file_references_without_copying_bytes(self):
        items = [{"url": "/private/files/source.pdf", "purpose": "table", "table": "vmms_training", "index": 0}]
        ctx = {"bundle": {"files": items}, "conf": {"defer_profile_files": True}, "warnings": [], "source": Mock()}
        with patch.object(frappe, "get_doc") as native, patch("onerc_vmms_migrate.load._copy_file", side_effect=AssertionError("Deferred bytes must not be copied")):
            _files(ctx, "RP-TEST")
        native.assert_not_called()
        ctx["source"].file_path.assert_not_called()
        self.assertEqual(ctx["bundle"]["files"], items)
        self.assertIn("deferred", ctx["warnings"][0])

    def test_default_import_still_processes_profile_attachments(self):
        ctx = {"bundle": {"files": [{"url": "/files/photo.png", "purpose": "photo"}]}, "conf": {}, "warnings": []}
        with patch.object(frappe, "get_doc"), patch("onerc_vmms_migrate.load._copy_file", return_value=None) as copy:
            _files(ctx, "RP-TEST")
        copy.assert_called_once()
