# SPDX-License-Identifier: AGPL-3.0-only
import json
import os
import runpy
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid

from carlos_ctl import o19etl, o19import, schema_contract
from .carlos_src import carlos_path, requires_carlos_src


class TestContractBinding(unittest.TestCase):
    @requires_carlos_src
    def test_exporter_and_importer_share_the_complete_metadata_protocol(self):
        exporter = runpy.run_path(carlos_path("scripts", "schema-contract.py"))
        self.assertEqual(exporter["FORMAT"], schema_contract.FORMAT)
        self.assertEqual(exporter["FIELDS"], schema_contract.FIELDS)
        for province in ("on", "bc"):
            document = json.loads(Path(carlos_path("database", "mysql", "schema-contract", province + ".json")).read_text())
            self.assertEqual(document["format"], schema_contract.FORMAT)
            self.assertEqual(set(document["schema"]), set(schema_contract.FIELDS))
            self.assertEqual(exporter["migration_digest"](province),
                             schema_contract.migration_digest(Path(carlos_path("database", "mysql", "migration")), province))

    def test_contract_rejects_a_different_migration_set(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            for location in ("common", "on"):
                (root / "migration" / location).mkdir(parents=True)
            migration = root / "migration/common/V1__baseline.sql"
            migration.write_text("CREATE TABLE sample (id INT);")
            (root / "contracts").mkdir()
            document = {"format": schema_contract.FORMAT, "province": "on", "schema": {kind: [] for kind in schema_contract.FIELDS},
                        "migration_sha256": schema_contract.migration_digest(root / "migration", "on")}
            (root / "contracts/on.json").write_text(json.dumps(document))
            self.assertEqual(schema_contract.load(root, "on"), document)
            migration.write_text("CREATE TABLE sample (id BIGINT);")
            with self.assertRaisesRegex(ValueError, "migration files"):
                schema_contract.load(root, "on")

    def test_contract_rejects_the_previous_column_protocol(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "contracts").mkdir()
            document = {"format": 1, "province": "on", "schema": {kind: [] for kind in schema_contract.FIELDS}}
            (root / "contracts/on.json").write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "unsupported"):
                schema_contract.load(root, "on")


@unittest.skipUnless(os.environ.get("CARLOS_TEST_MARIADB_SOCKET"), "requires isolated MariaDB")
class TestPhysicalSchemaMariaDB(unittest.TestCase):
    def setUp(self):
        self.schema = "o19_contract_test_" + uuid.uuid4().hex
        self.base = ["mariadb", "--no-defaults", "-uroot", "--socket=" + os.environ["CARLOS_TEST_MARIADB_SOCKET"],
                     "--batch", "--skip-column-names", "--default-character-set=utf8mb4"]
        self.query("CREATE DATABASE `{0}` CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci".format(self.schema))
        self.addCleanup(self.query, "DROP DATABASE `{0}`".format(self.schema))
        self.query("CREATE TABLE `{0}`.parent(id INT PRIMARY KEY) ENGINE=InnoDB;"
                   "CREATE TABLE `{0}`.child(id INT PRIMARY KEY, parent_id INT, "
                   "label VARCHAR(30) DEFAULT 'NULL', missing VARCHAR(30) DEFAULT NULL, "
                   "CONSTRAINT parent_fk FOREIGN KEY(parent_id) REFERENCES `{0}`.parent(id), "
                   "KEY label_idx(label)) ENGINE=InnoDB;".format(self.schema))
        self.expected = schema_contract.capture(self.query, self.schema)

    def query(self, sql):
        cp = subprocess.run(self.base, input=sql, text=True, capture_output=True, check=True, timeout=20)
        return o19import.batch_rows(cp.stdout)

    def test_same_name_wrong_index_definition_is_detected(self):
        self.query("ALTER TABLE `{0}`.child DROP INDEX label_idx, ADD INDEX label_idx(parent_id)".format(self.schema))
        differences = schema_contract.differences(self.expected, schema_contract.capture(self.query, self.schema))
        self.assertTrue(any("indexes:" in line and "label_idx" in line for line in differences))

    def test_column_reordering_is_detected_without_definition_changes(self):
        self.query("ALTER TABLE `{0}`.child MODIFY label VARCHAR(30) DEFAULT 'NULL' AFTER id".format(self.schema))
        actual = schema_contract.capture(self.query, self.schema)
        self.assertEqual([row[:-1] for row in self.expected["columns"]],
                         [row[:-1] for row in actual["columns"]])
        differences = schema_contract.differences(self.expected, actual)
        self.assertTrue(any("columns:" in line and "child.label" in line for line in differences))

    def test_defaults_and_column_collations_are_exact(self):
        columns = {row[1]: row for row in self.expected["columns"] if row[0] == "child"}
        self.assertEqual(columns["label"][4], "'NULL'")
        self.assertEqual(columns["missing"][4], "NULL")
        values = o19etl.introspect_columns(self.query, self.schema)["child"]
        self.assertEqual(values["label"]["default"], "NULL")
        self.assertIsNone(values["missing"]["default"])
        self.query("ALTER TABLE `{0}`.child MODIFY label VARCHAR(30) COLLATE utf8mb4_bin DEFAULT 'NULL'".format(self.schema))
        self.assertTrue(schema_contract.differences(self.expected, schema_contract.capture(self.query, self.schema)))

    def test_preservation_extensions_are_outside_managed_contract(self):
        self.query("ALTER TABLE `{0}`.child ADD COLUMN import_archived_extra TEXT;"
                   "CREATE TABLE `{0}`.import_archived_legacy(id INT);".format(self.schema))
        self.assertEqual(schema_contract.differences(self.expected, schema_contract.capture(self.query, self.schema)), [])

    def test_foreign_key_check_finds_rows_loaded_with_checks_disabled(self):
        self.query("SET foreign_key_checks=0; INSERT INTO `{0}`.child(id,parent_id) VALUES(1,99),(2,NULL); "
                   "SET foreign_key_checks=1;".format(self.schema))
        failures = schema_contract.foreign_key_violations(self.query, self.schema, self.expected)
        self.assertEqual(len(failures), 1)
        self.assertIn("1 row(s)", failures[0])
