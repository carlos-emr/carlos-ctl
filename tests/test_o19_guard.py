# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""The OSCAR-19-in-progress guard, as this CLI consults it.

`/usr/lib/carlos-emr/carlos-emr-o19-guard` (shipped by carlos-emr, tested
there: debian/assets/tests/test_o19_guard.py runs the script over the
full ledger matrix and pins the unit, the packaging and the postinst to
it) decides whether carlos-emr may start. `carlos-ctl start|restart`
consults it so the refusal lands on the terminal instead of a silent
systemd "condition failed". These tests pin that this CLI points at the
installed location and relays the verdict.

Run (from the repository root):
    python3 -m unittest discover -v -s tests -t .
"""

import contextlib
import io
import subprocess
import unittest
from unittest import mock

from carlos_ctl import cli, util


class TestGuardCallers(unittest.TestCase):

    """The CLI runs the shipped file, never a copy."""

    def test_cli_guard_path_matches_the_installed_location(self):
        self.assertEqual(cli.O19_GUARD,
                         "/usr/lib/carlos-emr/carlos-emr-o19-guard")


class TestLifecycleRefusal(unittest.TestCase):

    """`carlos-ctl start|restart` relay the guard's verdict to the
    terminal; `stop` never consults it."""

    def _lifecycle(self, verb, exists=True, rc=0, stderr=""):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, rc, stdout="",
                                               stderr=stderr)
        err = io.StringIO()
        with mock.patch.object(cli, "need_root"), \
                mock.patch.object(util, "reset_emr_start_limit"), \
                mock.patch.object(cli.os.path, "exists",
                                  return_value=exists), \
                mock.patch.object(util, "run", side_effect=fake_run), \
                mock.patch.object(cli.os, "execvp",
                                  side_effect=RuntimeError("execvp")) \
                as execvp, contextlib.redirect_stderr(err):
            try:
                cli._cmd_lifecycle(verb)
            except RuntimeError as e:
                self.assertEqual(str(e), "execvp")
                outcome = "exec"
            except SystemExit as e:
                outcome = e.code
        return outcome, calls, execvp, err.getvalue()

    def test_start_refused_while_import_in_progress(self):
        outcome, calls, execvp, err = self._lifecycle(
            "start", rc=1, stderr="carlos-emr: NOT starting: ...\n")
        self.assertEqual(outcome, 1)
        self.assertEqual(calls, [[cli.O19_GUARD]])
        execvp.assert_not_called()
        self.assertIn("OSCAR 19 import is in progress", err)
        self.assertIn("--resume", err)
        # the guard's own explanation is relayed verbatim
        self.assertIn("carlos-emr: NOT starting", err)

    def test_restart_refused_while_import_in_progress(self):
        outcome, calls, execvp, _ = self._lifecycle("restart", rc=1)
        self.assertEqual(outcome, 1)
        self.assertEqual(calls, [[cli.O19_GUARD]])
        execvp.assert_not_called()

    def test_start_proceeds_when_guard_permits(self):
        outcome, calls, execvp, err = self._lifecycle("start", rc=0)
        self.assertEqual(outcome, "exec")
        self.assertEqual(calls, [[cli.O19_GUARD]])
        execvp.assert_called_once_with(
            "systemctl", ["systemctl", "start", "carlos-emr.service"])
        self.assertEqual(err, "")

    def test_start_proceeds_when_guard_file_absent(self):
        # a host whose package predates the guard: unchanged behaviour
        outcome, calls, execvp, _ = self._lifecycle("start", exists=False)
        self.assertEqual(outcome, "exec")
        self.assertEqual(calls, [])
        execvp.assert_called_once()

    def test_stop_never_consults_the_guard(self):
        outcome, calls, execvp, _ = self._lifecycle("stop", rc=1)
        self.assertEqual(outcome, "exec")
        self.assertEqual(calls, [])
        execvp.assert_called_once_with(
            "systemctl", ["systemctl", "stop", "carlos-emr.service"])

    def test_guard_is_consulted_before_the_start_limit_reset(self):
        # order matters: resetting the start-rate counter is a side
        # effect on the unit, and a refused start must leave none
        order = []
        with mock.patch.object(cli, "need_root"), \
                mock.patch.object(util, "reset_emr_start_limit",
                                  side_effect=lambda: order.append(
                                      "reset")), \
                mock.patch.object(cli.os.path, "exists",
                                  return_value=True), \
                mock.patch.object(util, "run", side_effect=lambda c, **k: (
                    order.append("guard"),
                    subprocess.CompletedProcess(c, 1, "", ""))[1]), \
                mock.patch.object(cli.os, "execvp"), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli._cmd_lifecycle("restart")
        self.assertEqual(order, ["guard"])


if __name__ == "__main__":
    unittest.main()
