# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Scope, failure, standalone export, and resume contracts for exact repair."""

import ast
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from carlos_ctl import o19etl, o19manifest, o19_preflight as pf


class TestExactCharset(unittest.TestCase):
    def test_only_the_scoped_source_column_is_wrapped(self):
        entry = {"class": "copy", "cols": ["special", "comment"]}
        columns = {"special": {}, "comment": {}}
        scoped, _ = o19etl.effective_entry("drugs", entry, columns)
        self.assertNotIn("_exact_charset_columns", entry)
        self.assertEqual(scoped["_exact_charset_columns"], ["special"])
        self.assertIn("UNHEX('C3A2C280C293')", o19etl.source_expr(
            scoped, "special", {"special"}))
        self.assertNotIn("UNHEX", o19etl.source_expr(scoped, "special"))
        self.assertNotIn("UNHEX", o19etl.source_expr(
            scoped, "comment", {"comment"}))
        other, _ = o19etl.effective_entry("other", entry, columns)
        self.assertNotIn("UNHEX", o19etl.source_expr(
            other, "special", {"special"}))
        renamed = {"cols": ["instructions"], "renames": {"instructions": "special"}}
        renamed, _ = o19etl.effective_entry("drugs", renamed, columns)
        self.assertIn("UNHEX", o19etl.source_expr(
            renamed, "instructions", {"instructions"}))
        archive = {"cols": ["import_archived_special"],
                   "renames": {"import_archived_special": "special"},
                   "archived_cols": {"import_archived_special": "special"}}
        archive, _ = o19etl.effective_entry("drugs", archive, columns)
        self.assertNotIn("UNHEX", o19etl.source_expr(
            archive, "import_archived_special", {"import_archived_special"}))

    def test_warning_missing_and_malformed_results_fail_closed(self):
        for rows in ([["1"]], [["1"], ["1"]], [], [["NULL"], ["0"]],
                     [["-1"], ["0"]], [["1", "0"], ["0"]]):
            with self.subTest(rows=rows), self.assertRaises(RuntimeError):
                pf.checked_charset_count(lambda _sql: rows, "drugs", "1")

    def test_new_only_rows_are_in_the_saved_repair_set(self):
        measured = {"rows": 2, "bad": 0,
                    "rules": {r[0]: 1 for r in pf.EXACT_CHARSET_RULES}}
        audit = {}
        with patch.object(o19etl.o19map_schema, "TABLES", {
                "drugs": {"charset_scan": ["special"]}}), patch.object(
                pf, "exact_charset_counts", return_value=measured):
            result = o19etl.detect_repairs(None, "src", ["charset-repair"], audit=audit)
            self.assertEqual(result, {"drugs": {"special"}})
            self.assertEqual(audit["columns"]["drugs.special"], measured)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                o19etl.detect_repairs(None, "src", [])
            measured["bad"] = 1
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                o19etl.detect_repairs(None, "src", ["charset-repair"])

    def test_detected_source_rules_follow_the_target_rename(self):
        entry = {"cols": ["instructions"], "renames": {"instructions": "special"},
                 "charset_scan": ["special"]}
        measured = {"rows": 1, "bad": 0,
                    "rules": {r[0]: int(i == 0) for i, r in enumerate(pf.EXACT_CHARSET_RULES)}}
        with patch.object(o19etl.o19map_schema, "TABLES", {"drugs": entry}), patch.object(
                pf, "exact_charset_counts", return_value=measured):
            self.assertEqual(o19etl.detect_repairs(None, "src", ["charset-repair"]),
                             {"drugs": {"instructions"}})
            with tempfile.TemporaryDirectory() as directory:
                o19etl.save_progress(directory, {
                    "tables": {"drugs": {"done": True}},
                    "etl_contract": o19etl.ETL_CONTRACT_VERSION,
                    "repairs": {"drugs": ["instructions"]}})
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    o19etl.load_progress(directory)
            entry["value_exprs"] = {"instructions": "NULL"}
            with self.assertRaisesRegex(RuntimeError, "no writable target"):
                o19etl.detect_repairs(None, "src", ["charset-repair"])

    def test_standalone_exports_preserve_rules_and_python34_syntax(self):
        for province in ("on", "bc"):
            with self.subTest(province=province), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "preflight.py"
                o19manifest.write_standalone_preflight(str(path))
                text = path.read_text()
                ast.parse(text, feature_version=(3, 4))
                namespace = {"__name__": "exported_preflight"}
                exec(compile(text, str(path), "exec"), namespace)
                namespace["bind"](province)
                self.assertEqual(namespace["EXACT_CHARSET_RULES"], pf.EXACT_CHARSET_RULES)
                self.assertEqual(namespace["exact_charset_candidate"]("v"),
                                 o19etl.repair_expr("v", exact=True))
                self.assertEqual(namespace["exact_charset_residual"]("v"),
                                 pf.exact_charset_residual("v"))

    def test_rule_version_and_contract_cannot_change_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            progress = {"tables": {"drugs": {"done": True}},
                        "etl_contract": 2, "repairs": {"drugs": ["special"]}}
            o19etl.save_progress(directory, progress)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                o19etl.load_progress(directory, etl_contract=o19etl.ETL_CONTRACT_VERSION)
            progress["etl_contract"] = o19etl.ETL_CONTRACT_VERSION
            for version in (None, "unknown-version"):
                progress["charset_rules"] = {"version": version, "columns": {"drugs.special": {}}}
                o19etl.save_progress(directory, progress)
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    o19etl.load_progress(directory)
            progress["charset_rules"] = {
                "version": pf.EXACT_CHARSET_VERSION,
                "columns": {"drugs.special": {"rows": 1, "bad": 0,
                            "rules": {r[0]: int(i == 0) for i, r in enumerate(pf.EXACT_CHARSET_RULES)}}}}
            o19etl.save_progress(directory, progress)
            self.assertEqual(o19etl.load_progress(directory),
                             json.loads(json.dumps(progress)))
