# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Only an untouched import may adopt a lossless schema widening."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from carlos_ctl import o19etl, o19import, schema_contract


def metadata(kind="varchar(30)"):
    result = {name: [] for name in schema_contract.FIELDS}
    result["columns"] = [["favorites", "BN", kind, "YES", "NULL", "",
                          "utf8mb4", "utf8mb4_general_ci", "", 2]]
    return result


class TestWideningTransition(unittest.TestCase):
    def test_accepts_only_capacity_growth_with_other_attributes_identical(self):
        self.assertEqual(schema_contract.widening_columns(
            metadata("varchar(255)"), metadata()), ["favorites.BN"])
        self.assertEqual(schema_contract.widening_columns(
            metadata("mediumtext"), metadata("text")), ["favorites.BN"])
        self.assertEqual(schema_contract.widening_columns(
            metadata("mediumtext"), metadata("varchar(65535)")), ["favorites.BN"])

    def test_rejects_shrinking_and_unrelated_schema_drift(self):
        for wanted in ("varchar(20)", "int", "varbinary(255)"):
            self.assertEqual(schema_contract.widening_columns(
                metadata(wanted), metadata()), [])
        self.assertEqual(schema_contract.widening_columns(
            metadata("char(255)"), metadata()), [])
        for index, changed in ((3, "NO"), (7, "utf8mb4_bin"), (9, 3)):
            actual = metadata()
            actual["columns"][0][index] = changed
            self.assertEqual(schema_contract.widening_columns(
                metadata("varchar(255)"), actual), [])

    def test_text_to_varchar_is_not_a_generic_safe_upgrade(self):
        self.assertEqual(schema_contract.widening_columns(
            metadata("varchar(65535)"), metadata("text")), [])


class TestPreCopyRecovery(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.ctx = {"state_dir": self.directory.name, "resume": True,
                    "state": {"inputs": {"schema_map_version": "old",
                                          "target_contract_sha256": "old"}},
                    "schema_contract": {"schema": metadata("varchar(255)")},
                    "target_contract_sha256": "new", "contract_refresh": True,
                    "target_db": "carlos",
                    "query": lambda sql: [[o19import.o19map_schema.SEED_USER_NAME]]}

    def test_known_consent_transition_requires_exact_recorded_and_physical_contract(self):
        legacy = {"schema": metadata()}
        self.ctx["schema_contract"]["schema"]["indexes"] = [["new Consent constraint"]]
        self.ctx["previous_contract_sha256"] = hashlib.sha256(
            json.dumps(legacy, sort_keys=True).encode()).hexdigest()
        with mock.patch.object(o19import.HOST, "pre_copy_schema_contract", return_value=legacy), \
                mock.patch.object(o19import, "gather_copy_counts", return_value={}), \
                mock.patch.object(o19import, "startup_row_counts", return_value={}), \
                mock.patch.object(o19import, "pristine_violations", return_value=[]), \
                mock.patch.object(o19import.HOST, "backup_configured", return_value=True), \
                mock.patch.object(o19import.HOST, "pre_import_backup", return_value=(True, "")), \
                mock.patch.object(o19import.HOST, "import_schema_upgrade", return_value=0) as migrate:
            o19import.prepare_pre_copy_upgrade(self.ctx, metadata())
            migrate.assert_called_once()
            migrate.reset_mock()
            self.ctx["previous_contract_sha256"] = "different package"
            o19import.prepare_pre_copy_upgrade(self.ctx, metadata())
            migrate.assert_not_called()

    def test_failed_backup_never_runs_migrations_or_changes_binding(self):
        with mock.patch.object(o19import, "gather_copy_counts", return_value={}), \
                mock.patch.object(o19import, "startup_row_counts", return_value={}), \
                mock.patch.object(o19import, "pristine_violations", return_value=[]), \
                mock.patch.object(o19import.HOST, "backup_configured", return_value=True), \
                mock.patch.object(o19import.HOST, "pre_import_backup", return_value=(False, "test failure")), \
                mock.patch.object(o19import.HOST, "import_schema_upgrade") as migrate, \
                self.assertRaises(SystemExit):
            o19import.prepare_pre_copy_upgrade(self.ctx, metadata())
        migrate.assert_not_called()
        self.assertEqual(self.ctx["state"]["inputs"]["target_contract_sha256"], "old")

    def test_source_set_is_updated_and_invalid_source_set_cannot_rebind(self):
        path = Path(self.directory.name) / "source-set.json"
        path.write_text('{"target_contract_sha256":"old","sources":["retained"]}')
        o19import.commit_pre_copy_contract(self.ctx)
        self.assertEqual(json.loads(path.read_text()),
                         {"target_contract_sha256": "new", "sources": ["retained"]})
        self.ctx["state"]["inputs"]["target_contract_sha256"] = "old"
        path.write_text("broken JSON")
        with self.assertRaises(SystemExit):
            o19import.commit_pre_copy_contract(self.ctx)
        self.assertEqual(self.ctx["state"]["inputs"]["target_contract_sha256"], "old")

    def test_backup_and_migrate_only_after_pristine_checks(self):
        calls = []
        with mock.patch.object(o19import, "gather_copy_counts", return_value={}), \
                mock.patch.object(o19import, "startup_row_counts", return_value={}), \
                mock.patch.object(o19import, "pristine_violations", return_value=[]), \
                mock.patch.object(o19import.HOST, "backup_configured", return_value=True), \
                mock.patch.object(o19import.HOST, "pre_import_backup",
                                  side_effect=lambda: (calls.append("backup") or True, "")), \
                mock.patch.object(o19import.HOST, "import_schema_upgrade",
                                  side_effect=lambda: calls.append("migrate") or 0):
            o19import.prepare_pre_copy_upgrade(self.ctx, metadata())
        self.assertEqual(calls, ["backup", "migrate"])
        # The old binding stands until P0 verifies the upgraded schema.
        self.assertEqual(self.ctx["state"]["inputs"]["target_contract_sha256"], "old")
        o19import.commit_pre_copy_contract(self.ctx)
        self.assertEqual(self.ctx["state"]["inputs"]["target_contract_sha256"], "new")
        self.assertEqual(len(self.ctx["state"]["package_recoveries"]), 1)

    def test_partial_copy_never_runs_schema_upgrade(self):
        o19etl.save_progress(self.directory.name,
                             {"tables": {"favorites": {"started": True}}})
        with mock.patch.object(o19import.HOST, "import_schema_upgrade") as migrate:
            o19import.prepare_pre_copy_upgrade(self.ctx, metadata())
        migrate.assert_not_called()

    def test_target_with_clinic_rows_refuses_before_backup_or_migration(self):
        with mock.patch.object(o19import, "gather_copy_counts", return_value={}), \
                mock.patch.object(o19import, "startup_row_counts", return_value={}), \
                mock.patch.object(o19import, "pristine_violations", return_value=["clinic rows"]), \
                mock.patch.object(o19import.HOST, "pre_import_backup") as backup, \
                mock.patch.object(o19import.HOST, "import_schema_upgrade") as migrate, \
                self.assertRaises(SystemExit):
            o19import.prepare_pre_copy_upgrade(self.ctx, metadata())
        backup.assert_not_called()
        migrate.assert_not_called()

    def test_rename_only_ledger_may_rebind_but_a_copy_ledger_may_not(self):
        progress = {"tables": {}, "schema_map_version": "old",
                    "renamed_tables": ["consent -> Consent"]}
        o19etl.save_progress(self.directory.name, progress)
        current = o19etl.load_progress(self.directory.name, schema_map_version="new")
        self.assertEqual(current["schema_map_version"], "new")
        self.assertEqual(current["renamed_tables"], progress["renamed_tables"])
        copied = copy.deepcopy(progress)
        copied["tables"]["Consent"] = {"started": True}
        o19etl.save_progress(self.directory.name, copied)
        with self.assertRaises(SystemExit):
            o19etl.load_progress(self.directory.name, schema_map_version="new")
