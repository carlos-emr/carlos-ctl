# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Exercise the actual subprocess boundary, not pre-decoded mock rows."""

import subprocess
import sys
import unittest
from unittest import mock

from carlos_ctl import o19etl, o19host, o19import


class TestBufferedClientTransport(unittest.TestCase):

    def _base(self, output, rc=0, error=b""):
        # A real child emits MariaDB batch bytes, including literal CR.
        script = ("import sys; sys.stdin.buffer.read(); "
                  "sys.stdout.buffer.write({0!r}); "
                  "sys.stderr.buffer.write({2!r}); "
                  "sys.exit({1})").format(output, rc, error)
        return [sys.executable, "-c", script]

    def test_should_preserve_rows_and_values_in_both_buffered_readers(self):
        wire = b"before\rafter\tx\r\\ny\t\\\\n\t\xc3\xa9\n\tend\n"
        expected = [["before\rafter", "x\r\ny", "\\n", "é"], ["", "end"]]
        base = self._base(wire)
        host = o19host.Host()
        with mock.patch.object(host, "client_base_argv", return_value=base), \
                mock.patch.object(o19import, "HOST", host):
            for query in (o19import.make_query(None),
                          o19import.make_etl_query(base)):
                self.assertEqual(query("SELECT 'synthetic';"), expected)
        # Demonstrate why text=True is wrong before batch_rows sees it.
        control = subprocess.run(base, input="", text=True,
                                 capture_output=True, check=True)
        self.assertNotEqual(o19import.batch_rows(control.stdout), expected)

    def test_should_refuse_invalid_encoding_instead_of_replacing_data(self):
        base = self._base(b"sensitive-value\xff\n")
        with self.assertRaises(o19etl.QueryError) as raised:
            o19import.make_etl_query(base)("SELECT 'synthetic';")
        self.assertIn("invalid UTF-8", str(raised.exception))
        self.assertNotIn("sensitive-value", str(raised.exception))

    def test_should_propagate_client_failure(self):
        with self.assertRaises(o19etl.QueryError):
            o19import.make_etl_query(self._base(b"", 1))("SELECT 1;")

    def test_should_report_duplicate_keys_without_exposing_conflicting_values(self):
        for value in ("FAKE-PATIENT", "FAKE'PATIENT", "FAKE\nPATIENT"):
            with self.subTest(value=value):
                error = ("ERROR 1062 (23000) at line 1: Duplicate entry '{0}' "
                         "for key 'uq_name'\n".format(value)).encode()
                query = o19import.make_etl_query(self._base(b"", 1, error))
                with self.assertRaises(o19etl.QueryError) as raised:
                    query("INSERT INTO target SELECT * FROM source")
                for text in (str(raised.exception), raised.exception.stderr):
                    self.assertIn("1062", text)
                    self.assertIn("duplicate unique key", text)
                    self.assertNotIn("FAKE", text)
                    self.assertNotIn("PATIENT", text)
                self.assertFalse(o19etl.absent_object_error(raised.exception))

    def test_should_preserve_other_errors_when_echoed_sql_mentions_error_1062(self):
        error = (b"--------------\nSELECT 'ERROR 1062' FROM missing_table\n--------------\n"
                 b"ERROR 1146 (42S02) at line 1: Table 'missing_table' doesn't exist\n")
        with self.assertRaises(o19etl.QueryError) as raised:
            o19import.make_etl_query(self._base(b"", 1, error))("SELECT 1")
        self.assertEqual(raised.exception.stderr, error.decode())
        self.assertIn("1146", str(raised.exception))

    def test_should_disable_restore_commands_and_inherited_force(self):
        argv = o19host.Host().staging_client_argv(
            ["mariadb", "--force", "--binary-mode=0"], "/private/client.cnf")
        self.assertGreater(argv.index("--binary-mode"),
                           argv.index("--binary-mode=0"))
        self.assertGreater(argv.index("--skip-force"), argv.index("--force"))
