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

from carlos_ctl import o19_preflight as pf, o19docs, o19etl, o19host, o19import, o19sources
from tests.test_docs_driver import make_tar


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

    def test_audit_log_width_on_the_host_dry_run_and_before_copy(self):
        self.q("CREATE TABLE {0}.Log (id BIGINT PRIMARY KEY, data TEXT CHARACTER SET latin1); "
               "INSERT INTO {0}.Log VALUES (0, REPEAT(0x80, 65535)), (50001, 'last'); "
               "CREATE TABLE {1}.log (id BIGINT PRIMARY KEY, data MEDIUMTEXT CHARACTER SET utf8mb4)"
               .format(self.src, self.dst))
        queries = []
        etl_query = o19import.make_etl_query(self.base)

        def counted(sql):
            queries.append(sql)
            return etl_query(sql)

        ctx = {"state_dir": self.temp.name, "state": {}, "target_db": self.dst, "query": self.q,
               "query_etl": counted, "dry_run": True, "accepted": ["content-migration"], "province": "on"}

        def assess():
            with patch.object(pf, "run_checks", return_value={"schema_map_version": "test", "findings": []}), \
                    patch.object(o19import, "STAGING_SCHEMA", self.src):
                return o19import.run_p2(ctx)

        self.assertEqual(assess()["verdict"], "go")
        self.assertEqual(queries, [])
        self.assertEqual(self.q("SELECT LENGTH(CONVERT(data USING utf8mb4)) FROM {0}.Log WHERE id=0"
                                .format(self.src)), [["196605"]])
        self.q("ALTER TABLE {0}.Log MODIFY data MEDIUMTEXT CHARACTER SET latin1; "
               "UPDATE {0}.Log SET data=REPEAT(0x80, 6000000) WHERE id=50001".format(self.src))
        report = assess()
        self.assertEqual((report["verdict"], report["exit_code"]), ("no-go", 2))
        finding, = report["findings"]
        self.assertNotIn("accept", finding)
        self.assertIn("Log.data: 1 value(s)", finding["detail"])
        self.assertIn("id 50000..50001", finding["detail"])
        counts = [q for q in queries if "LENGTH(CONVERT(" in q]
        self.assertEqual(len(counts), 2)
        self.assertTrue(all(" AND s.`id` > " in q for q in counts))
        self.assertEqual(self.q("SELECT COUNT(*) FROM {0}.log".format(self.dst)), [["0"]])

        # An engine failure in a later range also withholds the verdict.
        def failed_read(sql):
            if "LENGTH(CONVERT(" in sql and "s.`id` > 49999" in sql:
                raise o19etl.QueryError('ERROR 1030 (HY000): Got error 168 from InnoDB')
            return etl_query(sql)
        ctx["query_etl"] = failed_read
        unreadable = assess()
        self.assertEqual(unreadable["exit_code"], 2)
        self.assertRegex(unreadable["findings"][0]["detail"], r"Log\.data \(id 50000\.\.50001\).*1030.*168")

        o19etl.normalize_table_case(self.q, self.src, ["Log"])
        src = o19etl.introspect_columns(self.q, self.src)
        dst = o19etl.introspect_columns(self.q, self.dst)
        entry, _ = o19etl.effective_entry("log", o19import.o19map_schema.TABLES["log"], src["log"])
        args = self.src, self.src + "_archive", src, dst, {"log": entry}, {}, "MigrationAdmin"
        problems = o19etl.etl_precheck_problems(ctx, self.q, etl_query, *args)
        self.assertTrue(any(p.startswith("log.data: 1 value(s)") for p in problems), problems)
        with self.assertRaisesRegex(o19etl.QueryError, r"log\.data \(id 50000\.\.50001\).*1030.*168"):
            o19etl.etl_precheck_problems(ctx, self.q, failed_read, *args)

    def test_text_foreign_key_width_is_checked_when_its_parent_map_exists(self):
        for db in (self.src, self.dst):
            self.q("CREATE TABLE {0}.validations (id INT PRIMARY KEY); "
                   "CREATE TABLE {0}.measurementType (validation VARCHAR(100) NOT NULL)".format(db))
        ctx = {"query": self.q, "query_etl": o19import.make_etl_query(self.base), "target_db": self.dst}
        names = {"validations": "validations", "measurementtype": "measurementType"}
        findings = []
        with patch.object(o19import, "STAGING_SCHEMA", self.src):
            self.assertEqual(o19import.preflight_width_problems(ctx, names, findings, None), [])
            self.assertEqual(findings, [])
            self.q("ALTER TABLE {0}.measurementType MODIFY validation VARCHAR(200) NOT NULL; "
                   "INSERT INTO {0}.measurementType VALUES (CONCAT(REPEAT('0',100),'7'))".format(self.src))
            self.assertEqual(o19import.preflight_width_problems(ctx, names, findings, None), [])
        self.assertEqual(findings[0]["data"], ["measurementType.validation"])
        dst = o19etl.introspect_columns(self.q, self.dst)["measurementType"]
        src = o19etl.introspect_columns(self.q, self.src)["measurementType"]
        entry = dict(o19import.o19map_schema.TABLES["measurementType"], cols=["validation"])
        args = ctx["query_etl"], "measurementType", entry, self.src, dst, src
        self.q("CREATE TABLE {0}.validations__idmap (old_id BIGINT PRIMARY KEY, new_id BIGINT NOT NULL)"
               .format(self.dst))
        self.assertIn("1 value(s)", o19etl.width_problems(*args, archive_schema=self.dst)[0])
        self.q("INSERT INTO {0}.validations__idmap VALUES (7,23)".format(self.dst))
        self.assertEqual(o19etl.width_problems(*args, archive_schema=self.dst), [])

    def test_audit_log_resume_and_final_verification_cover_all_ranges(self):
        for schema in (self.src, self.dst):
            self.q("CREATE TABLE {0}.log (id BIGINT PRIMARY KEY, data TEXT)".format(schema))
        self.q("INSERT INTO {0}.log VALUES (-1, NULL), (0,'first'), (49998,'edge'), (50000,'last')"
               .format(self.src))
        src = o19etl.introspect_columns(self.q, self.src)
        dst = o19etl.introspect_columns(self.q, self.dst)
        entry, _ = o19etl.effective_entry("log", o19import.o19map_schema.TABLES["log"], src["log"])
        state = {}
        progress = {"tables": {"log": state}}
        query = o19import.make_etl_query(self.base)

        def interrupted(sql):
            if " JOIN " in sql and "s.`id` > 49998" in sql:
                raise o19etl.QueryError("ERROR 1030: Got error 168 from InnoDB")
            return query(sql)

        run = SimpleNamespace(query=interrupted, plain=self.q, src=self.src, dst=self.dst, arch=self.src,
                              state_dir=self.temp.name, progress=progress, counts={"copy": 0},
                              seed_group=set(), admin_user=None, admin_pn=None)
        with self.assertRaisesRegex(o19etl.QueryError, r"log \(id 49999\.\.50000\).*1030.*168"):
            o19etl.etl_copy_table(run, "log", entry, state, dst["log"], set())
        self.assertEqual(o19etl.load_progress(self.temp.name)["tables"]["log"], {"started": True, "done_through": 49998})
        run.query = query
        o19etl.etl_copy_table(run, "log", entry, state, dst["log"], set())
        self.assertTrue(state["done"])
        args = self.src, self.dst, src, dst
        self.assertEqual(o19etl.copy_content_parity(query, *args)[1], [])
        ok, bad = o19etl.copy_content_parity(interrupted, *args)
        self.assertFalse(ok)
        self.assertRegex(bad[0], r"id 49999\.\.50000.*1030.*168")
        # Same row count, but one changed value and one missing twin.
        self.q("UPDATE {0}.log SET data='changed' WHERE id=49998; "
               "UPDATE {0}.log SET id=50001 WHERE id=50000".format(self.dst))
        details = []
        ok, bad = o19etl.copy_content_parity(query, *args, details=details)
        self.assertFalse(ok)
        self.assertIn("log: 2 copied row(s)", bad[0])
        self.assertIn("id=49998", details[0])
        self.assertIn("id=50000", details[1])

    def test_document_notice_resumes_and_preserves_exact_document_parity(self):
        for schema in (self.src, self.dst):
            self.q("CREATE TABLE {0}.document (document_no INT PRIMARY KEY, docfilename VARCHAR(255), "
                   "contenttype VARCHAR(255), number_of_pages INT, docdesc VARCHAR(255)); "
                   "INSERT INTO {0}.document VALUES(1,'present.pdf','application/pdf',2,'Original'), "
                   "(9,'source-scan.tiff','image/tiff',6,'Historic scan')".format(o19etl.ident(schema)))
        self.q("CREATE TABLE {0}.HRMDocument (id INT PRIMARY KEY, reportFile VARCHAR(255)); "
               "CREATE TABLE {0}.eform (fid INT PRIMARY KEY, form_name VARCHAR(255), form_html TEXT)"
               .format(o19etl.ident(self.dst)))
        state_dir = Path(self.temp.name, "state")
        state_dir.mkdir()
        documents_root = Path(self.temp.name, "documents")
        ctx_root = documents_root / o19docs.TARGET_CTX
        ctx_root.mkdir(parents=True)
        archive = make_tar(str(Path(self.temp.name, "source.tar.gz")),
                           {"document/present.pdf": "Original synthetic source bytes"})
        ctx = {"state_dir": str(state_dir), "state": {"phases": {}}, "query": self.q,
               "documents_root": str(documents_root), "documents": archive,
               "accepted": [], "dev_target": True, "target_db": self.dst,
               "archive_schema": self.src + "_archive"}
        with patch.object(o19import, "STAGING_SCHEMA", self.src):
            with self.assertRaises(SystemExit):
                o19docs.run_docs(ctx)
            ctx["accepted"] = [o19docs.MISSING_DOCUMENTS_ACCEPT]
            original_path = ctx_root / "document" / "source-scan.tiff"
            original_path.write_bytes(b"Recovered file")
            with self.assertRaises(SystemExit):
                o19docs.run_docs(ctx)
            self.assertEqual(original_path.read_bytes(), b"Recovered file")
            original_path.unlink()
            o19docs.run_docs(ctx)
            notice = ctx_root / "document" / o19docs.placeholder_name(9)
            inode = notice.stat().st_ino
            ctx["state"]["phases"]["documents"]["status"] = "in-progress"
            o19docs.run_docs(ctx)
            self.assertEqual(notice.stat().st_ino, inode)

        name = o19docs.placeholder_name(9)
        values = self.q("SELECT docfilename, contenttype, number_of_pages, docdesc "
                        "FROM {0}.document ORDER BY document_no".format(o19etl.ident(self.dst)))
        self.assertEqual(values, [["present.pdf", "application/pdf", "2", "Original"],
                                  [name, "application/pdf", "1", "Historic scan"]])
        self.assertEqual((ctx_root / "document" / "present.pdf").read_text(),
                         "Original synthetic source bytes")
        self.assertTrue(o19docs.is_placeholder(str(notice)))
        self.assertIn("Source document unavailable", notice.read_bytes().decode("ascii"))
        self.assertEqual(ctx["state"]["phases"]["documents"]["placeholders"], 1)
        self.assertIn("source-scan.tiff", (state_dir / "documents-details.txt").read_text())

        overrides = {"document": (o19docs.placeholder_predicate([9], "s"),
                                  o19docs.placeholder_values("s"))}
        source_columns = o19etl.introspect_columns(self.q, self.src)
        target_columns = o19etl.introspect_columns(self.q, self.dst)
        args = self.q, self.src, self.dst, source_columns, target_columns
        ok, bad = o19etl.copy_content_parity(*args, post_copy=overrides)
        self.assertEqual(bad, [])
        self.assertIn("document (copy): copied values and the documented post-copy changes match", ok)
        self.q("UPDATE {0}.document SET docdesc='Changed' WHERE document_no=9"
               .format(o19etl.ident(self.dst)))
        _ok, bad = o19etl.copy_content_parity(*args, post_copy=overrides)
        self.assertTrue(any("document: 1 copied row(s)" in finding for finding in bad))

        notice.write_bytes(b"Something else")
        problems, _lines, _private = o19docs.reconcile(
            self.q, self.dst, str(ctx_root), {"present.pdf"}, placeholders={9: "source-scan.tiff"})
        self.assertTrue(any("differs from the generated notice" in problem for problem in problems))

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

    @unittest.skipUnless(os.environ.get("CARLOS_SRC"), "set CARLOS_SRC to test the packaged Consent migration")
    def test_consent_upgrade_preserves_rows_audits_flags_and_is_repeatable(self):
        """Replay the migration twice and verify every flag and audit attribution."""
        migration = next((Path(os.environ["CARLOS_SRC"]) / "database/mysql/migration/common").glob(
            "*__one_live_consent_per_type.sql")).read_text()
        self.q("CREATE TABLE `{0}`.Consent(id INT PRIMARY KEY, demographic_no INT, consent_type_id INT, "
               "explicit TINYINT NULL, optout TINYINT NULL, deleted TINYINT NULL, edit_date DATETIME NULL);"
               "INSERT INTO `{0}`.Consent VALUES"
               "(1,100,1,1,0,0,'2024-01-01'),(2,100,1,0,1,0,'2023-01-01'),"
               "(3,100,1,1,0,1,'2026-01-01'),(4,101,1,0,NULL,0,NULL),"
               "(5,102,1,0,0,NULL,NULL),(6,103,1,NULL,0,0,NULL),"
               "(7,104,1,0,0,0,NULL),(8,104,1,0,0,0,NULL)".format(self.dst))
        for attempt in range(2):
            self.q("USE `{0}`; ".format(self.dst) + migration)
            self.assertEqual(self.q("SELECT id,deleted FROM `{0}`.Consent ORDER BY id".format(self.dst)),
                             [["1", "1"], ["2", "0"], ["3", "1"], ["4", "1"],
                              ["5", "1"], ["6", "0"], ["7", "1"], ["8", "0"]])
            self.assertEqual(self.q("SELECT COUNT(*) FROM `{0}`.Consent".format(self.dst)), [["8"]])
            self.assertEqual(self.q("SELECT consent_id,reason FROM `{0}`.Consent_migration_audit ORDER BY consent_id".format(self.dst)),
                             [["1", "duplicate_retired"], ["4", "null_flag"], ["5", "null_flag"],
                              ["6", "null_flag"], ["7", "duplicate_retired"]])
        self.assertEqual(self.q("SELECT COUNT(*) FROM `{0}`.Consent_migration_audit WHERE lastUpdateUser IS NULL OR lastUpdateUser <> 'system' OR lastUpdateDate IS NULL".format(self.dst)), [["0"]])
        with self.assertRaises(subprocess.CalledProcessError):
            self.q("INSERT INTO `{0}`.Consent(id,demographic_no,consent_type_id,deleted) VALUES(9,100,1,0)".format(self.dst))

    @unittest.skipUnless(os.environ.get("CARLOS_TEST_O19_REFERENCE_DB"),
                         "set CARLOS_TEST_O19_REFERENCE_DB to a freshly migrated CARLOS database")
    def test_migrated_capacity_preserves_long_values_through_real_etl(self):
        """Copy oversized legacy values through actual SQL into migrated columns."""
        reference = os.environ["CARLOS_TEST_O19_REFERENCE_DB"]
        self.assertRegex(reference, r"^[A-Za-z0-9_]+$")
        cases = (("demographic", "sourceOfIncome", "VARCHAR(255)", "REPEAT('guardian details ',8)"),
                 ("favorites", "BN", "VARCHAR(255)", "REPEAT('B',255)"),
                 ("property", "value", "VARCHAR(4000)", "REPEAT('P',3974)"),
                 ("demographic", "previousAddress", "VARCHAR(255)", "REPEAT('A',255)"),
                 ("formMentalHealthForm1", "datetimeOfDetention", "VARCHAR(50)", "REPEAT('D',50)"),
                 ("pharmacyInfo", "city", "VARCHAR(255)", "REPEAT('C',255)"),
                 ("pharmacyInfo", "notes", "TEXT", "REPEAT(CONVERT(0xE9 USING latin1),50000)"),
                 ("admission", "admission_notes", "TEXT", "REPEAT(CONVERT(0xE9 USING latin1),50000)"))
        execute = o19import.make_etl_query(self.base)
        for table, column, source_type, expression in cases:
            with self.subTest(column=table + "." + column):
                target_type = self.q("SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
                                     "WHERE TABLE_SCHEMA='{0}' AND TABLE_NAME='{1}' AND COLUMN_NAME='{2}'".format(
                                         reference, table, column))[0][0]
                self.q("CREATE TABLE `{0}`.sample(id INT PRIMARY KEY, value {2}) DEFAULT CHARSET=latin1;"
                       "CREATE TABLE `{1}`.sample(id INT PRIMARY KEY, value {3}) DEFAULT CHARSET=utf8mb4;"
                       "INSERT INTO `{0}`.sample VALUES(1,{4})".format(
                           self.src, self.dst, source_type, target_type, expression))
                try:
                    target = o19etl.introspect_columns(self.q, self.dst)["sample"]
                    execute(o19etl.copy_statement("sample", {"class": "copy", "cols": ["id", "value"]},
                                                 self.src, self.dst, target))
                    self.assertEqual(self.q("SELECT CHAR_LENGTH(s.value), CHAR_LENGTH(d.value), "
                                            "SHA2(CONVERT(s.value USING utf8mb4),256)=SHA2(d.value,256) "
                                            "FROM `{0}`.sample s JOIN `{1}`.sample d USING(id)".format(self.src, self.dst)),
                                     self.q("SELECT CHAR_LENGTH(value),CHAR_LENGTH(value),1 FROM `{0}`.sample".format(self.src)))
                finally:
                    self.q("DROP TABLE `{0}`.sample; DROP TABLE `{1}`.sample".format(self.src, self.dst))

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
