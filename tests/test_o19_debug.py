# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from carlos_ctl import o19debug, o19etl, o19import


class TestImportDiagnostics(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = temp.name
        self.path = Path(temp.name, o19debug.FILENAME)

    def trace(self, verbose=False, append=o19import.append_private):
        return o19debug.Diagnostics(self.directory, append, verbose=verbose, mode="dry-run",
                                    resume=False, version="1.2.3", manifest="test", timeout=90)

    def events(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_failed_width_keeps_identifiers_codes_and_the_exact_readonly_query(self):
        src_col = {"type": "mediumtext", "charset": "latin1", "octet_len": 16777215,
                   "default": "private column default", "column_type": "private extended type"}
        dst_col = dict(src_col, charset="utf8mb4", nullable=True)
        entry = {"cols": ["password"], "chunk_by": "id", "renames": {"password": "OLDField"}}
        source = {"OldField": src_col, "id": {"type": "bigint", "nullable": False}}
        error = ("--------------\nSELECT 'private SQL value\nERROR 1146 (42S02): quoted text'\n"
                 "--------------\n\nERROR 1030 (HY000) at line 1: Got error 168 "
                 "\"Unknown (generic) error from engine\" from storage engine InnoDB\n")
        query = Mock(side_effect=[[["id"]], [["-1", "50000"]], [["0"]],
                                  o19etl.QueryError("private SQL prefix", error)])
        with self.assertRaises(o19etl.QueryError):
            o19etl.width_problems(query, "Audit`Log", entry, "Oscar19", {"password": dst_col}, source,
                                  diagnostics=self.trace(), phase="P2", target_schema="Carlos", target_table="log")
        event, = self.events()
        self.assertEqual((event["phase"], event["operation"], event["event"]),
                         ("P2", "measure column width", "failed"))
        self.assertEqual((event["source_schema"], event["source_table"], event["target_schema"], event["target_table"]),
                         ("Oscar19", "Audit`Log", "Carlos", "log"))
        self.assertEqual(event["source_column_by_target"], {"password": "OldField"})
        self.assertEqual((event["range_key"], event["first_id"], event["last_id"]), ("id", 49999, 50000))
        self.assertEqual((event["mariadb_errno"], event["sqlstate"], event["engine"], event["engine_errno"]),
                         (1030, "HY000", "InnoDB", 168))
        self.assertEqual((event["limit_bytes"], event["source_definition"]["charset"]), (16777215, "latin1"))
        self.assertEqual(event["width_count_sql"], query.call_args.args[0])
        self.assertIn("`Oscar19`.`Audit``Log`", event["width_count_sql"])
        self.assertIn("journalctl --utc", event["server_journal_command"])
        self.assertGreaterEqual(event["elapsed_seconds"], 0)
        self.assertNotIn("private", self.path.read_text())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_trace_keeps_progress_and_attempts_but_omits_derived_sql_and_echoed_values(self):
        entry = {"cols": ["note"], "value_exprs": {"note": "CONCAT(s.`note`, 'private prescription s.decoy')"}}
        source = {"Note": {"type": "text", "charset": "utf8mb4", "char_len": 65535}, "decoy": {}}
        target = {"note": {"type": "varchar", "nullable": True, "char_len": 20, "default": "private default"}}
        for verbose in (False, True):
            query = Mock(return_value=[["0"]])
            self.assertEqual(o19etl.width_problems(query, "Note", entry, "stage", target, source,
                                                   diagnostics=self.trace(verbose), target_schema="carlos"), [])
            self.assertIn("private prescription", query.call_args.args[0])
            self.assertEqual(self.path.exists(), verbose)
        self.assertEqual([e["event"] for e in self.events()], ["opened", "started", "completed", "started", "completed"])
        self.assertEqual(self.events()[-1]["limit_characters"], 20)
        self.assertEqual(self.events()[-1]["over_limit_rows"], 0)
        self.assertEqual(self.events()[-1]["expression_source_columns"], {"note": ["Note"]})
        self.assertEqual(self.events()[-1]["source_column_by_target"], {"note": None})
        query = Mock(side_effect=o19etl.QueryError("private SQL", "ERROR 1406 (22001): private clinical value"))
        with self.assertRaises(o19etl.QueryError):
            o19etl.width_problems(query, "Note", entry, "stage", target, source, diagnostics=self.trace(),
                                  target_schema="carlos")
        events = self.events()
        self.assertNotEqual(events[0]["attempt"], events[-1]["attempt"])
        self.assertEqual(events[-1]["mariadb_errno"], 1406)
        self.assertNotIn("width_count_sql", self.path.read_text())
        self.assertNotIn("private", self.path.read_text())
        self.assertNotIn("server_journal_command", events[-1])
        o19import.save_state(self.directory, {})
        o19import.archive_state(self.directory)
        self.assertFalse(self.path.exists())
        self.assertEqual(len(list(self.path.parent.glob(o19debug.FILENAME + ".completed-*"))), 1)

    def test_debug_explains_the_stock_log_width_without_reading_any_rows(self):
        entry = {"cols": ["data"], "chunk_by": "id"}
        src = {"data": {"type": "text", "charset": "latin1", "octet_len": 65535}}
        dst = {"data": {"type": "mediumtext", "charset": "utf8mb4", "octet_len": 16777215, "nullable": True}}
        query = Mock(side_effect=AssertionError("no SQL needed"))
        self.assertEqual(o19etl.width_problems(query, "Log", entry, "stage", dst, src, phase="P2",
                                               target_schema="carlos", target_table="log", diagnostics=self.trace(True)), [])
        query.assert_not_called()
        opened, proof = self.events()
        self.assertEqual((opened["attempt"], proof["event"], proof["source_definition"]["type"],
                          proof["target_definition"]["type"]), (proof["attempt"], "width_proven", "text", "mediumtext"))
        self.assertNotIn("width_count_sql", proof)

    def test_warning_codes_only_and_failure_of_the_trace_does_not_replace_the_database_error(self):
        message = "ETL produced SQL warning(s) 1265, 1366 (private SQL ...); refusing to record completion"
        error = o19etl.QueryError(message, message)
        self.assertEqual(o19debug.error_fields(error), {"exception": "QueryError", "warning_codes": [1265, 1366]})
        append = Mock(side_effect=OSError(28, "private path"))
        with patch.object(o19debug, "warn") as warn:
            trace = self.trace(True, append)
            with self.assertRaises(o19etl.QueryError) as caught:
                with o19debug.operation(trace, "P4", "insert copied rows", {}):
                    raise error
            self.assertIs(caught.exception, error)
            warn.assert_called_once()
            self.assertNotIn("private path", warn.call_args.args[0])
        append.assert_called_once()
        self.assertFalse(self.path.exists())

    def test_flag_is_available_for_both_hosted_commands(self):
        for import_mode in (True, False):
            parser = o19import._parser("carlos-ctl", import_mode)
            self.assertTrue(parser.parse_args(["--debug"]).debug)
            self.assertFalse(parser.parse_args([]).debug)
