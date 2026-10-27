"""Regression specifications for an isolated, disposable MariaDB 11.x server.

Only infrastructure names are patched: staging schemas and accounts are uniquely
named per test. The real restore, assessment and ETL functions execute unchanged.

Example in an Ubuntu 26.04 test container after installing carlos-ctl:
  export PYTHONPATH=/usr/lib/carlos-ctl
  export CARLOS_CTL_O19_MANIFEST_DIR=/path/to/manifest-fixture
  export CARLOS_TEST_MARIADB_SOCKET=/run/mysqld/mysqld.sock
  python3 -m unittest -v test_o19_compat_mariadb.py
"""
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from carlos_ctl import o19_preflight as pf, o19etl, o19host, o19import, o19sources


@unittest.skipUnless(os.environ.get("CARLOS_TEST_MARIADB_SOCKET"),
                     "set CARLOS_TEST_MARIADB_SOCKET to an isolated test server")
class TestO19Compatibility(unittest.TestCase):
    def setUp(self):
        self.base = ["mariadb", "--no-defaults", "--protocol=socket", "--user=root",
                     "--socket=" + os.environ["CARLOS_TEST_MARIADB_SOCKET"]]
        self.src = "o19_review_" + uuid.uuid4().hex
        self.dst = self.src + "_dst"
        for module, key, value in ((o19import, "STAGING_SCHEMA", self.src + "_stage"),
                                   (o19import, "STAGING_USER", self.src + "_user"),
                                   (o19host, "STAGING_USER", self.src + "_user")):
            p = patch.object(module, key, value)
            p.start()
            self.addCleanup(p.stop)
        for db in (self.src, self.dst):
            self.q("CREATE DATABASE `{0}` CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci".format(db))
            self.addCleanup(self.q, "DROP DATABASE IF EXISTS `{0}`".format(db))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def q(self, sql):
        cp = subprocess.run(self.base + list(o19import.CLIENT_COMMON_ARGS), input=sql,
                            text=True, capture_output=True, timeout=30, check=True)
        return o19import.batch_rows(cp.stdout)

    def test_plain_and_etl_queries_pin_utc_for_temporal_conversions(self):
        base = self.base + ["--init-command=SET time_zone='+05:30'"]
        for query in (o19import.make_query(base[1:]), o19import.make_etl_query(base)):
            with self.subTest(query=query):
                self.assertEqual(query("SELECT @@session.time_zone"), [["+00:00"]])
                query("CREATE TABLE `{0}`.utc_value (value TIMESTAMP)".format(self.dst))
                try:
                    query("INSERT INTO `{0}`.utc_value SELECT CAST('2026-01-02 03:04:05' AS DATETIME)".format(self.dst))
                    self.assertEqual(self.q("SET time_zone='+00:00'; SELECT value FROM `{0}`.utc_value".format(self.dst)),
                                     [["2026-01-02 03:04:05"]])
                finally:
                    query("DROP TABLE `{0}`.utc_value".format(self.dst))

    def test_preservation_stream_pins_utc_and_keeps_statement_timeout(self):
        base = self.base[1:] + ["--init-command=SET time_zone='+05:30'"]
        stream = o19import.make_row_stream(base, statement_timeout=12)
        self.assertEqual(list(stream("SELECT @@session.time_zone, @@session.collation_connection, @@session.max_statement_time")),
                         [["+00:00", "utf8mb4_general_ci", "12.000000"]])
        self.q("CREATE TABLE `{0}`.utc_value (value TIMESTAMP); SET time_zone='+00:00'; "
               "INSERT INTO `{0}`.utc_value VALUES('2026-01-02 03:04:05')".format(self.src))
        self.assertEqual(list(stream("SELECT value FROM `{0}`.utc_value".format(self.src))),
                         [["2026-01-02 03:04:05"]])

    def test_dump_cannot_disable_note_diagnostics(self):
        import io
        source = b"/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */; SELECT @@session.sql_notes;"
        restored = b"".join(o19sources.restore_chunks(io.BytesIO(source))).decode()
        self.assertEqual(self.q(restored), [["1"]])

    def test_staging_refuses_note_level_decimal_truncation_after_dump_disables_notes(self):
        path = Path(self.temp.name) / "lossy.sql"
        path.write_text("SET sql_mode='';\n/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;\n"
                        "CREATE TABLE loss (value DECIMAL(3,0)); INSERT INTO loss VALUES(1.1);\n-- Dump completed\n")
        _, _, _, refusal = o19import._stream_dump(["cat", str(path)],
            self.base + list(o19import.CLIENT_COMMON_ARGS) + [self.src],
            diagnostics_path=str(Path(self.temp.name) / "diagnostics.txt"))
        self.assertIsNotNone(refusal)
        self.assertIn("1265", refusal)
        self.assertNotIn("1287", refusal)

    def test_staging_preserves_zero_auto_increment_keys_with_or_without_a_mode_header(self):
        headers = ("", "/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;\n")
        for index, header in enumerate(headers):
            with self.subTest(mode_header=bool(header)):
                table = "zero_identity_{0}".format(index)
                path = Path(self.temp.name) / (table + ".sql")
                footer = "/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;\n" if header else ""
                path.write_text(header + "CREATE TABLE {0} (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(20));\n"
                                "INSERT INTO {0} VALUES(0,'zero'),(7,'seven');\n".format(table)
                                + footer + "-- Dump completed\n")
                argv = self.base + list(o19import.CLIENT_COMMON_ARGS) + [
                    "--init-command=" + o19import.staging_init_command(10), self.src]
                source_rc, client_rc, _, refusal = o19import._stream_dump(["cat", str(path)], argv,
                    diagnostics_path=str(Path(self.temp.name) / (table + "-diagnostics.txt")))
                self.assertEqual((source_rc, client_rc), (0, 0))
                self.assertIsNone(refusal)
                self.assertEqual(self.q("SELECT id,name FROM `{0}`.`{1}` ORDER BY id".format(self.src, table)),
                                 [["0", "zero"], ["7", "seven"]])

    def test_dump_mode_changes_and_saved_restoration_keep_zero_keys_and_other_modes(self):
        cases = (
            ("SET SQL_MODE='';\n", "", {"NO_AUTO_VALUE_ON_ZERO"}),
            ("SET SQL_MODE='', @OLD_SQL_MODE=@@SQL_MODE;\n"
             "SET SQL_MODE='STRICT_TRANS_TABLES,NO_ZERO_DATE';\n",
             "SET SQL_MODE=@OLD_SQL_MODE;\n",
             {"NO_AUTO_VALUE_ON_ZERO", "STRICT_TRANS_TABLES", "NO_ZERO_DATE"}),
        )
        for index, (header, footer, expected_before) in enumerate(cases):
            with self.subTest(saved_mode=bool(footer)):
                tables = ["mode_zero_{0}_{1}".format(index, phase) for phase in ("before", "after")]
                observations = "mode_values_{0}".format(index)
                body = header + "CREATE TABLE {0}(phase VARCHAR(10), modes TEXT);\n".format(observations)
                for phase, table in zip(("before", "after"), tables):
                    if phase == "after":
                        body += footer
                    body += ("CREATE TABLE {0}(id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(20));\n"
                             "INSERT INTO {0} VALUES(0,'zero'),(7,'seven');\n"
                             "INSERT INTO {1} VALUES('{2}',@@SESSION.sql_mode);\n").format(table, observations, phase)
                path = Path(self.temp.name) / (observations + ".sql")
                path.write_text(body + "-- Dump completed\n")
                argv = self.base + list(o19import.CLIENT_COMMON_ARGS) + [
                    "--init-command=" + o19import.staging_init_command(10), self.src]

                source_rc, client_rc, _, refusal = o19import._stream_dump(["cat", str(path)], argv,
                    diagnostics_path=str(Path(self.temp.name) / (observations + "-diagnostics.txt")))

                self.assertEqual((source_rc, client_rc), (0, 0))
                self.assertIsNone(refusal)
                for table in tables:
                    self.assertEqual(self.q("SELECT id,name FROM `{0}`.`{1}` ORDER BY id".format(self.src, table)),
                                     [["0", "zero"], ["7", "seven"]])
                observed = dict(self.q("SELECT phase,modes FROM `{0}`.`{1}`".format(self.src, observations)))
                self.assertEqual(set(observed["before"].split(",")), expected_before)
                self.assertEqual(set(observed["after"].split(",")), {"NO_AUTO_VALUE_ON_ZERO"})

    def stage(self, padding):
        self.q("DROP DATABASE IF EXISTS `{0}`".format(o19import.STAGING_SCHEMA))
        self.addCleanup(self.q, "DROP DATABASE IF EXISTS `{0}`".format(o19import.STAGING_SCHEMA))
        dump = ("SET NAMES utf8mb4;\n/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;\n" + "-- padding\n" * padding +
                "CREATE TABLE casemgmt_note (note_id INT PRIMARY KEY, locked CHAR(1), password VARCHAR(255), note TEXT) "
                "ENGINE=MyISAM DEFAULT CHARSET=utf8 COLLATE=utf8_general_ci;\n"
                "INSERT INTO casemgmt_note VALUES (1,'0','NULL','Readable synthetic café');\n"
                "-- Dump completed on 2026-10-05 09:35:00\n")
        path = Path(self.temp.name) / "o19.sql"
        path.write_text(dump, encoding="utf-8")
        def query(sql):
            return self.q(sql)
        query.base_argv = self.base
        ctx = dict(query=query, dump=str(path), state={"phases": {}}, restage=False,
                   state_dir=self.temp.name, dump_uncompressed=os.path.getsize(path))
        try:
            o19import.run_p1(ctx)
        except SystemExit as exc:
            self.fail("The server accepts utf8_general_ci, but P1 refused it (exit {0})".format(exc.code))
        self.assertEqual(self.q("SELECT note FROM `{0}`.casemgmt_note".format(o19import.STAGING_SCHEMA)),
                         [["Readable synthetic café"]])

    def test_utf8_general_ci_in_head_restores(self):
        self.stage(0)

    def test_utf8_general_ci_after_head_restores(self):
        self.stage(7000)

    def note_blockers(self, locked, password):
        self.q("CREATE TABLE `{0}`.casemgmt_note (locked CHAR(1), password VARCHAR(255), note TEXT) ENGINE=MyISAM".format(self.src))
        self.q("INSERT INTO `{0}`.casemgmt_note VALUES ('{1}',{2},'Readable synthetic note')".format(self.src, locked, password))
        def count_live(table, where=None):
            return int(self.q("SELECT COUNT(*) FROM `{0}`.`{1}`{2}".format(self.src, table, " WHERE " + where if where else ""))[0][0])
        state = SimpleNamespace(count_live=count_live, findings=[], tables={"casemgmt_note": "casemgmt_note"})
        return pf.check_locked_notes(state), state.findings

    def test_unlocked_placeholder_is_not_encrypted(self):
        count, findings = self.note_blockers("0", "'NULL'")
        self.assertEqual(count, 0)
        self.assertEqual(findings, [])

    def test_unlocked_sql_null_is_not_encrypted(self):
        self.assertEqual(self.note_blockers("0", "NULL"), (0, []))

    def test_real_password_and_lock_remain_blocked(self):
        count, findings = self.note_blockers("1", "'synthetic-protected'")
        self.assertEqual(count, 1)
        self.assertTrue(findings[0]["id"].startswith("B4-"))
        self.assertEqual(findings[0]["severity"], pf.BLOCKER)

    def test_locked_literal_NULL_password_remains_blocked(self):
        # Upstream accepts any nonblank password and compares it literally.
        # Excluding the string NULL on a locked row would bypass a real lock.
        count, findings = self.note_blockers("1", "'NULL'")
        self.assertEqual(count, 1)
        self.assertEqual(findings[0]["severity"], pf.BLOCKER)

    def test_locked_note_with_missing_password_remains_blocked(self):
        self.assertEqual(self.note_blockers("1", "NULL")[0], 1)

    def text_tables(self):
        for db, coll in ((self.src, "utf8mb4_unicode_ci"), (self.dst, "utf8mb4_general_ci")):
            self.q("CREATE TABLE `{0}`.sample (id INT PRIMARY KEY, name VARCHAR(40)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE={1}".format(db, coll))
        self.q("INSERT INTO `{0}`.sample VALUES (2,'café')".format(self.src))
        self.q("INSERT INTO `{0}`.sample VALUES (1,'seed')".format(self.dst))
        return o19etl.introspect_columns(self.q, self.dst)["sample"]

    def test_merge_uses_carlos_collation_across_source_collations(self):
        cols = self.text_tables()
        entry = {"class": "merge", "cols": ["id", "name"], "merge_keys": ["name"]}
        o19import.make_etl_query(self.base)(o19etl.merge_statement("sample", entry, self.src, self.dst, cols))
        self.assertEqual(self.q("SELECT name, COLLATION(name) FROM `{0}`.sample WHERE id=2".format(self.dst)),
                         [["café", "utf8mb4_general_ci"]])

    def test_copy_preserves_text_in_carlos_collation(self):
        cols = self.text_tables()
        self.q("DELETE FROM `{0}`.sample".format(self.dst))
        entry = {"class": "copy", "cols": ["id", "name"]}
        o19import.make_etl_query(self.base)(o19etl.copy_statement("sample", entry, self.src, self.dst, cols))
        self.assertEqual(self.q("SELECT name, COLLATION(name) FROM `{0}`.sample".format(self.dst)),
                         [["café", "utf8mb4_general_ci"]])

    def test_verification_detects_lost_trailing_spaces(self):
        cols = self.text_tables()
        self.q("UPDATE `{0}`.sample SET name='note '".format(self.src))
        self.q("UPDATE `{0}`.sample SET name='note'".format(self.dst))
        comparison = o19etl.value_comparison("name", "s.name", cols["name"])
        self.assertEqual(self.q("SELECT {0} FROM `{1}`.sample s CROSS JOIN `{2}`.sample d".format(
            comparison, self.src, self.dst)), [["0"]])

    def test_numeric_narrowing_is_detected_before_copy(self):
        self.text_tables()
        self.q("ALTER TABLE `{0}`.sample MODIFY id TINYINT".format(self.dst))
        self.q("UPDATE `{0}`.sample SET id=128".format(self.src))
        source = o19etl.introspect_columns(self.q, self.src)["sample"]
        target = o19etl.introspect_columns(self.q, self.dst)["sample"]
        checks = o19etl.numeric_capacity_precheck_sql("sample", {"cols": ["id"]}, self.src, target, source)
        self.assertEqual(self.q(checks[0][1]), [["1"]])
        self.assertEqual(self.q("SELECT id FROM `{0}`.sample".format(self.dst)), [["1"]])

    def test_acknowledged_repair_preserves_correct_accents_without_conversion_warnings(self):
        self.text_tables()
        self.q("DELETE FROM `{0}`.sample; INSERT INTO `{0}`.sample VALUES(1,'cafÃ©'),(2,'café'),(3,'😀')".format(self.src))
        execute = o19import.make_etl_query(self.base)
        rows = execute("SELECT id,{0} FROM `{1}`.sample s ORDER BY id".format(o19etl.repair_expr("s.name"), self.src))
        self.assertEqual(rows, [["1", "café"], ["2", "café"], ["3", "😀"]])
        self.assertEqual(execute("SELECT BINARY REPEAT('a',9000000) REGEXP '{0}'".format(o19etl.UTF8_BYTE_PATTERN.replace("\\", "\\\\"))), [["1"]])
        self.assertEqual(execute("SELECT SUM(NOT (BINARY CONVERT(CHAR(seq USING latin1) USING utf8mb4) REGEXP '{0}')) FROM `{1}`.seq_0_to_255".format(o19etl.LATIN1_UTF8_BYTE_PATTERN.replace("\\", "\\\\"), self.src)), [["0"]])

    def test_numeric_foreign_keys_are_checked_after_the_parent_map_exists(self):
        self.text_tables()
        self.q("UPDATE `{0}`.sample SET id=1000; ALTER TABLE `{1}`.sample MODIFY id TINYINT".format(self.src, self.dst))
        source = o19etl.introspect_columns(self.q, self.src)["sample"]
        target = o19etl.introspect_columns(self.q, self.dst)["sample"]
        entry = {"cols": ["id"], "fk_remap": {"id": "parent"}}
        self.assertEqual(o19etl.numeric_capacity_precheck_sql("sample", entry, self.src, target, source), [])
        archive = self.src + "_archive"
        self.q("CREATE DATABASE `{0}`; CREATE TABLE `{0}`.parent__idmap(old_id INT, new_id INT);"
               "INSERT INTO `{0}`.parent__idmap VALUES(1000,1)".format(archive))
        self.addCleanup(self.q, "DROP DATABASE `{0}`".format(archive))
        checks = o19etl.numeric_capacity_precheck_sql("sample", entry, self.src, target, source, archive_schema=archive)
        self.assertEqual(self.q(checks[0][1]), [["0"]])
        self.q("UPDATE `{0}`.parent__idmap SET new_id=128".format(archive))
        self.assertEqual(self.q(checks[0][1]), [["1"]])

    def test_numeric_warning_does_not_complete_a_copy(self):
        self.text_tables()
        self.q("DELETE FROM `{0}`.sample".format(self.dst))
        self.q("ALTER TABLE `{0}`.sample MODIFY id TINYINT".format(self.dst))
        self.q("UPDATE `{0}`.sample SET id=128".format(self.src))
        target = o19etl.introspect_columns(self.q, self.dst)["sample"]
        with self.assertRaisesRegex(o19etl.QueryError, "warning.*1264"):
            o19import.make_etl_query(self.base)(o19etl.copy_statement(
                "sample", {"class": "copy", "cols": ["id", "name"]}, self.src, self.dst, target))

    def test_numeric_string_merge_twins_receive_distinct_id_maps(self):
        archive = self.src + "_archive"
        self.q("CREATE DATABASE `{0}`".format(archive))
        self.addCleanup(self.q, "DROP DATABASE `{0}`".format(archive))
        self.q("CREATE TABLE `{0}`.sample(id INT PRIMARY KEY, code VARCHAR(10));"
               "CREATE TABLE `{1}`.sample(id INT AUTO_INCREMENT PRIMARY KEY, code INT);"
               "INSERT INTO `{0}`.sample VALUES(1,'01'),(2,'1');".format(self.src, self.dst))
        target = o19etl.introspect_columns(self.q, self.dst)["sample"]
        entry = {"class": "merge", "cols": ["id", "code"], "merge_keys": ["code"], "surrogate_pk": "id"}
        execute = o19import.make_etl_query(self.base)
        execute(o19etl.merge_statement("sample", entry, self.src, self.dst, target))
        for statement in o19etl.idmap_statements("sample", entry, self.src, self.dst, archive, target):
            execute(statement)
        self.assertEqual(self.q("SELECT COUNT(*),COUNT(DISTINCT new_id) FROM `{0}`.sample__idmap".format(archive)), [["2", "2"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
