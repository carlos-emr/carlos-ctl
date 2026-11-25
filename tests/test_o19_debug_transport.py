# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from carlos_ctl import o19debug, o19etl, o19import


class TestImportDiagnosticTransport(unittest.TestCase):
    def run_failed_statement(self, sql, stderr):
        cp = subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)
        with patch.object(o19import, "run_sql_client", return_value=cp), self.assertRaises(o19etl.QueryError) as caught:
            o19import.make_etl_query(["mariadb"])(sql)
        return caught.exception

    def test_true_storage_error_after_a_client_banner_and_a_multiline_sql_echo(self):
        sql = "SELECT 'first line\nERROR 1062 (MANGO) at line 7654321: private value'"
        diagnostic = ('ERROR 1030 (HY000) at line 1: Got error 168 "Unknown (generic) error from engine" '
                      'from storage engine InnoDB\n')
        for banner in ("", "Warning: option deprecated\n", "\r\nmariadb: [Warning] option deprecated\r\n"):
            with self.subTest(banner=banner):
                stderr = banner + "--------------\n" + sql + "\n--------------\n\n" + diagnostic
                error = self.run_failed_statement(sql, stderr)
                self.assertEqual(error.stderr, diagnostic)
                fields = {"exception": "QueryError", "mariadb_errno": 1030, "sqlstate": "HY000",
                          "engine": "InnoDB", "engine_errno": 168}
                self.assertEqual(o19debug.error_fields(error), dict(fields, client_exit_status=1))
                self.assertEqual(o19debug.error_fields(o19etl.QueryError("failure", stderr)), fields)
                self.assertFalse(o19etl.absent_object_error(error))

    def test_ambiguous_or_truncated_echo_never_becomes_a_diagnostic(self):
        sql = "SELECT 'private echo\n--------------\nERROR 1146 (MANGO): synthetic value'"
        duplicate = "ERROR 1062 (23000) at line 1: Duplicate entry 'private value' for key 'uq'\n"
        for stderr in (
                f"--------------\n{sql}\n--------------\n\n{duplicate}",
                "--------------\nSELECT 'private value\nERROR 1146 (MANGO): missing the closing delimiter'",
                "--------------\nSELECT 1\n--------------\n" + duplicate.replace("private value", "private\n--------------\nvalue")):
            with self.subTest(stderr=stderr):
                error = self.run_failed_statement(sql, stderr)
                self.assertFalse(o19etl.absent_object_error(error))
                for text in (str(error), error.stderr):
                    self.assertIn("status 1", text)
                    self.assertNotIn("MANGO", text)
                    self.assertNotIn("private", text)
                    self.assertNotIn("1146", text)
                self.assertEqual(o19debug.error_fields(error), {"exception": "QueryError", "client_exit_status": 1})

    def test_resume_preserves_complete_events_and_replaces_only_the_torn_tail(self):
        previous = b'{"event":"completed","attempt":"earlier"}\n'
        for prefix in (b"", previous):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as directory:
                path = Path(directory, o19debug.FILENAME)
                path.write_bytes(prefix + b'{"event":"interrupted' + b"x" * 16384)
                trace = o19debug.Diagnostics(directory, o19import.append_private, verbose=False, mode="import",
                                             resume=True, version="test", manifest="test", timeout=0)
                with self.assertRaises(o19etl.QueryError):
                    with o19debug.operation(trace, "P4", "insert copied rows", {}):
                        raise o19etl.QueryError("ERROR 1062 (23000): duplicate")
                saved = path.read_bytes()
                self.assertTrue(saved.startswith(prefix))
                event, = [json.loads(line) for line in saved[len(prefix):].splitlines()]
                self.assertEqual((event["event"], event["mariadb_errno"]), ("failed", 1062))
                self.assertTrue(event["discarded_incomplete_record"])
