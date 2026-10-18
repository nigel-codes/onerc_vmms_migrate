"""Capture an old Frappe site through read-only REST, then use that fixed snapshot."""

import hashlib
import ipaddress
import json
import os
import socket
from urllib.parse import quote, unquote, urlsplit

import frappe
import requests

from onerc_vmms_migrate import workspace
from onerc_vmms_migrate.source import SourceError

MAX_FILE = 25 * 1024 * 1024


def validate_url(value):
	parsed = urlsplit(value.strip())
	if (
		parsed.scheme != "https"
		or not parsed.hostname
		or parsed.username
		or parsed.password
		or parsed.path not in ("", "/")
		or parsed.query
		or parsed.fragment
		or parsed.port not in (None, 443)
	):
		raise SourceError("Use the old site's HTTPS origin, for example https://old.example.org.")
	try:
		addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
	except OSError:
		raise SourceError("The old site's hostname could not be resolved.") from None
	if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
		raise SourceError("The source must resolve to public internet addresses.")
	return f"https://{parsed.hostname.lower()}"


class APIClient:
	def __init__(self, settings):
		self.url = validate_url(settings.source_url)
		self.session = requests.Session()
		self.session.trust_env = False
		self.session.headers.update(
			{
				"Authorization": f"token {settings.api_key}:{settings.get_password('api_secret')}",
				"Accept": "application/json",
			}
		)

	def close(self):
		self.session.close()

	def get(self, path, params=None, stream=False):
		# Recheck the destination; never follow redirects with credentials.
		validate_url(self.url)
		try:
			response = self.session.get(
				self.url + path, params=params, timeout=(10, 90), allow_redirects=False, stream=stream
			)
		except requests.RequestException:
			raise SourceError(
				"The old site could not be reached over HTTPS. Check connectivity and TLS."
			) from None
		if response.status_code != 200:
			response.close()
			raise SourceError(
				f"Old site returned HTTP {response.status_code} for {path}. Check API permissions; redirects are refused."
			)
		return response

	def json(self, path, params=None):
		with self.get(path, params) as response:
			try:
				return response.json()
			except ValueError:
				raise SourceError("The old site returned an unexpected response instead of JSON.") from None

	def docs(self, doctype, filters=None, fields=None):
		start = 0
		seen = set()
		while True:
			rows = self.json(
				"/api/resource/" + quote(doctype, safe=""),
				{
					"fields": json.dumps(fields or ["name"]),
					"filters": json.dumps(filters or []),
					"order_by": "name asc",
					"limit_start": start,
					"limit_page_length": 100,
				},
			).get("data")
			if not isinstance(rows, list):
				raise SourceError(f"Invalid listing for {doctype}.")
			if not rows:
				break
			for row in rows:
				if row["name"] in seen:
					raise SourceError(f"Pagination repeated a {doctype} record; capture stopped.")
				seen.add(row["name"])
				yield (
					row
					if fields
					else self.json(
						"/api/resource/" + quote(doctype, safe="") + "/" + quote(row["name"], safe="")
					)["data"]
				)
			start += len(rows)

	def download(self, url, directory):
		path = urlsplit(url)
		decoded = unquote(path.path)
		if (
			path.scheme
			or path.netloc
			or path.query
			or path.fragment
			or not decoded.startswith(("/files/", "/private/files/"))
		):
			raise SourceError(
				"An attachment has an unsupported URL; only files on the old site are supported."
			)
		prefix = "/private/files/" if decoded.startswith("/private/files/") else "/files/"
		filename = decoded[len(prefix) :]
		if not filename or "/" in filename or "\\" in filename or filename in (".", ".."):
			raise SourceError("An attachment has an unsafe filename.")
		folder = os.path.join(directory, hashlib.sha256(url.encode()).hexdigest())
		os.makedirs(folder, exist_ok=True)
		target = os.path.join(folder, filename)
		try:
			with self.get(quote(decoded, safe="/"), stream=True) as response, open(target, "wb") as handle:
				if "text/html" in response.headers.get("Content-Type", "").lower():
					raise SourceError("An attachment returned a login page instead of a file.")
				size = 0
				for chunk in response.iter_content(65536):
					size += len(chunk)
					if size > MAX_FILE:
						raise SourceError("An attachment exceeds the browser migration limit of 25 MB.")
					handle.write(chunk)
		except requests.RequestException:
			raise SourceError("An attachment download was interrupted; capture must be retried.") from None
		return os.path.relpath(target, workspace.root())


def capture(settings, capture_id):
	from onerc_vmms_migrate import extract

	client = APIClient(settings)
	try:
		# A privileged source user prevents Frappe user-permission filters silently omitting records.
		login = client.json("/api/method/frappe.auth.get_logged_user")["message"]
		user = client.json("/api/resource/User/" + quote(login, safe=""))["data"]
		if login != "Administrator" and not any(
			r.get("role") == "System Manager" for r in user.get("roles", [])
		):
			raise SourceError(
				"Use an old-site System Manager with unrestricted read access to the migration records."
			)
		if list(client.docs("User Permission", [["user", "=", login]], ["name"])):
			raise SourceError(
				"The source API user has User Permission restrictions. Remove them or use an unrestricted migration account."
			)
		snapshot = {
			"source": {
				"db_name": client.url,
				"site_path": client.url,
				"apps": "Frappe v15 REST snapshot",
				"capture_id": capture_id,
			},
			"password_hashes": {},
			"invoices": {},
			"files": {},
			"missing_columns": {},
			"custom_fields": [],
		}
		specs = [
			("users", "User", extract.USER_FIELDS),
			("employees", "Employee", extract.EMPLOYEE_FIELDS),
			("applicants", "Job Applicant", extract.APPLICANT_FIELDS),
			("members", "VM Member", extract.MEMBER_FIELDS),
			("memberships", "VM Membership", extract.MEMBERSHIP_FIELDS),
			("membership_types", "VM Membership Type", extract.MEMBERSHIP_TYPE_FIELDS),
		]
		for key, dt, fields in specs:
			meta_payload = client.json("/api/method/frappe.desk.form.load.getdoctype", {"doctype": dt})
			meta = next(
				(
					d
					for d in meta_payload.get("docs", [])
					if d.get("doctype") == "DocType" and d.get("name") == dt
				),
				None,
			)
			if not meta:
				raise SourceError(f"Could not read metadata for {dt}.")
			columns = {f["fieldname"] for f in meta.get("fields", [])} | {
				"name",
				"creation",
				"modified",
				"docstatus",
			}
			snapshot["missing_columns"][dt] = sorted(set(fields) - columns)
			tables = {
				"users": extract.USER_TABLES,
				"employees": extract.EMPLOYEE_TABLES,
				"applicants": extract.APPLICANT_TABLES,
			}.get(key, [])
			allowed = set(fields) | set(tables) | {"name", "modified"}
			snapshot[key] = {d["name"]: {k: v for k, v in d.items() if k in allowed} for d in client.docs(dt)}
			if key == "users":
				snapshot[key] = {
					k: v for k, v in snapshot[key].items() if k not in ("Administrator", "Guest")
				}
			for field in meta.get("fields", []):
				if field.get("is_custom_field"):
					snapshot["custom_fields"].append({"dt": dt, **field})
		# Pull invoice fields only when the old app exposes the membership link.
		meta = client.json("/api/method/frappe.desk.form.load.getdoctype", {"doctype": "Sales Invoice"})
		fields = [
			f for d in meta.get("docs", []) if d.get("name") == "Sales Invoice" for f in d.get("fields", [])
		]
		if any(f.get("fieldname") == "membership" for f in fields):
			for row in client.docs(
				"Sales Invoice",
				[["docstatus", "=", 1], ["membership", "!=", ""]],
				["name", "membership", "posting_date", "status", "outstanding_amount", "grand_total"],
			):
				old = snapshot["invoices"].get(row["membership"])

				def rank(invoice):
					return (
						invoice.get("status") == "Paid",
						str(invoice.get("posting_date") or ""),
						invoice["name"],
					)

				if old is None or rank(row) > rank(old):
					snapshot["invoices"][row["membership"]] = row
		for row in client.docs(
			"File",
			[["attached_to_doctype", "in", list(extract.ATTACHED_TO)]],
			[
				"name",
				"file_url",
				"file_name",
				"is_private",
				"attached_to_doctype",
				"attached_to_name",
				"attached_to_field",
				"content_hash",
				"file_size",
			],
		):
			if row.get("file_url"):
				snapshot["files"].setdefault(row["file_url"], row)
		# Include embedded image/document URLs even when File.attached_to_* is blank.
		urls = set(snapshot["files"])

		def find_urls(value):
			if isinstance(value, dict):
				for v in value.values():
					find_urls(v)
			elif isinstance(value, list):
				for v in value:
					find_urls(v)
			elif isinstance(value, str) and value.startswith(("/files/", "/private/files/")):
				urls.add(value)

		for key, _, _ in specs:
			find_urls(snapshot[key])
		directory = workspace.path("captures", capture_id, "files", "placeholder")
		directory = os.path.dirname(directory)
		snapshot["captured_files"] = {url: client.download(url, directory) for url in sorted(urls)}
		target = workspace.path("captures", capture_id, "snapshot.json")
		workspace.write_json(target, snapshot)
		return {
			"capture_id": capture_id,
			"source": client.url,
			"counts": {key: len(snapshot[key]) for key, _, _ in specs},
			"files": len(urls),
			"passwords": "Not available through REST. New users require password resets.",
		}
	finally:
		client.close()


class SnapshotSource:
	def __init__(self, conf):
		capture_id = conf.get("browser_capture")
		if not capture_id or not capture_id.replace("-", "").isalnum():
			raise SourceError("Capture the old site's data in Migration Settings first.")
		with open(workspace.path("captures", capture_id, "snapshot.json")) as handle:
			self.data = json.load(handle)
		if self.data.get("history_storage"):
			from onerc_vmms_migrate.history_store import SQLiteHistory
			from onerc_vmms_migrate.bulk_source import sha256_file
			storage = self.data["history_storage"]
			path = workspace.path("captures", capture_id, "transfer.sqlite")
			if sha256_file(path) != storage["sqlite_sha256"]:
				raise SourceError("Captured history failed integrity verification. Resume or recapture it.")
			self.data["history"] = SQLiteHistory(path, storage["count"])
		self.db_name = self.data["source"]["db_name"]
		self.site_path = self.data["source"]["site_path"]
		if conf.get("preserve_passwords") and not self.data["source"].get("logins", {}).get("included"):
			raise SourceError("This capture does not preserve existing passwords. Take a fresh protected login capture before importing.")
		if self.data["source"].get("logins", {}).get("included"):
			from onerc_vmms_migrate.logins import load_capture

			directory = os.path.dirname(workspace.path("captures", capture_id, "snapshot.json"))
			self.data["password_hashes"] = load_capture(directory, self.data["source"])

	def __enter__(self):
		return self

	def __exit__(self, *args):
		pass

	def frappe_version(self):
		return self.data["source"]["apps"]

	def custom_fields(self):
		return self.data.get("custom_fields", [])

	def file_path(self, url):
		relative = self.data.get("captured_files", {}).get(url)
		if not relative:
			return None
		root = os.path.realpath(workspace.root())
		target = os.path.realpath(os.path.join(root, relative))
		if not target.startswith(root + os.sep):
			raise SourceError("Invalid captured attachment path.")
		return target if os.path.isfile(target) else None

	def verify_file(self, url, path):
		item = self.data.get("captured_file_manifest", {}).get(url)
		if not item:
			if self.data.get("source", {}).get("protocol") in (1, 2):
				raise SourceError("The captured file is missing its integrity manifest.")
			return
		from onerc_vmms_migrate.bulk_source import sha256_file

		if os.path.getsize(path) != item["size"] or sha256_file(path) != item["sha256"]:
			raise SourceError("A captured attachment failed integrity verification. Capture it again.")

	def file_name(self, url):
		return os.path.basename(unquote(urlsplit(url).path))
