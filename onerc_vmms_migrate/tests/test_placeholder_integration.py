import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

from onerc_vmms_migrate import batch_browser, batches, browser, login_policy, placeholder_emails, run
from onerc_vmms_migrate.tests.test_transform import snapshot, volunteer_employee


class TestPlaceholderFingerprint(TestCase):
    def test_batch_initialization_allocates_before_freezing_the_input_fingerprint(self):
        snap = snapshot(source={"source_id": "s"}, schemas={"Employee": {}}, history={"E": {}},
            employees={"E": volunteer_employee("E", personal_email=None)})
        conf = {"assign_missing_emails": True, "preserve_passwords": True}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            walk = root / "crosswalks"
            walk.mkdir()
            with patch.object(placeholder_emails, "registry_path", return_value=root / "placeholder-emails.json"), \
                    patch.object(browser.workspace, "config", return_value=conf), \
                    patch.object(browser.workspace, "crosswalk_dir", return_value=str(walk)), \
                    patch.object(batch_browser.frappe, "db", MagicMock()), \
                    patch.object(batch_browser.frappe, "get_all", return_value=[]), \
                    patch.object(batches, "commit_checkpoint"), \
                    patch.object(run, "selftest", return_value={"passed": True}), \
                    patch.object(run, "_run_id", return_value="identity-test"), \
                    patch.object(login_policy, "plan", return_value={}):
                state = batch_browser.initialize("import", 1, conf, MagicMock(data=snap))
                self.assertTrue((root / "placeholder-emails.json").exists())
                self.assertEqual(state["fingerprint"], browser.fingerprint())
                self.assertEqual(snap["_migration_identity_emails"][0]["email"], "missingemail1@vmms.com")

    def test_rehearsal_and_resume_detect_changes_to_the_persistent_alias_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            crosswalks = root / "crosswalks"
            crosswalks.mkdir()
            registry = root / "placeholder-emails.json"
            registry.write_text('{"version":1,"assignments":[]}')
            with patch.object(browser.workspace, "config", return_value={}), \
                    patch.object(browser.workspace, "crosswalk_dir", return_value=str(crosswalks)):
                before = browser.fingerprint(include_code=False)
                registry.write_text('{"version":1,"assignments":[{"email":"missingemail1@vmms.com"}]}')
                self.assertNotEqual(browser.fingerprint(include_code=False), before)

    def test_a_native_schema_error_also_blocks_the_identity_review_row(self):
        result = {"bundles": [{"key": "person", "errors": [], "warnings": [], "volunteer": None,
            "member": None, "memberships": []}], "not_migrated": [],
            "mapping_issues": [{"person": "person", "severity": "error"}],
            "identity_review": [{"person": "person", "native_status": "eligible_pending_native_validation"}]}
        cw = MagicMock()
        cw.placeholder_hits = {}
        with tempfile.TemporaryDirectory() as folder, patch.object(run.workspace, "report_dir", return_value=folder), \
                patch.object(run.workspace, "write_json"), patch.object(run, "_say"):
            run._report_plan("test", cw, result)
        self.assertEqual(result["identity_review"][0]["native_status"], "blocked_pending_resolution")
