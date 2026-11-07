# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Live original/value/checkpoint regressions for historical Ontario billing."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
import uuid

from carlos_ctl import o19billing, o19etl, o19import
from .carlos_src import carlos_path


@unittest.skipUnless(os.environ.get("CARLOS_TEST_MARIADB_SOCKET") and shutil.which("mariadb"),
                     "set CARLOS_TEST_MARIADB_SOCKET for live billing regressions")
class TestBillingFilenamesMariaDB(unittest.TestCase):
    def setUp(self):
        """Create isolated source, destination and evidence schemas with real unique indexes."""
        self.client = ["mariadb", "--no-defaults", "--user=root", "--protocol=socket",
                       "--socket=" + os.environ["CARLOS_TEST_MARIADB_SOCKET"]]
        prefix = "billing_test_" + uuid.uuid4().hex
        self.src, self.dst, self.arch = (prefix + tail for tail in ("_src", "_dst", "_arch"))
        for schema in (self.src, self.dst, self.arch):
            self.plain("CREATE DATABASE " + o19billing.ident(schema))
            self.addCleanup(self.plain, "DROP DATABASE " + o19billing.ident(schema))
        self.writer = o19import.make_etl_query(self.client, statement_timeout=20)
        for table, (column, date, _token) in o19billing.TABLES.items():
            for schema in (self.src, self.dst):
                ddl = ("CREATE TABLE `{0}`.`{1}` (id INT PRIMARY KEY, `{2}` VARCHAR(50) NULL, "
                       "timestamp TIMESTAMP NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP, "
                       "payload TEXT NULL{3}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE={4}").format(
                           schema, table, column,
                           ", createdatetime DATETIME NULL" if date != "timestamp" else "",
                           "utf8mb4_bin" if schema == self.src else "utf8mb4_general_ci")
                self.plain(ddl)
            self.plain("ALTER TABLE `{0}`.`{1}` ADD UNIQUE KEY filename_uq (`{2}`)".format(self.dst, table, column))
        audit = Path(__file__).parent / "fixtures" / "billing_filename_import.sql"
        self.plain("USE " + o19billing.ident(self.dst) + "; " + audit.read_text())
        self.ledger = tempfile.TemporaryDirectory()
        self.addCleanup(self.ledger.cleanup)
        self.progress = {"tables": {}}

    def plain(self, sql):
        result = subprocess.run(self.client + ["--batch", "--skip-column-names"], input=sql,
                                text=True, capture_output=True, check=True, timeout=30)
        return o19import.batch_rows(result.stdout)

    def seed(self, table, rows):
        """Write known source values and timestamps without activating automatic timestamp updates."""
        column, date, _ = o19billing.TABLES[table]
        for row_id, filename, stamp in rows:
            value = "NULL" if filename is None else o19billing.literal(filename)
            when = "NULL" if stamp is None else o19billing.literal(stamp)
            fields = "id,`{0}`,`{1}`,payload".format(column, date)
            values = "{0},{1},{2},'clinical payload {0}'".format(row_id, value, when)
            if date != "timestamp":
                fields += ",timestamp"
                values += ", '2020-02-03 04:05:06'"
            self.writer("INSERT INTO `{0}`.`{1}` ({2}) VALUES ({3})".format(self.src, table, fields, values))

    def run_for(self, table, chunk=False):
        """Construct the real copy inputs with optional chunking and retained checkpoints."""
        src_info = o19etl.introspect_columns(self.plain, self.src)
        dst_info = o19etl.introspect_columns(self.plain, self.dst)
        columns = list(dst_info[table])
        entry = {"class": "copy", "cols": columns, "billing_filename": o19billing.RULE,
                 "value_exprs": {o19billing.TABLES[table][0]: o19billing.expression(table)}}
        if chunk:
            entry["chunk_by"] = "id"
        state = self.progress["tables"].setdefault(table, {})
        run = SimpleNamespace(query=self.writer, plain=self.plain, src=self.src, dst=self.dst,
                              arch=self.arch, src_info=src_info, state_dir=self.ledger.name,
                              progress=self.progress, counts={"copy": 0}, seed_group=set(),
                              admin_user="unused", admin_pn=None)
        return run, entry, state, dst_info[table]

    def copy(self, table, chunk=False):
        run, entry, state, columns = self.run_for(table, chunk)
        o19billing.prepare(run, table, entry, state, columns)
        o19etl.etl_copy_table(run, table, entry, state, columns, set())
        self.assertTrue(state["done"])
        self.assertEqual(o19billing.check_helper(self.plain, table, self.src, self.arch,
                         run.src_info[table], columns), [])
        self.assertEqual(o19billing.check_audit(self.plain, table, self.dst, self.arch), [])
        return run, entry, state, columns

    def values(self, table, schema=None):
        """Read identities, filenames and clinical payload from a chosen schema for comparison."""
        column = o19billing.TABLES[table][0]
        return self.plain("SELECT id, `{0}`, payload, timestamp FROM `{1}`.`{2}` ORDER BY id".format(
            column, schema or self.dst, table))

    def test_both_tables_keep_all_rows_originals_payload_and_timestamps(self):
        """Renaming collisions must preserve source rows and every unrelated destination value."""
        for table, (column, _date, token) in o19billing.TABLES.items():
            with self.subTest(table=table):
                self.seed(table, [(1, "same", "2020-01-01"), (2, "same", "2021-01-01")])
                before = self.values(table, self.src)
                self.copy(table)
                extension = ".html" if token == "html" else ".claim"
                after = self.values(table)
                self.assertEqual([row[1] for row in after],
                                 ["o19-" + token + "-2020-1-0" + extension,
                                  "o19-" + token + "-2021-2-0" + extension])
                self.assertEqual([(r[0], r[2:]) for r in after], [(r[0], r[2:]) for r in before])
                self.assertEqual(self.values(table, self.src), before)
                self.assertEqual(self.plain("SELECT original_filename, file_status FROM `{0}`.{1} "
                                 "WHERE source_table={2} ORDER BY source_id".format(
                                     self.dst, o19billing.AUDIT, o19billing.literal(table))),
                                 [["same", "ambiguous"], ["same", "ambiguous"]])
                self.assertEqual(self.plain("SELECT lastUpdateUser, YEAR(lastUpdateDate) > 0 "
                                 "FROM `{0}`.{1} WHERE source_table={2} ORDER BY source_id".format(
                                     self.dst, o19billing.AUDIT, o19billing.literal(table))),
                                 [["carlos-ctl/import-o19", "1"], ["carlos-ctl/import-o19", "1"]])

    def test_update_actor_is_verified_without_freezing_later_reviewers(self):
        """Missing provenance fails verification; a later named reviewer remains legitimate."""
        table = "billing_on_diskname"
        self.seed(table, [(1, "shared.001", "2020-01-01"), (2, "shared.001", "2021-01-01")])
        self.copy(table)
        self.plain("UPDATE `{0}`.{1} SET lastUpdateUser='' WHERE source_id=1".format(self.dst, o19billing.AUDIT))
        self.assertTrue(o19billing.check_audit(self.plain, table, self.dst, self.arch))
        self.plain("UPDATE `{0}`.{1} SET lastUpdateUser='billing-reviewer', "
                   "lastUpdateDate=CURRENT_TIMESTAMP WHERE source_id=1".format(self.dst, o19billing.AUDIT))
        self.assertEqual(o19billing.check_audit(self.plain, table, self.dst, self.arch), [])

    def test_target_collation_not_source_collation_decides_groups(self):
        table = "billing_on_diskname"
        self.seed(table, [(1, "Éfile", "2021-01-01"), (2, "efile ", "2021-01-01"),
                          (3, "EFILE", "2021-01-01")])
        self.copy(table)
        self.assertEqual(self.plain("SELECT DISTINCT group_size FROM `{0}`.`{1}`".format(
            self.arch, o19billing.helper(table))), [["3"]])

    def test_nulls_zero_and_negative_ids_and_unknown_dates_survive(self):
        """Sentinel identities, nullable filenames and unknown years must survive allocation."""
        table = "billing_on_diskname"
        self.seed(table, [(-3, None, None), (0, None, None), (1, "same", None),
                          (2, "same", "0000-00-00")])
        self.copy(table, chunk=True)
        self.assertEqual([row[:2] for row in self.values(table)],
                         [["-3", "NULL"], ["0", "NULL"],
                          ["1", "o19-claim-x-1-0.claim"], ["2", "o19-claim-0-2-0.claim"]])

    def test_preexisting_candidates_are_reserved_including_case_and_trailing_space(self):
        table = "billing_on_diskname"
        self.seed(table, [(1, "same", "2021-01-01"), (2, "same", "2021-01-01"),
                          (3, "O19-CLAIM-2021-2-0.CLAIM ", "2022-01-01"),
                          (4, "o19-claim-2021-2-1.claim", "2022-01-01")])
        self.copy(table)
        self.assertEqual(self.values(table)[1][1], "o19-claim-2021-2-2.claim")
        self.assertEqual(self.values(table)[2][1], "O19-CLAIM-2021-2-0.CLAIM ")

    def test_fifty_character_original_is_preserved_byte_for_byte(self):
        table = "billing_on_filename"
        name = "é" * 50
        self.seed(table, [(1, name, "2020-01-01"), (2, name, "2021-01-01")])
        self.copy(table)
        self.assertEqual(self.plain("SELECT HEX(original_filename) FROM `{0}`.`{1}` ORDER BY row_id".format(
            self.arch, o19billing.helper(table))), [[name.encode().hex().upper()]] * 2)

    def test_claim_candidates_also_reserve_original_html_names(self):
        table = "billing_on_diskname"
        self.seed(table, [(1, "same", "2021-01-01"), (2, "same", "2021-01-01")])
        self.seed("billing_on_filename", [(3, "o19-claim-2021-2-0.claim", "2021-01-01")])
        self.copy(table)
        self.assertEqual(self.values(table)[1][1], "o19-claim-2021-2-1.claim")

    def test_shared_basename_across_tables_is_ambiguous_without_within_table_duplicates(self):
        """One reference in each table must still create an ambiguous shared-file cohort."""
        tables = list(o19billing.TABLES)
        for table in tables:
            self.seed(table, [(1, "shared.001", "2021-01-01")])
        destination = o19etl.introspect_columns(self.plain, self.dst)
        sources = {table: table for table in tables}
        for table in tables:
            info = destination[table][o19billing.TABLES[table][0]]
            self.assertEqual(o19billing.collision_count(
                self.plain, table, self.src, info, source_tables=sources), 1)
            self.copy(table)
        self.assertEqual(self.values(tables[0])[0][1], "o19-claim-2021-1-0.claim")
        self.assertEqual(self.values(tables[1])[0][1], "o19-html-2021-1-0.html")
        self.assertEqual(self.plain("SELECT source_table, source_id, original_filename, group_size, file_status "
                                   "FROM `{0}`.{1} ORDER BY source_table".format(self.dst, o19billing.AUDIT)),
                         [[table, "1", "shared.001", "2", "ambiguous"] for table in tables])
        for table in tables:
            self.assertEqual(self.values(table, self.src)[0][1], "shared.001")

    def test_shared_namespace_uses_destination_collation_and_restored_case_in_preflight(self):
        """Cross-table counts must use target equality and the actual restored table names."""
        disk, html = o19billing.TABLES
        self.seed(disk, [(1, "Éfile", "2020-01-01")])
        self.seed(html, [(2, "efile ", "2021-01-01")])
        destination = o19etl.introspect_columns(self.plain, self.dst)
        actual = {disk: "Billing_on_diskname", html: "BILLING_ON_FILENAME"}
        for table, spelling in actual.items():
            self.plain("RENAME TABLE `{0}`.`{1}` TO `{0}`.`{2}`".format(self.src, table, spelling))
        for table, spelling in actual.items():
            self.assertEqual(o19billing.collision_count(
                self.plain, table, self.src, destination[table][o19billing.TABLES[table][0]],
                spelling, actual), 1)
        for table, spelling in actual.items():
            self.plain("RENAME TABLE `{0}`.`{1}` TO `{0}`.`{2}`".format(self.src, spelling, table))
        for table in actual:
            self.copy(table)
        self.assertEqual(self.values(disk)[0][1], "o19-claim-2020-1-0.claim")
        self.assertEqual(self.values(html)[0][1], "o19-html-2021-2-0.html")

    def test_preflight_counts_mixed_case_restored_table_before_normalization(self):
        table = "billing_on_diskname"
        self.seed(table, [(1, "same", "2020-01-01"), (2, "same", "2021-01-01")])
        info = o19etl.introspect_columns(self.plain, self.dst)[table]["ohipfilename"]
        self.plain("RENAME TABLE `{0}`.`{1}` TO `{0}`.`Billing_on_diskname`".format(self.src, table))
        self.assertEqual(o19billing.collision_count(
            self.plain, table, self.src, info, "Billing_on_diskname"), 2)

    def test_resume_after_insert_commits_without_completion_checkpoint(self):
        """A lost commit acknowledgement must not change filenames or lose rows on retry."""
        table = "billing_on_diskname"
        self.seed(table, [(1, "same", "2020-01-01"), (50001, "same", "2021-01-01")])
        run, entry, state, columns = self.run_for(table, chunk=True)
        o19billing.prepare(run, table, entry, state, columns)
        original = run.query

        def interrupted(sql):
            result = original(sql)
            if sql.startswith("INSERT INTO `{0}`.`{1}`".format(self.dst, table)):
                raise o19etl.QueryError("simulated lost acknowledgement after commit")
            return result

        run.query = interrupted
        with self.assertRaises(o19etl.QueryError):
            o19etl.etl_copy_table(run, table, entry, state, columns, set())
        self.assertFalse(state.get("done"))
        self.assertNotIn("done_through", state)
        self.copy(table, chunk=True)
        self.assertEqual(len(self.values(table)), 2)

    def resume_mapping_publication(self, after):
        """Interrupt either side of the atomic rename and prove deterministic same-version recovery."""
        table = "billing_on_diskname"
        self.seed(table, [(1, "same", "2020-01-01"), (2, "same", "2021-01-01")])
        run, entry, state, columns = self.run_for(table)
        original = run.query

        def interrupted(sql):
            if sql.startswith("RENAME TABLE "):
                if after:
                    original(sql)
                raise o19etl.QueryError("simulated interruption around atomic mapping publication")
            return original(sql)

        run.query = interrupted
        with self.assertRaises(o19etl.QueryError):
            o19billing.prepare(run, table, entry, state, columns)
        self.assertTrue(o19etl.load_progress(run.state_dir)["tables"][table]["billing_started"])
        self.assertEqual(self.values(table), [])
        self.copy(table)
        self.assertEqual([row[1] for row in self.values(table)],
                         ["o19-claim-2020-1-0.claim", "o19-claim-2021-2-0.claim"])

    def test_resume_before_mapping_publication(self):
        self.resume_mapping_publication(after=False)

    def test_resume_after_mapping_publication_without_acknowledgement(self):
        self.resume_mapping_publication(after=True)

    def test_changed_original_is_detected_even_when_live_audit_is_changed_to_match(self):
        table = "billing_on_diskname"
        self.seed(table, [(1, "same", "2020-01-01"), (2, "same", "2021-01-01")])
        run, entry, state, columns = self.copy(table)
        self.plain("UPDATE `{0}`.`{1}` SET original_filename='forged' WHERE row_id=1".format(
            self.arch, o19billing.helper(table)))
        self.plain("UPDATE `{0}`.{1} SET original_filename='forged' WHERE source_id=1".format(
            self.dst, o19billing.AUDIT))
        self.assertEqual(o19billing.check_audit(self.plain, table, self.dst, self.arch), [])
        with self.assertRaises(o19etl.QueryError):
            o19billing.prepare(run, table, entry, state, columns)

    def test_missing_audit_row_refuses_checkpoint_and_verification(self):
        table = "billing_on_diskname"
        self.seed(table, [(1, "same", "2020-01-01"), (2, "same", "2021-01-01")])
        run, entry, state, columns = self.copy(table)
        self.plain("DELETE FROM `{0}`.{1} WHERE source_id=1".format(self.dst, o19billing.AUDIT))
        with self.assertRaises(o19etl.QueryError):
            o19billing.prepare(run, table, entry, state, columns)

    def test_live_missing_or_changed_submission_cannot_be_waived_as_content(self):
        table = "billing_on_diskname"
        self.seed(table, [(1, "same", "2020-01-01"), (2, "same", "2021-01-01")])
        run, entry, state, columns = self.copy(table)
        self.plain("DELETE FROM `{0}`.`{1}` WHERE id=1".format(self.dst, table))
        dst_info = o19etl.introspect_columns(self.plain, self.dst)
        _ok, bad = o19billing.verify(self.plain, self.src, self.dst, self.arch,
                                    run.src_info, dst_info, {table: entry})
        self.assertTrue(bad)
        # Cleanup proves retained originals/audit after go-live; it must not
        # freeze the clinical tables the application is now allowed to edit.
        _ok, bad = o19billing.verify(self.plain, self.src, self.dst, self.arch,
                                    run.src_info, dst_info, {table: entry}, live=False)
        self.assertEqual(bad, [])

    def test_incomplete_reconciliation_metadata_is_not_a_passing_audit(self):
        table = "billing_on_diskname"
        self.seed(table, [(1, "same", "2020-01-01"), (2, "same", "2021-01-01")])
        self.copy(table)
        self.plain("UPDATE `{0}`.{1} SET file_status='reconciled' WHERE source_id=1".format(
            self.dst, o19billing.AUDIT))
        self.assertTrue(o19billing.check_audit(self.plain, table, self.dst, self.arch))
        self.plain("UPDATE `{0}`.{1} SET file_sha256=REPEAT('a',64), reconciled_by='reviewer', "
                   "reconciled_at='2021-02-03 04:05:06' WHERE source_id=1".format(self.dst, o19billing.AUDIT))
        self.assertEqual(o19billing.check_audit(self.plain, table, self.dst, self.arch), [])

    def test_canonical_audit_fixture_matches_application_migration(self):
        """The installed-package test schema must match the paired application migration bytes."""
        application = Path(carlos_path("database", "mysql", "migration", "common",
                                      "V1.0.59.33__audit_imported_billing_filenames.sql"))
        if not application.is_file():
            self.skipTest("set CARLOS_SRC to check the paired schema contract")
        fixture = Path(__file__).parent / "fixtures" / "billing_filename_import.sql"
        self.assertEqual(fixture.read_bytes(), application.read_bytes())
