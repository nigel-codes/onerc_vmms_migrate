"""Browser migration boundary tests; no live source or target is modified."""

import json
import os
import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from onerc_vmms_migrate import browser, browser_source, extract
from onerc_vmms_migrate.source import SourceError

PUBLIC_DNS = [(2, 1, 6, "", ("8.8.8.8", 443))]


class TestRemoteSource(TestCase):
	@patch.object(browser_source.socket, "getaddrinfo", return_value=PUBLIC_DNS)
	def test_only_public_https_origins(self, dns):
		self.assertEqual(browser_source.validate_url("https://old.example.org/"), "https://old.example.org")
		for value in (
			"http://old.example.org",
			"https://user:secret@old.example.org",
			"https://old.example.org/api",
			"https://old.example.org?key=secret",
			"https://old.example.org:8443",
			"https://old.example.org/#fragment",
		):
			with self.subTest(value=value), self.assertRaises(SourceError):
				browser_source.validate_url(value)
		dns.return_value = [(2, 1, 6, "", ("127.0.0.1", 443))]
		with self.assertRaises(SourceError):
			browser_source.validate_url("https://localhost")
		dns.return_value = [*PUBLIC_DNS, (10, 1, 6, "", ("::1", 443, 0, 0))]
		with self.assertRaises(SourceError):
			browser_source.validate_url("https://mixed.example.org")

	def client(self):
		client = object.__new__(browser_source.APIClient)
		client.url = "https://old.example.org"
		client.session = MagicMock()
		return client

	@patch.object(browser_source, "validate_url")
	def test_redirects_are_refused_and_headers_never_enter_error(self, validate):
		client = self.client()
		client.session.get.return_value.status_code = 302
		with self.assertRaisesRegex(SourceError, "redirects are refused"):
			client.get("/api/resource/User")
		self.assertFalse(client.session.get.call_args.kwargs["allow_redirects"])
		client.session.get.return_value.close.assert_called_once()

	def test_pagination_handles_server_page_size_caps(self):
		client = self.client()
		client.json = MagicMock(
			side_effect=[{"data": [{"name": "a"}]}, {"data": [{"name": "b"}]}, {"data": []}]
		)
		rows = list(client.docs("User", fields=["name"]))
		self.assertEqual([r["name"] for r in rows], ["a", "b"])
		self.assertEqual([call.args[1]["limit_start"] for call in client.json.call_args_list], [0, 1, 2])

	def test_repeated_page_fails_instead_of_silently_duplicating(self):
		client = self.client()
		client.json = MagicMock(return_value={"data": [{"name": "a"}]})
		with self.assertRaisesRegex(SourceError, "Pagination repeated"):
			list(client.docs("User", fields=["name"]))

	def test_attachment_paths_cannot_leave_source_or_cache(self):
		client = self.client()
		with tempfile.TemporaryDirectory() as directory:
			for url in (
				"https://other.example.org/files/a.pdf",
				"//other.example.org/files/a.pdf",
				"/files/../secret",
				"/files/%2e%2e%2fsecret",
				"/private/files/..",
				"/files/a?token=x",
				"/files/a%5cb.pdf",
			):
				with self.subTest(url=url), self.assertRaises(SourceError):
					client.download(url, directory)
			client.session.get.assert_not_called()

	def test_snapshot_reuses_captured_data_and_confines_files(self):
		source = object.__new__(browser_source.SnapshotSource)
		source.data = {"captured_files": {"/files/a": "../secret"}}
		self.assertIs(extract.extract(source), source.data)
		with patch.object(browser_source.workspace, "root", return_value="/tmp/capture-root"):
			with self.assertRaises(SourceError):
				source.file_path("/files/a")


class TestBrowserControls(TestCase):
	def setUp(self):
		def throw(message, exc=frappe.ValidationError, **kwargs):
			raise exc(message)

		mocked = patch.object(frappe, "throw", side_effect=throw)
		mocked.start()
		self.addCleanup(mocked.stop)

	def test_non_manager_is_denied(self):
		with (
			patch.object(frappe, "session", SimpleNamespace(user="user@example.org")),
			patch.object(frappe, "get_roles", return_value=["Website User"]),
		):
			with self.assertRaises(frappe.PermissionError):
				browser.authorize()

	@patch.object(browser, "authorize")
	def test_report_traversal_rejected_before_reading(self, authorize):
		for run, filename in (("../capture", "snapshot.json"), ("run", "../secret.json"), ("..", "a.json")):
			with self.subTest(run=run, filename=filename), self.assertRaises(frappe.ValidationError):
				browser.download_report(run, filename)

	@patch.object(browser, "authorize")
	@patch.object(frappe, "get_single", return_value=SimpleNamespace(browser_capture="abc"))
	@patch.object(browser, "state", return_value={})
	def test_import_without_successful_rehearsal_is_blocked(self, state, settings, authorize):
		with (
			patch.object(browser, "fingerprint", return_value="new"),
			self.assertRaises(frappe.ValidationError),
		):
			browser.start("import", "LOAD", True, True, True)

	@patch.object(browser, "authorize")
	@patch.object(frappe, "get_single", return_value=SimpleNamespace(browser_capture="abc"))
	def test_source_email_login_requires_delivery_confirmation(self,settings,authorize):
		value={"rehearsal":{"fingerprint":"current","passed":True,
			"authentication_policy":{"mail_delivery_test_required":True}}}
		with patch.object(browser,"state",return_value=value),patch.object(browser,"fingerprint",return_value="current"):
			with self.assertRaisesRegex(frappe.ValidationError,"email-link"):
				browser.start("import","LOAD",True,True,True)

	@patch.object(browser, "authorize")
	@patch.object(frappe, "get_single", return_value=SimpleNamespace(browser_capture="abc"))
	def test_changed_mapping_invalidates_import(self, settings, authorize):
		with (
			patch.object(
				browser, "state", return_value={"rehearsal": {"fingerprint": "old", "passed": True}}
			),
			patch.object(browser, "fingerprint", return_value="new"),
			self.assertRaises(frappe.ValidationError),
		):
			browser.start("import", "LOAD", True, True, True)

	@patch.object(browser, "authorize")
	@patch.object(frappe, "get_single", return_value=SimpleNamespace(browser_capture="abc"))
	def test_all_cutover_confirmations_required(self, settings, authorize):
		with (
			patch.object(
				browser, "state", return_value={"rehearsal": {"fingerprint": "current", "passed": True}}
			),
			patch.object(browser, "fingerprint", return_value="current"),
		):
			for args in [
				("LOAD", False, True, True),
				("LOAD", True, False, True),
				("LOAD", True, True, False),
				("load", True, True, True),
			]:
				with self.subTest(args=args), self.assertRaises(frappe.ValidationError):
					browser.start("import", *args)


class TestCaptureFinalization(TestCase):
	setUp = TestBrowserControls.setUp
	def test_registration_reopens_idle_connection_before_writing(self):
		database = MagicMock()
		with patch.object(frappe, "db", database):
			browser._register_capture("a" * 32)
		self.assertEqual([call[0] for call in database.mock_calls],
			["close", "connect", "set_single_value", "commit"])
		database.set_single_value.assert_called_once_with(browser.SETTINGS, "browser_capture", "a" * 32)

	def test_explicit_resume_requires_local_transfer_and_capture_action(self):
		with tempfile.TemporaryDirectory() as directory:
			path = os.path.join(directory, "transfer.json")
			with patch.object(browser.workspace, "path", return_value=path):
				with self.assertRaises(frappe.ValidationError):
					browser._validate_resume_capture("capture", "a" * 32)
				with open(path, "w") as handle:
					handle.write("{}")
				browser._validate_resume_capture("capture", "a" * 32)
				for action, identifier in [("import", "a" * 32), ("capture", "../escape")]:
					with self.assertRaises(frappe.ValidationError):
						browser._validate_resume_capture(action, identifier)


class TestCapture(TestCase):
	def test_capture_keeps_migration_fields_and_downloads_unattached_images(self):
		from onerc_vmms_migrate.tests.test_transform import volunteer_employee

		client = MagicMock()
		client.url = "https://old.example.org"
		login = {"name": "migration@example.org", "roles": [{"role": "System Manager"}]}
		user = {
			"name": "amina@example.org",
			"first_name": "Amina",
			"last_name": "Otieno",
			"enabled": 1,
			"user_image": "/files/amina.png",
			"api_key": "MUST-NOT-COPY",
			"api_secret": "MUST-NOT-COPY",
			"education": [{"school_univ": "School"}],
		}

		def payload(path, params=None):
			if path.endswith("get_logged_user"):
				return {"message": login["name"]}
			if path.startswith("/api/resource/User/"):
				return {"data": login}
			dt = params["doctype"]
			return {"docs": [{"doctype": "DocType", "name": dt, "fields": []}]}

		client.json.side_effect = payload
		client.docs.side_effect = lambda dt, *args: iter(
			{"User": [user], "Employee": [volunteer_employee()]}.get(dt, [])
		)
		client.download.return_value = "captures/cap/files/amina.png"
		with (
			tempfile.TemporaryDirectory() as directory,
			patch.object(browser_source, "APIClient", return_value=client),
			patch.object(browser_source.workspace, "root", return_value=directory),
		):
			result = browser_source.capture(SimpleNamespace(), "cap")
			source = browser_source.SnapshotSource({"browser_capture": "cap"})
			self.assertEqual(result["counts"]["employees"], 1)
			self.assertEqual(result["files"], 1)
			self.assertNotIn("api_key", source.data["users"][user["name"]])
			self.assertNotIn("api_secret", source.data["users"][user["name"]])
			self.assertEqual(source.data["users"][user["name"]]["education"], user["education"])
			self.assertEqual(source.data["password_hashes"], {})
			client.download.assert_called_once()
		client.close.assert_called_once()

	def test_restricted_source_account_is_rejected(self):
		client = MagicMock()
		client.json.side_effect = [
			{"message": "migration@example.org"},
			{"data": {"roles": [{"role": "System Manager"}]}},
		]
		client.docs.return_value = iter([{"name": "restricted"}])
		with (
			patch.object(browser_source, "APIClient", return_value=client),
			self.assertRaisesRegex(SourceError, "restrictions"),
		):
			browser_source.capture(SimpleNamespace(), "cap")


class TestServiceRecovery(TestCase):
	setUp = TestBrowserControls.setUp

	@patch.object(browser, "authorize")
	@patch.object(browser, "assert_idle")
	def test_incomplete_import_requires_explicit_recovery(self, idle, authorize):
		with (
			patch.object(
				browser, "state", return_value={"cutover": {"scheduler_enabled": False, "mute_emails": 1}}
			),
			self.assertRaises(frappe.ValidationError),
		):
			browser.finish_cutover()

	@patch.object(browser, "authorize")
	@patch.object(browser, "assert_idle")
	@patch.object(browser, "save_state")
	def test_recovery_restores_original_values_without_deleting_records(self, save, idle, authorize):
		value = {"cutover": {"scheduler_enabled": False, "mute_emails": 1}, "import_result": {"failed": 3}}
		with (
			patch.object(browser, "state", return_value=value),
			patch("frappe.utils.scheduler.toggle_scheduler") as scheduler,
			patch("frappe.installer.update_site_config") as config,
		):
			browser.finish_cutover("RESTORE")
		scheduler.assert_called_once_with(False)
		config.assert_called_once_with("mute_emails", 1)
		self.assertNotIn("cutover", value)
		self.assertEqual(value["import_result"]["failed"], 3)
		self.assertTrue(value["services_restored_after_failure"])
