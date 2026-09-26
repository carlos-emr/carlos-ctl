# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""carlos-ctl is its own package and outlives `apt remove carlos-emr`.

Every verb administers what carlos-emr installs, so on a host without
that package the dispatcher answers with one line naming the missing
package and the install command -- before any verb runs, and never for
--help. The check is a presence check on the package's payload (the
exploded webapp), not a dpkg status check: the carlos-emr postinst calls
this tool while dpkg still reports the package half-configured.

Run (from the repository root):
    python3 -m unittest discover -v -s tests -t .
"""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from carlos_ctl import cli, util


class TestPresencePredicate(unittest.TestCase):

    def test_installed_means_the_webapp_is_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(util, "WEBAPP", os.path.join(tmp, "carlos")):
                self.assertFalse(util.carlos_emr_installed())
                os.makedirs(os.path.join(tmp, "carlos", "WEB-INF"))
                self.assertTrue(util.carlos_emr_installed())

    def test_the_marker_is_under_the_carlos_emr_share_tree(self):
        # the predicate must look at carlos-emr's files, never this
        # package's own: both being installed is the normal state and
        # only one of them can be missing
        self.assertTrue(util.WEBAPP.startswith(util.SHARE + "/"))
        self.assertFalse(util.SHARE.startswith(util.CTL_LIB))


class TestDispatcherRefusal(unittest.TestCase):

    def _main(self, argv, installed):
        handler = mock.Mock(return_value=0)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(cli._VERBS, {"check": handler, "status": handler,
                                          "db": handler}), \
                mock.patch.object(util, "carlos_emr_installed",
                                  return_value=installed), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as exc:
                rc = exc.code
        return rc, handler, out.getvalue(), err.getvalue()

    def test_a_verb_is_refused_with_the_remedy_when_carlos_emr_is_absent(self):
        for argv in (["check"], ["status"], ["db", "-e", "select 1"]):
            rc, handler, _, err = self._main(argv, installed=False)
            self.assertEqual(rc, 1, argv)
            handler.assert_not_called()
            self.assertIn("carlos-emr is not installed", err)
            self.assertIn("carlos-ctl " + argv[0], err)
            self.assertIn("apt install", err)
            self.assertIn("carlos-emr_<version>_amd64.deb", err)

    def test_a_verb_runs_when_carlos_emr_is_present(self):
        rc, handler, _, err = self._main(["check"], installed=True)
        self.assertEqual(rc, 0)
        handler.assert_called_once_with([])
        self.assertEqual(err, "")

    def test_help_answers_without_carlos_emr(self):
        for argv in ([], ["--help"], ["help"], ["check", "--help"],
                     ["db", "-h"]):
            rc, handler, out, _ = self._main(argv, installed=False)
            self.assertEqual(rc, 0, argv)
            if argv[:1] == ["db"]:
                # a verb with its own parser answers its own -h
                handler.assert_called_once_with(["-h"])
                handler.reset_mock()
            else:
                handler.assert_not_called()
                self.assertIn("carlos-ctl", out)

    def test_argument_mistakes_are_still_answered_as_mistakes(self):
        # a typo must not be reported as "carlos-emr is not installed":
        # the argument gates run first
        rc, handler, _, err = self._main(["check", "--bogus"],
                                          installed=False)
        self.assertEqual(rc, 1)
        handler.assert_not_called()
        self.assertIn("takes no arguments", err)
        self.assertNotIn("not installed", err)
        rc, _, _, err = self._main(["no-such-verb"], installed=False)
        self.assertEqual(rc, 1)
        self.assertIn("unknown command", err)


if __name__ == "__main__":
    unittest.main()
