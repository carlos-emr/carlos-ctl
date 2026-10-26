# SPDX-License-Identifier: AGPL-3.0-only
import gzip
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

from carlos_ctl import o19bundle, o19import, o19sources


class TestCompleteBackupSet(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = str(self.root / "prepared")

    def dump(self, filename, table="a", value="plain ; text", engine="InnoDB"):
        path = self.root / filename
        content = ("SET NAMES utf8mb4;\n-- Table metadata\n"
                   "DROP TABLE IF EXISTS `{0}`;\n"
                   "CREATE TABLE `{0}` (id INT PRIMARY KEY, note TEXT) ENGINE={1} DEFAULT CHARSET=utf8 COLLATE=utf8_general_ci;\n"
                   "LOCK TABLES `{0}` WRITE;\nINSERT INTO `{0}` VALUES (1,'{2}');\nUNLOCK TABLES;\n"
                   "-- Dump completed on synthetic fixture\n").format(table, engine, value).encode()
        if filename.endswith(".gz"):
            with gzip.open(path, "wb") as output:
                output.write(content)
        else:
            path.write_bytes(content)
        return str(path)

    def test_complementary_main_and_myisam_parts(self):
        paths = [self.dump("OscarBackup.sql"), self.dump("MyISAMBackup.sql.gz", "b", engine="MyISAM")]
        result = o19sources.prepare_dump_set(paths, self.work)
        sql = Path(result["dump"]).read_bytes()
        self.assertEqual(sql.count(b"CREATE TABLE `a`"), 1)
        self.assertEqual(sql.count(b"CREATE TABLE `b`"), 1)
        self.assertIn(b"plain ; text", sql)
        self.assertEqual(result["duplicates"], [])
        self.assertEqual(len(result["parts"]), 2)

    def test_duplicate_full_dump_is_not_restored_twice(self):
        result = o19sources.prepare_dump_set([self.dump("main.sql"), self.dump("myisam.sql.gz")], self.work)
        sql = Path(result["dump"]).read_bytes()
        self.assertEqual(sql.count(b"INSERT INTO `a`"), 1)
        self.assertEqual(result["duplicates"], [{"table": "a", "part": 1, "owner": 0}])

    def test_differing_overlap_is_refused_before_output(self):
        with self.assertRaisesRegex(ValueError, "overlapping table"):
            o19sources.prepare_dump_set([self.dump("main.sql"), self.dump("old.sql", value="other")], self.work)
        self.assertFalse((Path(self.work) / "combined.sql").exists())

    def test_same_bytes_with_different_encoding_declarations_are_not_duplicates(self):
        first, second = self.dump("first.sql"), self.dump("second.sql")
        Path(second).write_bytes(Path(second).read_bytes().replace(b"NAMES utf8mb4", b"NAMES latin1"))
        with self.assertRaisesRegex(ValueError, "overlapping table"):
            o19sources.prepare_dump_set([first, second], self.work)

    def test_incomplete_part_is_refused(self):
        first, second = self.dump("main.sql"), self.dump("myisam.sql", "b")
        Path(second).write_bytes(Path(second).read_bytes().split(b"-- Dump completed")[0])
        with self.assertRaisesRegex(ValueError, "completion trailer"):
            o19sources.prepare_dump_set([first, second], self.work)

    def test_quoted_sql_and_multiline_values_remain_bytes(self):
        path = self.root / "statements.sql"
        original = b"/* comment ; */ CREATE TABLE a(x TEXT); INSERT INTO a VALUES ('a;\nCREATE TABLE fake(x int);');\n-- end\n"
        path.write_bytes(original)
        with path.open("rb") as source:
            pieces = [statement.read() for statement in o19sources.statements(source)]
        self.assertEqual(b"".join(pieces), original)
        self.assertEqual(len(pieces), 3)

    def test_dump_set_controls_cannot_disable_restore_uniqueness(self):
        source = b"/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;\nINSERT INTO a VALUES ('UNIQUE_CHECKS=0');\n"
        restored = b"".join(o19sources.restore_chunks(io.BytesIO(source)))
        self.assertIn(b"*/;\nSET SESSION UNIQUE_CHECKS=1;", restored)
        self.assertIn(b"INSERT INTO a VALUES ('UNIQUE_CHECKS=0');", restored)

    def documents(self, filename, files):
        path = self.root / filename
        with tarfile.open(path, "w:gz") as archive:
            for name, content in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        return str(path)

    def test_document_increment_overlays_only_its_paths(self):
        full = self.documents("full.tar.gz", {"oscar/document/a.pdf": b"old", "oscar/document/b.pdf": b"retained"})
        newer = self.documents("newer.tar.gz", {"oscar/document/a.pdf": b"new"})
        result = o19sources.prepare_documents([full, newer], self.work)
        with tarfile.open(result["documents"]) as archive:
            self.assertEqual(archive.extractfile("oscar/document/a.pdf").read(), b"new")
            self.assertEqual(archive.extractfile("oscar/document/b.pdf").read(), b"retained")
            self.assertEqual(len(archive.getnames()), 2)

    def test_native_incremental_document_paths_are_relative_to_context(self):
        full = self.documents("full.tar.gz", {"oscar/document/a.pdf": b"old"})
        newer = self.documents("incremental.tar.gz", {"./document/a.pdf": b"new"})
        result = o19sources.prepare_documents([full, newer], self.work)
        with tarfile.open(result["documents"]) as archive:
            self.assertEqual(archive.getnames(), ["oscar/document/a.pdf"])
            self.assertEqual(archive.extractfile("oscar/document/a.pdf").read(), b"new")

    def test_native_full_backup_roles_include_documents_and_auxiliary_files(self):
        members = ["oscar/OscarBackup.sql", "oscar/MyISAMBackup.sql.gz", "oscar/drugref.sql",
                   "oscar/oscar.properties", "oscar/oscar.war", "oscar/oscar_backup.sh", "oscar/document/a.pdf"]
        roles = o19bundle.classify_members(members)
        self.assertEqual(roles["dumps"], members[:2])
        self.assertEqual(roles["embedded_documents"], ["oscar/document/a.pdf"])
        self.assertIn("oscar/drugref.sql", roles["extras"])

    def test_saved_document_sql_and_json_are_not_database_inputs(self):
        roles = o19bundle.classify_members(["oscar/OscarBackup.sql", "oscar/oscar.properties",
                                            "oscar/eform/report.sql", "oscar/eform/data.json"])
        self.assertEqual(roles["dumps"], ["oscar/OscarBackup.sql"])
        self.assertIsNone(roles["digests"])
        self.assertIn("oscar/eform/report.sql", roles["embedded_documents"])

    def test_repeated_flags_collect_all_parts(self):
        args = o19import._parser("test", True).parse_args([
            "--dump", "main.sql", "--dump", "myisam.sql", "--documents", "full.tar", "--documents", "increment.tar"])
        self.assertEqual(args.dump_parts, ["main.sql", "myisam.sql"])
        self.assertEqual(args.documents_parts, ["full.tar", "increment.tar"])
