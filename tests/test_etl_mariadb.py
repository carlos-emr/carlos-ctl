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


@unittest.skipUnless(os.environ.get("CARLOS_TEST_MARIADB_SOCKET") and shutil.which("mariadb"),
                     "set CARLOS_TEST_MARIADB_SOCKET to run live OAuth migration checks")
class TestOAuthGrantMariaDB(unittest.TestCase):
    def setUp(self):
        self.base = ["mariadb", "--no-defaults", "--protocol=socket", "--user=root",
                     "--socket=" + os.environ["CARLOS_TEST_MARIADB_SOCKET"]]
        name = "carlos_oauth_test_" + uuid.uuid4().hex
        self.src, self.dst = name + "_src", name + "_dst"
        for schema in (self.src, self.dst):
            self.plain("CREATE DATABASE `{0}`".format(schema))
            self.addCleanup(self.plain, "DROP DATABASE `{0}`".format(schema))
            self.plain("CREATE TABLE `{0}`.ServiceClient (id INT PRIMARY KEY, name VARCHAR(255), "
                       "clientKey VARCHAR(255), clientSecret VARCHAR(255), uri VARCHAR(255), "
                       "dateCreated TIMESTAMP NULL, lifetime INT NULL)".format(schema))
            self.plain("CREATE TABLE `{0}`.ServiceAccessToken (id INT PRIMARY KEY, clientId INT, "
                       "tokenId VARCHAR(255), tokenSecret VARCHAR(255), issued INT, lifetime INT, "
                       "providerNo VARCHAR(25), scopes VARCHAR(255), dateCreated TIMESTAMP NULL)".format(schema))
        self.ledger = tempfile.TemporaryDirectory(prefix="oauth-import-test-")
        self.addCleanup(self.ledger.cleanup)
        self.run = SimpleNamespace(query=o19import.make_etl_query(self.base, statement_timeout=10),
            plain=self.plain, src=self.src, dst=self.dst, arch=self.dst,
            state_dir=self.ledger.name, progress={"tables": {}}, counts={"copy": 0},
            seed_group=set(), admin_user="unused", admin_pn=None)
        self.plain("INSERT INTO `{0}`.ServiceClient VALUES "
                   "(7,'fixture','fixture-o19-consumer','fixture-o19-consumer-secret',"
                   "'https://integration.example/callback','2019-02-12 19:33:20',-1)".format(self.src))
        self.plain("INSERT INTO `{0}`.ServiceAccessToken VALUES "
                   "(1,7,'fixture-o19-token','fixture-o19-token-secret',1550000000,-1,'999998','demographics.read',NULL),"
                   "(2,7,'fixture-finite','fixture-finite-secret',1550000000,86400,'999998','demographics.read',NULL),"
                   "(3,7,'fixture-expired','fixture-expired-secret',1,1,'999998','',NULL)".format(self.src))

    def plain(self, sql):
        cp = subprocess.run(self.base + ["--batch", "--skip-column-names"], input=sql,
                            text=True, capture_output=True, check=True, timeout=20)
        return o19import.batch_rows(cp.stdout)

    def copy(self, table):
        from carlos_ctl import o19map_schema
        entry = o19map_schema.TABLES[table]
        self.assertEqual(entry["class"], "copy")
        source = o19etl.introspect_columns(self.plain, self.src)[table]
        entry, _ = o19etl.effective_entry(table, entry, source)
        target = o19etl.introspect_columns(self.plain, self.dst)[table]
        state = self.run.progress["tables"].setdefault(table, {})
        o19etl.etl_copy_table(self.run, table, entry, state, target, set())

    def test_should_preserve_credentials_ownership_scopes_and_original_expiry(self):
        self.copy("ServiceClient")
        self.copy("ServiceAccessToken")
        fields = "id,clientId,HEX(tokenId),HEX(tokenSecret),issued,lifetime,providerNo,HEX(scopes)"
        self.assertEqual(self.plain("SELECT {0} FROM `{1}`.ServiceAccessToken ORDER BY id".format(fields,self.src)),
                         self.plain("SELECT {0} FROM `{1}`.ServiceAccessToken ORDER BY id".format(fields,self.dst)))
        self.assertEqual(self.plain("SELECT HEX(clientKey),HEX(clientSecret),lifetime FROM `{0}`.ServiceClient".format(self.src)),
                         self.plain("SELECT HEX(clientKey),HEX(clientSecret),lifetime FROM `{0}`.ServiceClient".format(self.dst)))
        self.copy("ServiceAccessToken")
        self.assertEqual(self.plain("SELECT COUNT(*) FROM `{0}`.ServiceAccessToken".format(self.dst)), [["3"]])

    def test_should_resume_an_unconfirmed_token_copy_without_duplicate_grants(self):
        self.copy("ServiceClient")
        self.copy("ServiceAccessToken")
        self.run.progress["tables"]["ServiceAccessToken"].pop("done")
        self.copy("ServiceAccessToken")
        self.assertEqual(self.plain("SELECT COUNT(*) FROM `{0}`.ServiceAccessToken".format(self.dst)), [["3"]])
        self.assertEqual(self.plain("SELECT issued,lifetime FROM `{0}`.ServiceAccessToken WHERE id=1".format(self.dst)),
                         [["1550000000", "-1"]])

    def test_should_use_minus_one_only_when_the_old_source_column_is_absent(self):
        self.plain("ALTER TABLE `{0}`.ServiceClient DROP COLUMN lifetime".format(self.src))
        self.copy("ServiceClient")
        self.assertEqual(self.plain("SELECT lifetime FROM `{0}`.ServiceClient".format(self.dst)), [["-1"]])

    def test_should_preserve_a_present_null_setting_instead_of_inventing_a_default(self):
        self.plain("UPDATE `{0}`.ServiceClient SET lifetime=NULL".format(self.src))
        self.copy("ServiceClient")
        self.assertEqual(self.plain("SELECT lifetime IS NULL FROM `{0}`.ServiceClient".format(self.dst)), [["1"]])

    def test_should_detect_target_collation_ambiguity_without_disclosing_credentials(self):
        from carlos_ctl import o19_preflight as pf
        checks = []
        # The subquery runs in the staged database, just as standalone preflight does.
        def staged_count(table, where):
            return int(self.plain("USE `{0}`; SELECT COUNT(*) FROM `{1}` WHERE {2}".format(self.src,table,where))[0][0])
        self.plain("INSERT INTO `{0}`.ServiceAccessToken SELECT 4,clientId,UPPER(tokenId),tokenSecret,issued,lifetime,providerNo,scopes,dateCreated "
                   "FROM `{0}`.ServiceAccessToken WHERE id=1".format(self.src))
        pf.check_oauth_grants(SimpleNamespace(tables={"ServiceClient":"ServiceClient", "ServiceAccessToken":"ServiceAccessToken"},
                                             count_live=staged_count, findings=checks))
        self.assertTrue(any(f["id"] == "oauth-ambiguous-ServiceAccessToken" for f in checks))
        self.assertNotIn("fixture-o19-token", str(checks))
