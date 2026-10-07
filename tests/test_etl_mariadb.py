# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Live ETL copy regressions, using only uniquely named disposable schemas.

Set CARLOS_TEST_MARIADB_SOCKET to enable. Temporary ledger files follow TMPDIR.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from types import SimpleNamespace

from carlos_ctl import o19etl, o19import


@unittest.skipUnless(os.environ.get("CARLOS_TEST_MARIADB_SOCKET")
                     and shutil.which("mariadb"),
                     "set CARLOS_TEST_MARIADB_SOCKET to run live SQL regressions")
class TestCopyMariaDB(unittest.TestCase):
    def setUp(self):
        self.base = ["mariadb", "--no-defaults", "--protocol=socket", "--user=root",
                     "--socket=" + os.environ["CARLOS_TEST_MARIADB_SOCKET"]]
        prefix = "carlos_copy_test_" + uuid.uuid4().hex
        self.src, self.dst = prefix + "_src", prefix + "_dst"
        for schema in (self.src, self.dst):
            self.plain("CREATE DATABASE `{0}`".format(schema))
            self.addCleanup(self.plain, "DROP DATABASE `{0}`".format(schema))
            self.plain("CREATE TABLE `{0}`.sample (id INT PRIMARY KEY, "
                       "name VARCHAR(40), UNIQUE KEY uq_name (name)) "
                       "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 "
                       "COLLATE=utf8mb4_general_ci".format(schema))
        # The dump need not carry the target's newer unique index.
        self.plain("ALTER TABLE `{0}`.sample DROP INDEX uq_name".format(self.src))
        self.ledger = tempfile.TemporaryDirectory(prefix="etl-copy-test-")
        self.addCleanup(self.ledger.cleanup)
        self.state = {}
        self.run = SimpleNamespace(
            query=o19import.make_etl_query(self.base, statement_timeout=10),
            plain=self.plain, src=self.src, dst=self.dst, arch=self.dst,
            state_dir=self.ledger.name, progress={"tables": {"sample": self.state}},
            counts={"copy": 0}, seed_group=set(), admin_user="unused", admin_pn=None)
        self.entry = {"class": "copy", "cols": ["id", "name"]}
        self.columns = o19etl.introspect_columns(self.plain, self.dst)["sample"]

    def plain(self, sql):
        cp = subprocess.run(self.base + ["--batch", "--skip-column-names"],
                            input=sql, text=True, capture_output=True, check=True,
                            timeout=20)
        return o19import.batch_rows(cp.stdout)

    def seed(self, values):
        self.plain("INSERT INTO `{0}`.sample VALUES {1}".format(self.src, values))

    def copy(self):
        o19etl.etl_copy_table(self.run, "sample", self.entry, self.state,
                             self.columns, set())

    def target_count(self):
        return int(self.plain("SELECT COUNT(*) FROM `{0}`.sample".format(self.dst))[0][0])

    def assert_collision_refused(self):
        with self.assertRaises(o19etl.QueryError) as raised:
            self.copy()
        self.assertIn("1062", str(raised.exception))
        self.assertEqual(self.target_count(), 0)
        persisted = o19etl.load_progress(self.ledger.name)["tables"]["sample"]
        self.assertTrue(persisted["started"])
        self.assertFalse(persisted.get("done", False))
        self.assertNotIn("done_through", persisted)
        self.assertEqual(self.run.counts["copy"], 0)

    def test_should_refuse_duplicate_unique_values_before_recording_completion(self):
        self.seed("(1,'same'),(2,'same')")
        self.assert_collision_refused()

    def test_should_refuse_target_collation_collisions(self):
        self.seed("(1,'Same'),(2,'same')")
        self.assert_collision_refused()

    def test_should_leave_failed_window_unconfirmed(self):
        self.entry["chunk_by"] = "id"
        self.seed("(1,'same'),(2,'same')")
        self.assert_collision_refused()

    def test_should_resume_refused_copy_after_source_correction(self):
        self.seed("(1,'same'),(2,'same')")
        self.assert_collision_refused()
        self.plain("UPDATE `{0}`.sample SET name='different' WHERE id=2".format(self.src))
        self.copy()
        self.assertEqual(self.target_count(), 2)
        self.assertTrue(self.state["done"])

    def test_should_copy_distinct_and_nullable_unique_values(self):
        self.seed("(1,'first'),(2,'second'),(3,NULL),(4,NULL)")
        self.copy()
        self.assertEqual(self.target_count(), 4)
        self.assertTrue(self.state["done"])

    def test_should_complete_an_empty_source(self):
        self.copy()
        self.assertEqual(self.target_count(), 0)
        self.assertTrue(self.state["done"])

    def test_should_preserve_a_completed_window_when_the_next_window_collides(self):
        self.entry["chunk_by"] = "id"
        last = o19etl.CHUNK_ROWS + 2
        self.seed("(1,'first'),(2,'second'),({0},'same'),({1},'same')".format(last - 1, last))
        with self.assertRaises(o19etl.QueryError):
            self.copy()
        persisted = o19etl.load_progress(self.ledger.name)["tables"]["sample"]
        self.assertEqual(persisted["done_through"], o19etl.CHUNK_ROWS)
        self.assertFalse(persisted.get("done", False))
        self.assertEqual(self.target_count(), 2)
        self.plain("UPDATE `{0}`.sample SET name='different' WHERE id={1}".format(self.src, last))
        self.copy()
        self.assertEqual(self.target_count(), 4)
        self.assertTrue(self.state["done"])

    def test_should_preserve_existing_target_rows_when_a_copy_collides(self):
        self.seed("(1,'same')")
        self.plain("INSERT INTO `{0}`.sample VALUES (9,'same')".format(self.dst))
        with self.assertRaises(o19etl.QueryError):
            self.copy()
        self.assertEqual(self.plain("SELECT id FROM `{0}`.sample".format(self.dst)), [["9"]])
        self.assertFalse(self.state.get("done", False))
