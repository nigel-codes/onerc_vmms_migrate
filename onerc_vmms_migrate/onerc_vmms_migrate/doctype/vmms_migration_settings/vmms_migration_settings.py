import frappe
from frappe.model.document import Document


class VMMSMigrationSettings(Document):
	def validate(self):
		from onerc_vmms_migrate.browser import assert_idle
		from onerc_vmms_migrate.browser_source import validate_url
		from onerc_vmms_migrate.bulk_source import source_origin

		assert_idle()
		self.source_url = source_origin(self.source_url) if self.get("capture_mode") == "Bulk Export" else validate_url(self.source_url)
		if self.get("preserve_passwords") and self.get("capture_mode") != "Bulk Export":
			frappe.throw("Preserving passwords requires Bulk Export and the source companion app.")
		if not str(self.default_country_code or "").isdigit():
			frappe.throw("Enter a numeric country calling code, for example 254.")
		previous = self.get_doc_before_save()
		if previous and previous.browser_capture != self.browser_capture:
			frappe.throw("Captured source is managed by the migration process.")
		if (
			self.browser_capture
			and previous
			and any(previous.get(f) != self.get(f) for f in ("source_url", "api_key", "api_secret", "capture_mode", "preserve_passwords"))
		):
			frappe.throw("Clear the capture before changing the old site connection.")
