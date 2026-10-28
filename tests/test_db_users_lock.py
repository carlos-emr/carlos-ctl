# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""db-users and the database-ownership lock (carlos-emr/carlos-ctl#10 review).

db-users reads the recorded passwords, sets them in MariaDB and records the
results. Two runs interleaving there could each generate a password and leave
carlos.properties recording one generation while MariaDB accepts the other.
So db-users holds the database-ownership lock that finish-install, both
postinsts and import-o19 already share, from before its first read to after
its last write. Its callers that already hold that lock must still get
through: a postinst runs `carlos-ctl db-users` holding it on fd 9, and
finish-install calls db-users in its own process.

The other side is real wherever it matters: a separate process holding the
lock, flock(1) on fd 9 exactly as the postinsts take it, and two db-users
runs started side by side.

Run (from the repository root):
    python3 -m unittest discover -v -s tests -t .
"""

import contextlib
import fcntl
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from carlos_ctl import provision, util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A child process that takes the lock the way db-users does and says how.
_TAKE = """
import sys
from unittest import mock
from carlos_ctl import util
with mock.patch.object(util, "DB_OWNERSHIP_LOCK", sys.argv[1]):
    held = util.hold_db_ownership_lock("db-users")
print("reused" if held is None else "opened", flush=True)
"""


def _held_by_someone_else(path):
    """Whether a fresh open file description can NOT lock `path` right now.
    flock locks belong to the open file description, so a second open in
    this same process contends exactly as another process would."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


def _python(code, *args, **kw):
    """Run `code` in a fresh interpreter that imports this checkout."""
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, "-c", code, *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60, **kw)


class _LockDir(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="db-users-lock-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.lock = os.path.join(self.root, "var", ".finish-install.lock")
        patcher = mock.patch.object(util, "DB_OWNERSHIP_LOCK", self.lock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def take(self):
        """hold_db_ownership_lock as db-users calls it: (refused?, fd, stderr)."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                fd = util.hold_db_ownership_lock("db-users")
            except SystemExit:
                return True, None, err.getvalue()
        if fd is not None:
            self.addCleanup(os.close, fd)
        return False, fd, err.getvalue()

    def another_run(self):
        """A separate process holding the lock until the test ends."""
        os.makedirs(os.path.dirname(self.lock), exist_ok=True)
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl, os, sys, time\n"
             "fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT, 0o644)\n"
             "fcntl.flock(fd, fcntl.LOCK_EX)\n"
             "print('held', flush=True)\n"
             "time.sleep(60)\n", self.lock],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        return holder


class TestTheLockDbUsersTakes(_LockDir):

    def test_a_free_lock_is_taken(self):
        refused, fd, _ = self.take()
        self.assertFalse(refused)
        self.assertIsNotNone(fd)
        self.assertTrue(_held_by_someone_else(self.lock))

    def test_it_is_released_once_its_descriptor_closes(self):
        fd = util.hold_db_ownership_lock("db-users")
        self.assertTrue(_held_by_someone_else(self.lock))
        os.close(fd)
        self.assertFalse(_held_by_someone_else(self.lock))

    def test_another_run_holding_it_is_a_refusal_not_a_wait(self):
        self.another_run()
        started = time.monotonic()
        refused, fd, message = self.take()
        self.assertTrue(refused)
        self.assertIsNone(fd)
        self.assertLess(time.monotonic() - started, 5)
        for holder in ("apt transaction", "carlos-emr-provision.service",
                       "finish-install", "import-o19", "another db-users"):
            self.assertIn(holder, message)
        self.assertIn("Nothing has been changed", message)
        self.assertIn("carlos-ctl db-users", message)

    def test_finish_install_holding_it_in_this_process_is_let_through(self):
        # finish-install takes the lock, then calls db-users in-process; a
        # fresh open would contend with its own descriptor and refuse
        with mock.patch.object(provision, "LOCK", self.lock), \
                mock.patch.object(provision, "_LOCK_HANDLE", None), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(provision._acquire_lock(False))
            handle = provision._LOCK_HANDLE
        self.addCleanup(handle.close)
        refused, fd, _ = self.take()
        self.assertFalse(refused)
        self.assertIsNone(fd)
        # and db-users leaves the lock with finish-install
        self.assertTrue(_held_by_someone_else(self.lock))

    def test_the_descriptor_it_opens_is_not_inherited(self):
        # db-users runs mariadb clients; none may keep the database owned
        _refused, fd, _ = self.take()
        self.assertFalse(os.get_inheritable(fd))

    def test_an_unopenable_lock_refuses_rather_than_running_unlocked(self):
        blocker = os.path.join(self.root, "file")
        open(blocker, "w").close()
        with mock.patch.object(util, "DB_OWNERSHIP_LOCK",
                               os.path.join(blocker, "under-a-file.lock")):
            refused, _fd, message = self.take()
        self.assertTrue(refused)
        self.assertIn("will not start", message)


class TestAcrossProcesses(_LockDir):

    def test_a_child_without_the_lock_is_refused_while_another_run_holds_it(self):
        self.another_run()
        child = _python(_TAKE, self.lock)
        self.assertEqual(child.returncode, 1, child.stderr)
        self.assertIn("another CARLOS run owns the database", child.stderr)

    @unittest.skipUnless(shutil.which("flock"), "flock(1) not installed")
    def test_a_postinst_holding_it_on_fd_9_lets_the_carlos_ctl_it_runs_through(self):
        # exactly the postinsts' form: open fd 9, flock it, then run carlos-ctl,
        # which inherits fd 9 across exec
        os.makedirs(os.path.dirname(self.lock), exist_ok=True)
        script = ('exec 9>"$1" || exit 2; flock -n 9 || exit 3; '
                  'PYTHONPATH="$2" PYTHONDONTWRITEBYTECODE=1 exec "$3" -c "$4" "$1"')
        postinst = subprocess.run(["sh", "-c", script, "sh", self.lock, ROOT,
                                   sys.executable, _TAKE],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(postinst.returncode, 0, postinst.stderr)
        self.assertEqual(postinst.stdout.strip(), "reused")
        # the shell and its child have exited, so the lock went with fd 9
        self.assertFalse(_held_by_someone_else(self.lock))

    @unittest.skipUnless(shutil.which("flock"), "flock(1) not installed")
    def test_an_inherited_descriptor_does_not_override_another_runs_lock(self):
        # fd 9 is open on the file but another run holds the lock: the child
        # must still be refused, not let through on a descriptor alone
        self.another_run()
        script = ('exec 9>"$1" || exit 2; '
                  'PYTHONPATH="$2" PYTHONDONTWRITEBYTECODE=1 exec "$3" -c "$4" "$1"')
        child = subprocess.run(["sh", "-c", script, "sh", self.lock, ROOT,
                                sys.executable, _TAKE],
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(child.returncode, 1, child.stderr)
        self.assertIn("another CARLOS run owns the database", child.stderr)


# One db-users run in a child process: everything external is replaced, and
# the fake MariaDB appends each reporting password it is set to to server.log.
# The run that reaches MariaDB first then waits for the test's go-ahead, which
# holds open the window in which a second run could interleave.
_DB_USERS = r"""
import argparse, os, sys, time
from unittest import mock
from carlos_ctl import dbops, util

lock, work, me = sys.argv[1:4]
arrivals = os.path.join(work, "arrivals")


class Cp:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def fake_db_root(args, **kw):
    if "-e" in args:
        return Cp(0, "0\n")
    for line in kw.get("input", "").splitlines():
        if line.startswith("ALTER USER 'carlos_report'@'localhost' IDENTIFIED BY '"):
            with open(os.path.join(work, "server.log"), "a") as fh:
                fh.write(me + " " + line.split("'")[5] + "\n")
    with open(arrivals, "a") as fh:
        fh.write(me + "\n")
    with open(arrivals) as fh:
        first = fh.read().split()[0] == me
    deadline = time.monotonic() + 30
    while first and not os.path.exists(os.path.join(work, "go")) and time.monotonic() < deadline:
        time.sleep(0.05)
    return Cp(0)


with mock.patch.object(util, "DB_OWNERSHIP_LOCK", lock), \
        mock.patch.object(dbops, "need_root", lambda verb: None), \
        mock.patch.object(dbops, "require_db_root", lambda: None), \
        mock.patch.object(dbops.config, "load", lambda: argparse.Namespace(db_name="carlos")), \
        mock.patch.object(dbops, "db_root", fake_db_root), \
        mock.patch.object(dbops, "run", lambda *a, **k: Cp(3)), \
        mock.patch.object(dbops, "PROPERTIES", os.path.join(work, "carlos.properties")), \
        mock.patch.object(dbops, "DRUGREF_PROPERTIES", os.path.join(work, "absent")), \
        mock.patch.object(dbops, "BACKUP_ENV", os.path.join(work, "absent.env")):
    sys.exit(dbops.cmd_db_users([]))
"""


class TestTwoDbUsersRuns(_LockDir):

    """Two first-run db-users side by side: without the lock, the second
    sets MariaDB to its password and records it, then the first records its
    own, so carlos.properties and MariaDB disagree. With it, the second is
    refused before it reads anything."""

    def setUp(self):
        super().setUp()
        self.work = os.path.join(self.root, "work")
        os.makedirs(self.work)
        with open(os.path.join(self.work, "carlos.properties"), "w", encoding="latin-1") as fh:
            fh.write(f"db_username = carlos\ndb_password = {util.genpw()}\n"
                     "db_reporting_username =\ndb_reporting_password =\n")

    def start(self, name):
        env = dict(os.environ, PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1")
        run = subprocess.Popen([sys.executable, "-c", _DB_USERS, self.lock, self.work, name],
                               cwd=ROOT, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
        self.addCleanup(run.wait)
        self.addCleanup(run.kill)
        return run

    def read(self, name):
        path = os.path.join(self.work, name)
        if not os.path.exists(path):
            return ""
        with open(path, encoding="latin-1") as fh:
            return fh.read()

    def test_the_second_run_is_refused_and_mariadb_agrees_with_the_file(self):
        first = self.start("first")
        deadline = time.monotonic() + 30
        while "first" not in self.read("arrivals").split():
            if first.poll() is not None:
                self.fail("the first run ended before reaching MariaDB: " + first.stderr.read())
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        # `first` is inside its SQL batch now, holding the lock
        second = self.start("second")
        _out, second_err = second.communicate(timeout=60)
        open(os.path.join(self.work, "go"), "w").close()
        _out, first_err = first.communicate(timeout=60)

        self.assertEqual(first.returncode, 0, first_err)
        self.assertEqual(second.returncode, 1, second_err)
        self.assertIn("another CARLOS run owns the database", second_err)
        server = self.read("server.log").split("\n")
        self.assertEqual([line.split()[0] for line in server if line], ["first"])
        accepted = server[0].split()[1]
        recorded = util.prop_unescape(util.prop_get(
            os.path.join(self.work, "carlos.properties"), "db_reporting_password") or "")
        self.assertEqual(recorded, accepted)


if __name__ == "__main__":
    unittest.main()
