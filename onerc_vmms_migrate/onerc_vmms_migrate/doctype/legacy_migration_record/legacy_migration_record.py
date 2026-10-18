# Copyright (c) 2026, Nigel and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class LegacyMigrationRecord(Document):
	def validate(self):
		# One row per old record per kind of new record. An Employee becomes both
		# a volunteer and the application that makes it Active, so the pair is
		# the key, not the old name alone.
		clash = frappe.db.exists(
			self.doctype,
			{
				"source_doctype": self.source_doctype,
				"source_name": self.source_name,
				"target_doctype": self.target_doctype,
				"name": ("!=", self.name),
			},
		)
		if clash:
			frappe.throw(
				f"{self.source_doctype} {self.source_name} is already recorded as migrated to a {self.target_doctype}."
			)
