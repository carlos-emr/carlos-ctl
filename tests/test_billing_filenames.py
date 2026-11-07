# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Compatibility and irreversible-cleanup gates for billing import evidence."""
import tempfile
import contextlib
import unittest
from unittest import mock

from carlos_ctl import o19billing, o19etl, o19import, o19manifest


class TestBillingContract(unittest.TestCase):
    def test_a_previous_import_audit_is_not_a_pristine_target(self):
        self.assertTrue(o19import.pristine_violations({o19billing.AUDIT: 1}))
        self.assertEqual(o19import.pristine_violations({o19billing.AUDIT: 0}), [])

    def test_missing_or_changed_rule_is_refused_for_an_ontario_source(self):
        table = "billing_on_diskname"
        entry = {"class": "copy", "billing_filename": o19billing.RULE,
                 "value_exprs": {"ohipfilename": o19billing.expression(table)}}
        self.assertEqual(o19billing.manifest_problems({table: entry}, [table], "on"), [])
        for change in ({"class": "reference"}, {"billing_filename": "unknown"}, {"value_exprs": {}}):
            with self.subTest(change=change):
                self.assertTrue(o19billing.manifest_problems({table: dict(entry, **change)}, [table], "on"))

    def test_archived_ontario_tables_in_a_bc_profile_do_not_require_copy_helpers(self):
        self.assertEqual(o19billing.manifest_problems(
            {"billing_on_diskname": {"class": "archive"}}, ["billing_on_diskname"], "bc"), [])

    def test_schema_envelope_requires_helper_aware_cli_protocol(self):
        self.assertEqual(o19manifest.manifest_format("o19map-schema"), 2)
        self.assertEqual(o19manifest.manifest_format("o19map-props"), 1)
        self.assertEqual(o19manifest.manifest_format("o19-preflight"), 1)

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
