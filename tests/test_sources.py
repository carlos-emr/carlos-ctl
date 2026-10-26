# SPDX-License-Identifier: AGPL-3.0-only
import gzip
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
import contextlib
from unittest import mock

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

    def test_compressed_sql_capacity_is_checked_before_any_expanded_write(self):
        paths = [self.dump("main.sql.gz", value="A" * (2 << 20)),
                 self.dump("myisam.sql.gz", "b", value="B" * (2 << 20))]
        self.assertTrue(all(Path(path).stat().st_size < 65536 for path in paths))
        with mock.patch.object(o19sources.shutil, "disk_usage", return_value=mock.Mock(free=65536)), \
                mock.patch.object(o19sources, "inventory") as inventory:
            with self.assertRaisesRegex(ValueError, "expanded SQL dump set"):
                o19sources.prepare_dump_set(paths, self.work)
        inventory.assert_not_called()
        self.assertFalse((Path(self.work) / "combined.sql").exists())
        self.assertFalse((Path(self.work) / "combined.sql.incoming").exists())

    def test_preparation_budgets_disk_spool_in_addition_to_combined_sql(self):
        paths = [self.dump("main.sql.gz", value="A" * (2 << 20)),
                 self.dump("myisam.sql.gz", "b", value="B" * (2 << 20))]
        # The combined SQL fits in 5 MiB, but its 2 MiB INSERT spool does not.
        with mock.patch.object(o19sources.shutil, "disk_usage", return_value=mock.Mock(free=5 << 20)), \
                mock.patch.object(o19sources, "inventory") as inventory:
            with self.assertRaisesRegex(ValueError, "temporary statement spool"):
                o19sources.prepare_dump_set(paths, self.work)
        inventory.assert_not_called()

    def test_preparation_does_not_charge_snapshots_twice(self):
        paths = [self.dump("main.sql", value="A" * (2 << 20)),
                 self.dump("myisam.sql", "b", value="B" * (2 << 20))]
        capacity = 11 << 20

        def remaining_capacity(path):
            consumed = sum(part.stat().st_size for part in Path(path).glob("part-*.sql"))
            return mock.Mock(free=capacity - consumed)

        with mock.patch.object(o19sources.shutil, "disk_usage", side_effect=remaining_capacity):
            result = o19sources.prepare_dump_set(paths, self.work)
        # Snapshots consume 4 MiB and current free space is about 7 MiB;
        # 4 MiB combined SQL plus one 2 MiB spool still fit safely.
        self.assertGreater(Path(result["dump"]).stat().st_size, 4 << 20)
        self.assertEqual(len(result["parts"]), 2)
        self.assertEqual(Path(result["dump"]).read_bytes().count(b"INSERT INTO"), 2)

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
        self.assertIn(b"*/;\nSET SESSION UNIQUE_CHECKS=1, SQL_NOTES=1, note_verbosity='all';", restored)
        self.assertIn(b"INSERT INTO a VALUES ('UNIQUE_CHECKS=0');", restored)

    def test_dump_set_controls_cannot_suppress_note_diagnostics(self):
        source = b"/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;\nINSERT INTO a VALUES ('SQL_NOTES=0');\n"
        restored = b"".join(o19sources.restore_chunks(io.BytesIO(source)))
        self.assertIn(b"SQL_NOTES=0 */;\nSET SESSION UNIQUE_CHECKS=1, SQL_NOTES=1, note_verbosity='all';", restored)
        self.assertIn(b"INSERT INTO a VALUES ('SQL_NOTES=0');", restored)

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

    def test_pax_incremental_document_names_keep_the_full_context(self):
        full = self.documents("full.tar.gz", {"oscar/document/santé.pdf": b"old"})
        newer = self.documents("incremental.tar.gz", {"./document/santé.pdf": b"new"})
        result = o19sources.prepare_documents([full, newer], self.work)
        with tarfile.open(result["documents"]) as archive:
            self.assertEqual(archive.getnames(), ["oscar/document/santé.pdf"])
            self.assertEqual(archive.extractfile("oscar/document/santé.pdf").read(), b"new")

    def test_document_set_capacity_includes_pax_headers_and_padding(self):
        paths = [self.documents("part-{0}.tar.gz".format(part),
                                {"oscar/document/santé-{0}-{1}.pdf".format(part, number): b"x"
                                 for number in range(30)}) for part in range(2)]
        baseline = o19sources.prepare_documents(paths, self.work)
        actual = Path(baseline["documents"]).stat().st_size
        self.assertGreater(actual, 60 * 1025 + 10240)
        refused = str(self.root / "refused")
        with mock.patch.object(o19sources.shutil, "disk_usage", return_value=mock.Mock(free=actual - 1)):
            with self.assertRaisesRegex(ValueError, "complete documents backup set"):
                o19sources.prepare_documents(paths, refused)
        self.assertFalse((Path(refused) / "documents.tar.incoming").exists())
        self.assertFalse((Path(refused) / "documents.tar").exists())
        with mock.patch.object(o19sources.shutil, "disk_usage", return_value=mock.Mock(free=actual)):
            accepted = o19sources.prepare_documents(paths, str(self.root / "accepted"))
        self.assertEqual(Path(accepted["documents"]).stat().st_size, actual)

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

    def context(self, dump, state, restage=False, assessment=False):
        properties = self.root / "oscar.properties"
        properties.write_text("billregion=ON\n")
        args = o19import._parser("test", True).parse_args([
            "--dump", dump, "--properties", str(properties), "--skip-documents",
            "--accept", "no-documents", "--dev-target", "--mariadb-arg=--no-defaults"])
        args.restage = restage
        with mock.patch.object(o19import.HOST, "is_packaged_host", return_value=False), \
                mock.patch.object(o19import, "take_workspace_lock"), \
                mock.patch.object(o19import, "package_version", return_value="test"):
            return o19import._make_ctx(args, not assessment, str(state))

    def test_only_accepted_durable_source_sets_retire_older_siblings(self):
        state = self.root / "state"
        dump = self.dump("clinic.sql")
        first = self.context(dump, state)
        original = Path(first["properties"]).parent
        root = original.parent
        stale = root / ("0" * 64)
        stale.mkdir()
        (stale / "source.properties").write_text("obsolete source")
        evidence = root / "operator-evidence"
        evidence.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (root / ("f" * 64)).symlink_to(outside, target_is_directory=True)
        ledger = Path(o19import.state_path(str(state)))
        before = ledger.read_bytes()
        self.dump("clinic.sql", value="new snapshot")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.context(dump, state)
        self.assertEqual(ledger.read_bytes(), before)
        self.assertTrue(original.exists())
        self.assertTrue(stale.exists())
        candidate = next(path for path in root.iterdir()
                         if path.is_dir() and path not in (original, stale, evidence) and not path.is_symlink())

        save = o19import.save_state
        remove = o19import.shutil.rmtree
        events = []

        def durable_save(*args):
            save(*args)
            events.append("saved")

        def retire(path):
            self.assertIn("saved", events)
            self.assertNotEqual(ledger.read_bytes(), before)
            self.assertTrue(candidate.exists())
            events.append("retired")
            remove(path)

        with mock.patch.object(o19import, "save_state", side_effect=durable_save), \
                mock.patch.object(o19import.shutil, "rmtree", side_effect=retire):
            accepted = self.context(dump, state, restage=True)
        self.assertEqual(Path(accepted["properties"]).parent, candidate)
        self.assertFalse(original.exists())
        self.assertFalse(stale.exists())
        self.assertTrue(evidence.exists())
        self.assertTrue((root / ("f" * 64)).is_symlink())
        self.assertTrue(outside.exists())

        # If accepting the next set cannot persist its ledger, no earlier
        # snapshot may disappear, including the currently recorded candidate.
        self.dump("clinic.sql", value="third snapshot")
        with mock.patch.object(o19import, "save_state", side_effect=OSError("disk failure")), \
                mock.patch.object(o19import, "retire_input_snapshots") as cleanup, \
                self.assertRaises(OSError):
            self.context(dump, state, restage=True)
        cleanup.assert_not_called()
        self.assertTrue(candidate.exists())

    def test_assessment_snapshot_retirement_does_not_touch_real_inputs_or_ledger(self):
        state = self.root / "state"
        dump = self.dump("clinic.sql")
        first = self.context(dump, state)
        real_sources = Path(first["properties"]).parent
        ledger = Path(o19import.state_path(str(state)))
        before = ledger.read_bytes()
        old = self.context(dump, state, assessment=True)
        old_sources = Path(old["properties"]).parent
        self.dump("clinic.sql", value="new assessment")
        new = self.context(dump, state, assessment=True)
        self.assertFalse(old_sources.exists())
        self.assertTrue(Path(new["properties"]).parent.exists())
        self.assertTrue(real_sources.exists())
        self.assertEqual(ledger.read_bytes(), before)
