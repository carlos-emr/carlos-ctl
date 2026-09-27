# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""The WAF policy rewrite (waf._set_engine) guards the front door: it must
replace the file atomically, never follow a planted temporary name, keep
the live file's mode and owner, and make the rename durable before
`waf blocking` / `detect-only` report success.

Run (from the repository root):
    python3 -m unittest discover -v -s tests -t .
"""

import os
import stat
import tempfile
import unittest
from unittest import mock

from carlos_ctl import waf

POLICY = """# CARLOS ModSecurity policy
SecRuleEngine DetectionOnly
SecRequestBodyAccess On
"""


class TestSetEngine(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.main = os.path.join(self.tmp.name, "main.conf")
        with open(self.main, "w", encoding="utf-8") as fh:
            fh.write(POLICY)
        os.chmod(self.main, 0o640)
        patch = mock.patch.object(waf, "MAIN", self.main)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_engine_line_is_rewritten_and_nothing_else(self):
        waf._set_engine("On")
        with open(self.main, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text, POLICY.replace("DetectionOnly", "On"))
        self.assertEqual(stat.S_IMODE(os.stat(self.main).st_mode), 0o640)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["main.conf"])

    def test_a_planted_temporary_name_is_not_followed(self):
        canary = os.path.join(self.tmp.name, "canary")
        with open(canary, "w", encoding="utf-8") as fh:
            fh.write("untouched\n")
        os.symlink(canary, self.main + ".tmp")
        waf._set_engine("On")
        with open(canary, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "untouched\n")
        self.assertFalse(os.path.islink(self.main))

    def test_the_file_is_synced_finished_and_the_rename_is_synced_too(self):
        # order matters: mode/owner on the descriptor, then the file sync,
        # then the rename, then a sync of the policy DIRECTORY
        calls = []
        real = {name: getattr(os, name) for name in
                ("fchmod", "fchown", "fsync", "replace")}

        def record(name):
            def wrapper(*args, **kwargs):
                if name == "fsync":
                    kind = ("dir" if stat.S_ISDIR(os.fstat(args[0]).st_mode)
                            else "file")
                    calls.append("fsync-" + kind)
                else:
                    calls.append(name)
                return real[name](*args, **kwargs)
            return wrapper

        with mock.patch.object(waf.os, "fchmod", record("fchmod")), \
                mock.patch.object(waf.os, "fchown", record("fchown")), \
                mock.patch.object(waf.os, "fsync", record("fsync")), \
                mock.patch.object(waf.os, "replace", record("replace")):
            waf._set_engine("On")
        self.assertEqual(calls, ["fchmod", "fchown", "fsync-file", "replace",
                                 "fsync-dir"])

    def test_a_failed_write_leaves_the_policy_and_no_temporary_file(self):
        with mock.patch.object(waf.os, "replace",
                               side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                waf._set_engine("On")
        with open(self.main, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), POLICY)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["main.conf"])


if __name__ == "__main__":
    unittest.main()
