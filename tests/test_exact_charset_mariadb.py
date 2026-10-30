# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Exact drug instructions through the real assessment, copy, and P7 code."""

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from carlos_ctl import o19etl, o19import, o19_preflight as pf

DASH = bytes.fromhex("C3A2C280C293").decode()
LE = bytes.fromhex("C3A2C289C2A4").decode()
UNKNOWN = bytes.fromhex("C3A2C280C294").decode()
CP = "Café".encode().decode("cp1252")


def literal(value):
    return "NULL" if value is None else "CONVERT(UNHEX('{0}') USING utf8mb4)".format(value.encode().hex())


@unittest.skipUnless(os.environ.get("CARLOS_TEST_MARIADB_SOCKET") and shutil.which("mariadb"),
                     "set CARLOS_TEST_MARIADB_SOCKET for live encoding regressions")
class TestExactCharsetMariaDB(unittest.TestCase):
    def setUp(self):
        self.base = ["mariadb", "--no-defaults", "--protocol=socket", "--user=root",
                     "--socket=" + os.environ["CARLOS_TEST_MARIADB_SOCKET"]]
        name = "o19_exact_" + uuid.uuid4().hex
        self.src, self.dst = name + "_src", name + "_dst"
        for db in (self.src, self.dst):
            self.query("CREATE DATABASE `{0}`".format(db))
            self.addCleanup(self.query, "DROP DATABASE `{0}`".format(db))
            self.query("CREATE TABLE `{0}`.drugs (drugid INT PRIMARY KEY, "
                       "special TEXT, comment TEXT) CHARACTER SET utf8mb4 "
                       "COLLATE utf8mb4_bin".format(db))
        self.entry = {"class": "copy", "cols": ["drugid", "special", "comment"],
                      "charset_scan": ["special"]}
        manifest = patch.object(o19etl.o19map_schema, "TABLES", {"drugs": self.entry})
        manifest.start()
        self.addCleanup(manifest.stop)
        self.state = {}
        self.ledger = tempfile.TemporaryDirectory()
        self.addCleanup(self.ledger.cleanup)
        self.run = SimpleNamespace(
            query=o19import.make_etl_query(self.base, statement_timeout=30),
            plain=self.query, src=self.src, dst=self.dst, arch=self.dst,
            state_dir=self.ledger.name, progress={"tables": {"drugs": self.state}},
            counts={"copy": 0}, seed_group=set(), admin_user="unused", admin_pn=None)

    def query(self, sql):
        p = subprocess.run(self.base + ["--default-character-set=utf8mb4", "--batch", "--skip-column-names"],
                           input=sql, text=True, capture_output=True, check=True, timeout=45)
        return o19import.batch_rows(p.stdout)

    def source_query(self, sql):
        return self.query("USE `{0}`; {1}".format(self.src, sql))

    def seed(self, values):
        self.query("TRUNCATE `{0}`.drugs".format(self.src))
        rows = ",".join("({0},{1},{2})".format(n + 37, literal(value), literal("unrelated " + DASH))
                        for n, value in enumerate(values))
        self.query("INSERT INTO `{0}`.drugs VALUES {1}".format(self.src, rows))

    def assessment(self, guarded=True):
        c = SimpleNamespace(tables={"drugs": "drugs"}, findings=[], query_errors={}, absent_objects={},
                            query=self.source_query,
                            count_live=lambda t, w: int(self.source_query(
                                "SELECT COUNT(*) FROM `{0}` WHERE {1}".format(t, w))[0][0]))
        if guarded:
            pf.check_text_encoding(c)
        else:
            with patch.object(pf, "guarded_charset_supported", return_value=False):
                pf.check_text_encoding(c)
        self.assertEqual(c.query_errors, {})
        return c.findings

    def detect(self, accepted=("charset-repair",)):
        audit = {}
        repairs = o19etl.detect_repairs(self.query, self.src, accepted, audit=audit)
        return repairs, audit

    def copy(self, repairs):
        self.src_info = o19etl.introspect_columns(self.query, self.src)
        self.dst_info = o19etl.introspect_columns(self.query, self.dst)
        entry, _ = o19etl.effective_entry("drugs", self.entry, self.src_info["drugs"], {"drugs"})
        o19etl.etl_copy_table(self.run, "drugs", entry, self.state,
                             self.dst_info["drugs"], repairs.get("drugs", set()))

    def parity(self, repairs):
        return o19etl.copy_content_parity(self.query, self.src, self.dst,
                                         self.src_info, self.dst_info, repairs=repairs)

    def test_should_require_acknowledgement_and_count_overlapping_rules_once(self):
        self.seed([DASH + LE + DASH])
        findings = self.assessment()
        f = next(f for f in findings if f["id"] == "charset-mojibake")
        self.assertEqual(f["accept"], "charset-repair")
        self.assertEqual(f["data"]["drugs.special"], 1)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.detect(())
        repairs, audit = self.detect()
        self.assertEqual(repairs, {"drugs": {"special"}})
        self.assertEqual(audit["columns"]["drugs.special"]["rules"],
                         {r[0]: 1 for r in pf.EXACT_CHARSET_RULES})
        self.copy(repairs)
        self.assertTrue(self.state["done"])
        self.assertEqual(self.query("SELECT HEX(special) FROM `{0}`.drugs".format(self.dst)),
                         [["E28093E289A4E28093"]])

    def test_should_copy_and_verify_correct_text_and_exact_spans_without_changing_source(self):
        cases = [(DASH, "–"), (LE, "≤"), (CP, "Café"), (None, None), ("", ""),
                 ("1,800", "1,800"), ("Café São", "Café São"),
                 ("– ≤ 漢字 🧪", "– ≤ 漢字 🧪"),
                 ("Café\u0085line", "Café\u0085line"), ("a\u0089b", "a\u0089b"),
                 ("a\0\r\n" + LE + "\tb", "a\0\r\n≤\tb"),
                 ("é 漢字 " + DASH + " 🧪", "é 漢字 – 🧪"),
                 ("a" * 65529 + DASH, "a" * 65529 + "–")]
        for collation in ("utf8mb4_general_ci", "utf8mb4_bin"):
            with self.subTest(collation=collation):
                self.query("TRUNCATE `{0}`.drugs".format(self.dst))
                self.state.clear()
                self.query("ALTER TABLE `{0}`.drugs MODIFY special TEXT CHARACTER SET utf8mb4 COLLATE {1}".format(self.src, collation))
                self.seed([v for v, _ in cases])
                before = self.query("SELECT drugid,HEX(special),HEX(comment) FROM `{0}`.drugs ORDER BY drugid".format(self.src))
                self.assertFalse(any(f["id"] == "B8-charset-unrepairable" for f in self.assessment()))
                repairs, _ = self.detect()
                self.copy(repairs)
                values = self.query("SELECT HEX(special) FROM `{0}`.drugs ORDER BY drugid".format(self.dst))
                self.assertEqual(values, [["NULL" if v is None else v.encode().hex().upper()] for _, v in cases])
                self.assertEqual(self.query("SELECT drugid,HEX(special),HEX(comment) FROM `{0}`.drugs ORDER BY drugid".format(self.src)), before)
                self.assertEqual(self.query("SELECT HEX(comment) FROM `{0}`.drugs LIMIT 1".format(self.dst)),
                                 [[("unrelated " + DASH).encode().hex().upper()]])
                _ok, bad = self.parity(repairs)
                self.assertEqual(bad, [])
                self.query("UPDATE `{0}`.drugs SET special='altered' WHERE drugid=37".format(self.dst))
                self.assertTrue(self.parity(repairs)[1])

    def test_should_refuse_residual_and_extra_encoding_layers_before_copy(self):
        # A literal â+C1 pair is valid UTF-8 but ambiguous. It must not
        # be guessed or silently rewritten, even if it was intended.
        cases = [UNKNOWN, DASH[:-1], "â\u0089", DASH + UNKNOWN, CP + DASH,
                 CP.encode().decode("cp1252"), DASH.encode().decode("cp1252"),
                 LE.encode().decode("cp1252")]
        for value in cases:
            with self.subTest(hex=value.encode().hex()):
                self.seed([value])
                self.assertTrue(any(f["id"] == "B8-charset-unrepairable" for f in self.assessment()))
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    self.detect()
                self.assertEqual(self.query("SELECT COUNT(*) FROM `{0}`.drugs".format(self.dst)), [["0"]])

    def test_should_assess_known_tokens_without_modern_byte_regex(self):
        self.seed([DASH + LE])
        findings = self.assessment(guarded=False)
        self.assertTrue(any(f["id"] == "charset-mojibake" for f in findings))
        self.assertTrue(any(f["id"] == "charset-exact-unmeasured" for f in findings))
        self.assertFalse(any(f["id"] == "B8-charset-unrepairable" for f in findings))
        with patch.object(pf, "_hex_escape_regexp_supported", return_value=False):
            findings = self.assessment(guarded=False)
        self.assertTrue(any(f["id"] == "charset-mojibake" for f in findings))
        self.assertTrue(any(f["id"] == "charset-b8-unmeasured" for f in findings))

    def test_should_preserve_the_genuine_latin1_staging_path(self):
        self.query("ALTER TABLE `{0}`.drugs MODIFY special TEXT CHARACTER SET latin1".format(self.src))
        self.seed([CP, "Café", "plain"])
        repairs, _ = self.detect()
        self.copy(repairs)
        self.assertEqual(self.query("SELECT HEX(special) FROM `{0}`.drugs ORDER BY drugid".format(self.dst)),
                         [["436166C3A9"], ["436166C3A9"], ["706C61696E"]])
        self.assertEqual(self.parity(repairs)[1], [])

    def test_should_refuse_truncation_without_a_completion_checkpoint(self):
        self.query("ALTER TABLE `{0}`.drugs MODIFY special VARCHAR(1)".format(self.dst))
        self.seed([DASH + LE])
        repairs, _ = self.detect()
        with self.assertRaises(o19etl.QueryError):
            self.copy(repairs)
        self.assertFalse(self.state.get("done"))
