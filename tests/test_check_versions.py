# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""`carlos-ctl check` names both package versions.

The CLI and the application package version independently since the
split, so a check transcript must say which pair produced it; dpkg is
the source, and a package dpkg does not know is said so rather than
omitted.

Run (from the repository root):
    python3 -m unittest discover -v -s tests -t .
"""

import subprocess
import unittest
from unittest import mock

from carlos_ctl import validate


class TestPackageVersions(unittest.TestCase):

    def _run(self, answers):
        def fake_run(cmd, **kw):
            self.assertEqual(cmd[:4], ["dpkg-query", "-W", "-f", "${Version}"])
            pkg = cmd[4]
            if pkg in answers:
                return subprocess.CompletedProcess(cmd, 0, answers[pkg], "")
            return subprocess.CompletedProcess(cmd, 1, "", "no packages found")
        with mock.patch.object(validate, "run", side_effect=fake_run):
            return validate._package_versions()

    def test_both_versions_are_reported(self):
        lines = self._run({"carlos-emr": "2026.09.0~snapshot25\n",
                           "carlos-ctl": "1.0.0\n"})
        self.assertEqual(lines, ["carlos-emr 2026.09.0~snapshot25",
                                 "carlos-ctl 1.0.0"])

    def test_a_package_dpkg_does_not_know_is_said_so(self):
        lines = self._run({"carlos-ctl": "1.0.0"})
        self.assertEqual(lines[0], "carlos-emr (not installed according to dpkg)")
        self.assertEqual(lines[1], "carlos-ctl 1.0.0")


if __name__ == "__main__":
    unittest.main()
