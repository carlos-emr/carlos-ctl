# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Compatibility and irreversible-cleanup gates for billing import evidence."""
import tempfile
import contextlib
import unittest
from unittest import mock

from carlos_ctl import o19billing, o19etl, o19import, o19manifest


class TestBillingContract(unittest.TestCase):
    def test_host_preflight_uses_restored_case_for_filename_counts(self):
        """Host preflight must count the complete namespace before table-case normalization."""
        sources = {"billing_on_diskname": "Billing_on_diskname", "billing_on_filename": "BILLING_ON_FILENAME"}
        entries = {table: {"class": "copy", "billing_filename": o19billing.RULE,
                           "value_exprs": {o19billing.TABLES[table][0]: o19billing.expression(table)}}
                   for table in sources}
        report = {"findings": [], "verdict": "go", "exit_code": 0,
                  "acknowledged": [], "required_accepts": []}
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(o19import.o19map_schema, "TABLES", entries))
            stack.enter_context(mock.patch.object(o19import.o19_preflight, "run_checks", return_value=report))
            stack.enter_context(mock.patch.object(o19import.o19_preflight, "render_text", return_value=""))
            stack.enter_context(mock.patch.object(o19etl, "schema_tables", return_value=list(sources.values())))
            stack.enter_context(mock.patch.object(o19etl, "introspect_columns", return_value={
                table: {o19billing.TABLES[table][0]: {}} for table in sources}))
            count = stack.enter_context(mock.patch.object(o19billing, "collision_count", return_value=1))
            stack.enter_context(mock.patch.object(o19import, "report_content_transfer"))
            query = mock.Mock()
            o19import.run_p2({"query": query, "state": {}, "state_dir": directory,
                              "province": "on", "target_db": "target", "accepted": set(), "dry_run": True})
            self.assertEqual(count.call_args_list, [
                mock.call(query, table, o19import.STAGING_SCHEMA, {}, actual, sources)
                for table, actual in sources.items()])
            self.assertTrue(any(f["id"] == "historical-billing-filenames" for f in report["findings"]))

    def test_a_previous_import_audit_is_not_a_pristine_target(self):
        self.assertTrue(o19import.pristine_violations({o19billing.AUDIT: 1}))
        self.assertEqual(o19import.pristine_violations({o19billing.AUDIT: 0}), [])

    def test_missing_or_changed_rule_is_refused_for_an_ontario_source(self):
        """A copy without the exact audited rule must fail before dependent writes."""
        table = "billing_on_diskname"
        entry = {"class": "copy", "billing_filename": o19billing.RULE,
                 "value_exprs": {"ohipfilename": o19billing.expression(table)}}
        self.assertEqual(o19billing.manifest_problems({table: entry}, [table], "on"), [])
        for change in ({"class": "reference"}, {"billing_filename": "unknown"}, {"value_exprs": {}}):
            with self.subTest(change=change):
                self.assertTrue(o19billing.manifest_problems({table: dict(entry, **change)}, [table], "on"))

    def test_archived_ontario_tables_in_a_bc_profile_do_not_require_copy_helpers(self):
        """BC archives ignore Ontario copy requirements even on a mixed development schema."""
        self.assertEqual(o19billing.manifest_problems(
            {"billing_on_diskname": {"class": "archive"}}, ["billing_on_diskname"], "bc"), [])
        tables = {name: {} for name in o19billing.TABLES}
        query = mock.Mock(side_effect=AssertionError("archived tables need no filename or audit queries"))
        self.assertEqual(o19billing.precheck(query, "src", tables, tables, {}, province="bc"), [])
        entries = {name: {"class": "archive"} for name in tables}
        self.assertEqual(o19billing.precheck(query, "src", tables, tables, entries, province="bc"), [])
        self.assertTrue(o19billing.precheck(query, "src", tables, tables, entries, province="on"))

    def test_bc_explicit_copy_still_requires_the_audited_mapping(self):
        """A BC override that requests a copy must not bypass the billing contract."""
        table = "billing_on_diskname"
        query = mock.Mock(side_effect=AssertionError("invalid column shape must fail before querying"))
        self.assertTrue(o19billing.precheck(query, "src", {table: {}}, {table: {}},
                                           {table: {"class": "copy"}}, province="bc"))

    def test_schema_envelope_requires_helper_aware_cli_protocol(self):
        self.assertEqual(o19manifest.manifest_format("o19map-schema"), 2)
        self.assertEqual(o19manifest.manifest_format("o19map-props"), 1)
        self.assertEqual(o19manifest.manifest_format("o19-preflight"), 1)

    def test_audit_provenance_columns_are_required_before_copy(self):
        """A target missing either required update-audit column must fail before writes."""
        table = "billing_on_diskname"
        columns = {"id": {"type": "int"}, "ohipfilename": {
            "char_len": 50, "charset": "utf8mb4", "collation": "utf8mb4_general_ci"}}
        entry = {"class": "copy", "billing_filename": o19billing.RULE,
                 "value_exprs": {"ohipfilename": o19billing.expression(table)}}
        query = mock.Mock(return_value=[["0"]])
        audit = {column: {} for column in o19billing.AUDIT_COLUMNS}
        destination = {table: columns, o19billing.AUDIT: audit}
        self.assertEqual(o19billing.precheck(query, "src", destination, {table: columns}, {table: entry}), [])
        for column in ("lastUpdateUser", "lastUpdateDate"):
            with self.subTest(column=column):
                destination[o19billing.AUDIT] = {key: value for key, value in audit.items() if key != column}
                self.assertTrue(any("audit schema" in problem for problem in o19billing.precheck(
                    query, "src", destination, {table: columns}, {table: entry})))

    def test_helper_names_cannot_be_replaced_by_a_vendor_table(self):
        self.assertTrue(o19etl.reserved_helper_names([o19billing.helper("billing_on_filename")]))

    def test_cleanup_rechecks_billing_originals_before_staging_can_be_dropped(self):
        with mock.patch.object(o19etl, "preserved_parity", return_value=([], [])), \
                mock.patch.object(o19etl, "preserved_content_parity", return_value=([], [])), \
                mock.patch.object(o19etl, "introspect_columns", return_value={}), \
                mock.patch.object(o19billing, "verify", return_value=([], ["billing audit differs"])):
            _ok, bad = o19import._cleanup_home_parity({"query": mock.Mock(), "target_db": "target"})
        self.assertEqual(bad, ["billing audit differs"])
        self.assertIsNotNone(o19import.cleanup_data_refusal(True, bad))

    def test_inventory_is_inactive_when_no_billing_mapping_was_written(self):
        with tempfile.TemporaryDirectory() as directory:
            query = mock.Mock(side_effect=AssertionError("no billing SQL expected"))
            self.assertEqual(o19billing.inventory({"query": query, "state_dir": directory}, directory), [])

    def test_content_migration_acceptance_cannot_waive_missing_originals(self):
        """A content acknowledgement must never waive structural evidence loss."""
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            for function in ("row_parity", "preserved_parity", "preserved_content_parity",
                             "copy_content_parity", "merge_content_parity", "archived_column_parity"):
                stack.enter_context(mock.patch.object(o19etl, function, return_value=([], [])))
            stack.enter_context(mock.patch.object(o19etl, "introspect_columns", return_value={}))
            stack.enter_context(mock.patch.object(o19billing, "verify", return_value=([], ["missing billing originals"])))
            _ok, bad = o19import._row_parity({"state_dir": directory, "query": mock.Mock(),
                                             "target_db": "target", "archive_schema": "archive",
                                             "accepted": {"content-migration"}})
        self.assertEqual(bad, ["missing billing originals"])
