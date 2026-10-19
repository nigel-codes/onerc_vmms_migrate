"""Exercise migration coverage with Frappe's real SELECT validation and parser."""
import sqlite3
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe
from frappe.database.query import SQLFunctionParser, _validate_select_field
from pypika import Field

from onerc_vmms_migrate import full_run


class TestArchiveCoverage(TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("CREATE TABLE archive (name TEXT, source_doctype TEXT, conversion_status TEXT)")

    def get_all(self, doctype, fields, group_by, **kwargs):
        engine = SimpleNamespace(function_aliases=set())
        parser = SQLFunctionParser(engine)
        selected = []
        for field in fields:
            if isinstance(field, str):
                _validate_select_field(field)
                selected.append(Field(field).get_sql(quote_char='"'))
            else:
                selected.append(parser.parse_function(field).get_sql(quote_char='"', with_alias=True))
        grouped = ", ".join(Field(field.strip()).get_sql(quote_char='"') for field in group_by.split(","))
        rows = self.connection.execute("SELECT " + ", ".join(selected) + " FROM archive GROUP BY " + grouped)
        return [frappe._dict(dict(row)) for row in rows]

    def coverage(self):
        def throw(message, exc=frappe.ValidationError, **kwargs):
            raise exc(message)
        with patch.object(frappe, "get_all", side_effect=self.get_all), patch.object(frappe, "throw", side_effect=throw), patch("frappe.database.query._", side_effect=lambda message: message):
            return full_run.coverage({})

    def test_counts_native_conversion_separately_from_retained_history(self):
        self.connection.executemany("INSERT INTO archive VALUES (?, ?, ?)", [
            ("1", "Employee", "Converted"),
            ("2", "Employee", "Converted"),
            ("3", "Employee", "Retained"),
            ("4", "Sales Invoice", "Retained"),
        ])
        self.assertEqual(self.coverage(), {
            "Employee": {"Converted": 2, "Retained": 1},
            "Sales Invoice": {"Retained": 1},
        })

    def test_empty_archive_has_no_invented_counts(self):
        self.assertEqual(self.coverage(), {})
