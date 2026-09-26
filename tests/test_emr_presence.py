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
                                          "db": handler, "import-o19": handler,
                                          "cert": handler,
                                          "o19-preflight": handler}), \
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
        # ...and from the dispatcher's own usage table: a verb's fuller
        # help lives behind carlos-emr's files (import-o19 loads the
        # manifests that package ships before it builds its parser, cert
        # and backup exec the package's helpers), so handing --help to the
        # handler would fail instead of helping
        for argv in ([], ["--help"], ["help"], ["check", "--help"],
                     ["db", "-h"], ["import-o19", "--help"],
                     ["cert", "--help"], ["o19-preflight", "-h"]):
            rc, handler, out, _ = self._main(argv, installed=False)
            self.assertEqual(rc, 0, argv)
            handler.assert_not_called()
            self.assertIn("carlos-ctl", out)
            if argv[:1] in (["db"], ["import-o19"], ["cert"],
                            ["o19-preflight"]):
                # verbs with their own parsers: answered from the usage
                # table here, with the reason the fuller help is missing
                self.assertIn(f"carlos-ctl {argv[0]}", out)
                self.assertIn("carlos-emr is not installed", out)

    def test_help_reaches_the_verb_when_carlos_emr_is_present(self):
        # a verb with its own parser answers its own -h on a normal host
        rc, handler, out, _ = self._main(["db", "-h"], installed=True)
        self.assertEqual(rc, 0)
        handler.assert_called_once_with(["-h"])
        rc, handler, out, _ = self._main(["import-o19", "--help"],
                                          installed=True)
        self.assertEqual(rc, 0)
        handler.assert_called_once_with(["--help"])

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
