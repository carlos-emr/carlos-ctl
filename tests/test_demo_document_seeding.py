# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""`dbops._demo_seed_document_files`: the demo document store seeding.

The function copies the shipped demo PDFs and HRM XML files into the
document store after the demo SQL has loaded. It is best-effort by design (a
failed copy must not make the SQL load look incomplete), so every swallowed
failure in it is a place where the operator could be left with a half-seeded
store and no trace. These tests pin which failures are silent on purpose and
which must reach the operator:

  * a document the running application stored under a fixture's name between
    the exists() check and the link() is kept, silently -- that is the
    designed outcome, not a failure;
  * the temporary file is removed whether the copy succeeded, was lost to a
    link race, or died half-way, and a copy that never created it is not
    worth a word;
  * a temporary file that cannot be removed (anything but "already gone")
    leaves a stray dot-file in the clinical document store, so it is
    reported rather than swallowed (issue carlos-emr/carlos#3605).

Run (from the repository root):
    python3 -m unittest discover -v -s tests -t .
"""

import contextlib
import errno
import io
import os
import shutil
import stat
import tempfile
import types
import unittest
from unittest import mock

from carlos_ctl import dbops

PARTIAL_SUFFIX = ".carlos-demo-partial"


class TestDemoDocumentSeeding(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.demo = os.path.join(tmp.name, "demo")
        self.source = os.path.join(self.demo, "documents")
        self.state = os.path.join(tmp.name, "state")
        self.dest = os.path.join(self.state, "CarlosDocument", "carlos", "document")
        os.makedirs(self.source)
        # The function chowns to the `carlos` account; point that account at
        # the user running the suite so the real os.chown succeeds unprivileged.
        account = types.SimpleNamespace(pw_uid=os.getuid())
        group = types.SimpleNamespace(gr_gid=os.getgid())
        for patcher in (
                mock.patch.object(dbops, "DEMO_DIR", self.demo),
                mock.patch.object(dbops, "STATE", self.state),
                mock.patch("pwd.getpwnam", return_value=account),
                mock.patch("grp.getgrnam", return_value=group)):
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- helpers -----------------------------------------------------------

    def fixture(self, name, data=b"%PDF-1.4 fictitious demo report\n"):
        with open(os.path.join(self.source, name), "wb") as fh:
            fh.write(data)

    def seed(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            dbops._demo_seed_document_files()
        return out.getvalue(), err.getvalue()

    def stored(self, name):
        with open(os.path.join(self.dest, name), "rb") as fh:
            return fh.read()

    def leftovers(self):
        return sorted(n for n in os.listdir(self.dest) if n.endswith(PARTIAL_SUFFIX))

    # -- the happy path ----------------------------------------------------

    def test_a_fixture_is_copied_with_the_upload_mode_and_no_temporary_left(self):
        self.fixture("report.pdf", b"%PDF-1.4 one")
        out, err = self.seed()
        self.assertEqual(self.stored("report.pdf"), b"%PDF-1.4 one")
        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.join(self.dest, "report.pdf")).st_mode), 0o640)
        self.assertEqual(self.leftovers(), [])
        self.assertIn("(1 copied)", out)
        self.assertEqual(err, "")

    def test_an_existing_document_is_never_overwritten(self):
        self.fixture("report.pdf", b"fixture")
        os.makedirs(self.dest)
        with open(os.path.join(self.dest, "report.pdf"), "wb") as fh:
            fh.write(b"a real upload")
        out, err = self.seed()
        self.assertEqual(self.stored("report.pdf"), b"a real upload")
        self.assertEqual(self.leftovers(), [])
        self.assertIn("(0 copied)", out)
        self.assertEqual(err, "")

    # -- the link() race: silent on purpose --------------------------------

    def test_a_document_stored_by_the_application_mid_copy_is_kept_silently(self):
        self.fixture("report.pdf", b"fixture")
        real_link = os.link

        def application_wins_the_race(src, dst):
            with open(dst, "wb") as fh:
                fh.write(b"stored by the running application")
            return real_link(src, dst)  # now refuses: the target exists

        with mock.patch.object(os, "link", application_wins_the_race):
            out, err = self.seed()
        self.assertEqual(self.stored("report.pdf"), b"stored by the running application")
        self.assertEqual(self.leftovers(), [])
        self.assertIn("(0 copied)", out)
        self.assertEqual(err, "", "losing the link race is the designed outcome, not a warning")

    # -- a copy that fails -------------------------------------------------

    def test_a_copy_that_never_created_the_temporary_warns_once_and_not_about_cleanup(self):
        self.fixture("report.pdf")
        disk_full = OSError(errno.ENOSPC, "No space left on device")
        with mock.patch("shutil.copyfile", side_effect=disk_full):
            out, err = self.seed()
        self.assertIn("could not seed demo document report.pdf", err)
        # nothing was created, so "already gone" at cleanup is expected and silent
        self.assertNotIn("could not remove", err)
        self.assertFalse(os.path.exists(os.path.join(self.dest, "report.pdf")))
        self.assertEqual(self.leftovers(), [])
        self.assertIn("(0 copied)", out)

    def test_a_copy_that_dies_half_way_leaves_no_truncated_file_behind(self):
        self.fixture("report.pdf")

        def dies_after_a_few_bytes(src, dst):
            with open(dst, "wb") as fh:
                fh.write(b"%PDF-1.4 trunc")
            raise OSError(errno.EIO, "Input/output error")

        with mock.patch("shutil.copyfile", side_effect=dies_after_a_few_bytes):
            _, err = self.seed()
        self.assertIn("could not seed demo document report.pdf", err)
        self.assertNotIn("could not remove", err)
        self.assertFalse(os.path.exists(os.path.join(self.dest, "report.pdf")),
                         "a truncated file must never appear under the real name")
        self.assertEqual(self.leftovers(), [])

    # -- a temporary that cannot be removed: must be reported --------------

    def test_a_temporary_that_cannot_be_removed_is_reported_and_seeding_continues(self):
        self.fixture("a.pdf", b"first")
        self.fixture("b.pdf", b"second")
        real_unlink = os.unlink

        def refuses_the_temporary(path, *args, **kwargs):
            if str(path).endswith(PARTIAL_SUFFIX):
                raise PermissionError(errno.EACCES, "Permission denied", path)
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(os, "unlink", refuses_the_temporary):
            out, err = self.seed()
        # both documents landed, and each failed cleanup is named in the output
        self.assertEqual(self.stored("a.pdf"), b"first")
        self.assertEqual(self.stored("b.pdf"), b"second")
        self.assertEqual(err.count("could not remove temporary demo document file"), 2)
        self.assertIn(".a.pdf" + PARTIAL_SUFFIX, err)
        self.assertIn(".b.pdf" + PARTIAL_SUFFIX, err)
        self.assertIn("Permission denied", err)
        self.assertIn("(2 copied)", out)

    def test_a_stranded_temporary_is_reported_without_any_mocked_filesystem_call(self):
        # The same outcome with nothing mocked in os or shutil: a directory
        # squatting on the temporary name makes copyfile fail and the cleanup
        # unlink fail with EISDIR, which is not "already gone". Pins the
        # behaviour against the real kernel, not only against the doubles above.
        self.fixture("report.pdf")
        os.makedirs(os.path.join(self.dest, ".report.pdf" + PARTIAL_SUFFIX))
        _, err = self.seed()
        self.assertIn("could not seed demo document report.pdf", err)
        self.assertIn("could not remove temporary demo document file", err)
        self.assertIn(".report.pdf" + PARTIAL_SUFFIX, err)
        self.assertFalse(os.path.exists(os.path.join(self.dest, "report.pdf")))

    def test_a_temporary_already_gone_at_cleanup_is_not_reported(self):
        self.fixture("report.pdf")
        real_unlink = os.unlink

        def already_gone(path, *args, **kwargs):
            real_unlink(path, *args, **kwargs)
            if str(path).endswith(PARTIAL_SUFFIX):
                raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)

        with mock.patch.object(os, "unlink", already_gone):
            _, err = self.seed()
        self.assertEqual(self.stored("report.pdf"), b"%PDF-1.4 fictitious demo report\n")
        self.assertEqual(err, "")

    # -- the documented early returns --------------------------------------

    def test_a_missing_source_directory_warns_and_creates_nothing(self):
        shutil.rmtree(self.source)
        out, err = self.seed()
        self.assertIn("demo document files not seeded", err)
        self.assertFalse(os.path.exists(self.dest))
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
