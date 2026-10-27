# SPDX-License-Identifier: AGPL-3.0-only
import unittest

from carlos_ctl import o19sql


class TestDumpSyntax(unittest.TestCase):
    def test_collations_survive_every_read_boundary(self):
        sql = (b"/* ordinary COLLATE=fake */\nCREATE TABLE `COLLATE` ("
               b"x varchar(20) collate 'utf8_general_ci', "
               b"y varchar(20) COLLATE = `utf8mb4_bin`) "
               b"/*!40100 COLLATE = \"utf8mb4_unicode_ci\" */;\n"
               b"INSERT INTO t VALUES ('saved SQL\\\' COLLATE=fake', 'line\nCOLLATE=fake');\n"
               b"# COLLATE=fake\n-- COLLATE=fake\n")
        expected = {"utf8_general_ci", "utf8mb4_bin", "utf8mb4_unicode_ci"}
        for width in (1, 2, 3, 7, 64, len(sql)):
            reader = o19sql.Collations()
            found = set()
            for offset in range(0, len(sql), width):
                found.update(reader.feed(sql[offset:offset + width]))
            found.update(reader.feed(b"", final=True))
            self.assertEqual(found, expected, width)

    def test_large_literals_do_not_accumulate(self):
        reader = o19sql.Collations()
        reader.feed(b"INSERT INTO notes VALUES ('")
        for _ in range(20):
            self.assertEqual(reader.feed(b"clinical text " * 10000), set())
            self.assertLessEqual(len(reader.lexer.value), 1024)
            self.assertLessEqual(len(reader.lexer.pending), 3)
        self.assertEqual(reader.feed(b"'); CREATE TABLE t(a int) COLLATE=utf8_general_ci;\n"),
                         {"utf8_general_ci"})

    def test_alias_family_matches_restore_mode(self):
        rows = [["utf8mb3_general_ci"], ["utf8mb4_unicode_ci"], ["uca1400_ai_ci"]]
        legacy = o19sql.available_collations(rows, "UTF8_IS_UTF8MB3")
        modern = o19sql.available_collations(rows, "")
        self.assertIn("utf8_general_ci", legacy)
        self.assertNotIn("utf8_unicode_ci", legacy)
        self.assertIn("utf8_unicode_ci", modern)
        self.assertNotIn("utf8_general_ci", modern)
        self.assertIn("utf8mb4_uca1400_ai_ci", modern)

    def test_redirects_ignore_literals_and_carry_syntax_at_every_boundary(self):
        safe = (b"INSERT INTO notes VALUES ('line\nUSE the inhaler;\nCREATE DATABASE x; "
                b"SET @@GLOBAL.GTID_PURGED=1'); /* USE x; */\n")
        for suffix, expected in ((b"", None), (b"USE clinic", "USE"),
                (b"CREATE /* gap */ DATABASE clinic;", "CREATE DATABASE"),
                (b"CREATE OR REPLACE SCHEMA clinic;", "CREATE SCHEMA"),
                (b"/*!50718 SET @@GLOBAL.GTID_PURGED='x' */;", "GTID_PURGED")):
            sql = safe + suffix
            for width in (1, 2, 7, len(sql)):
                reader = o19sql.Redirects()
                found = None
                for offset in range(0, len(sql), width):
                    found = reader.feed(sql[offset:offset + width]) or found
                found = reader.feed(b"", final=True) or found
                self.assertEqual(found, expected, (suffix, width))

    def test_extended_insert_emits_only_metadata_and_keeps_next_statement(self):
        reader = o19sql.Lexer()
        reader.feed(b"INSERT INTO notes VALUES (1,'first')")
        for _ in range(8):
            tokens = reader.feed(b",(2,'clinical text; USE inhaler')" * 5000)
            self.assertLessEqual(len(tokens), 1)
        tokens = reader.feed(b"; CREATE TABLE next_table(x INT);", final=True)
        self.assertIn(("word", b"CREATE"), tokens)
        self.assertIn(("word", b"next_table"), tokens)
        self.assertEqual(len(reader.boundaries), 2)
