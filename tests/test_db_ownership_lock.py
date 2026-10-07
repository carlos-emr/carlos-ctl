# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""The database-ownership lock the importer shares with every provisioning
path (carlos-emr/carlos#3678).

`finish-install`, carlos-emr-provision.service and both postinsts check the
o19 guard before they touch the database, but the guard cannot see an import
until the import has published its ledger. Without a shared lock a configure
could pass its guard check a moment before an import became visible and then
restart MariaDB, rewrite grants or run Flyway under the copy. These tests pin
the fix: import-o19 takes the same lock first, holds it while it runs, and
lets go on every way out. Neither side gets past a lock the other holds.

The "other side" is real wherever it can be: provision._acquire_lock as
finish-install runs it, and flock(1) exactly as the postinsts call it.

Run (from the repository root):
    python3 -m unittest discover -v -s tests -t .
"""

import contextlib
import fcntl
import io
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from carlos_ctl import o19host, o19import, provision, util
from tests.carlos_src import carlos_path, requires_carlos_src


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


class _LockDir(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="o19dblock-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.lock = os.path.join(self.root, "var", ".finish-install.lock")
        self.state_dir = os.path.join(self.root, "o19-import")
        # a test that fails mid-way must not leave the module holding a
        # descriptor every later test would then see as "already held"
        self.addCleanup(o19import.release_db_ownership_lock)
        self.addCleanup(o19import._WORKSPACE_LOCK.clear)

    def hold(self, path=None):
        """Take the lock from another open file description, as a
        concurrent run would; released at test cleanup."""
        path = path or self.lock
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)
        return fd

    def take(self):
        """take_db_ownership_lock; returns (raised SystemExit?, stderr)."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                o19import.take_db_ownership_lock(self.lock, self.state_dir)
            except SystemExit:
                return True, err.getvalue()
        return False, err.getvalue()


class TestOneFileForEveryone(unittest.TestCase):

    """One lock only works if everyone names the same file."""

    def test_finish_install_takes_the_shared_lock(self):
        self.assertEqual(provision.LOCK, util.DB_OWNERSHIP_LOCK)

    def test_the_name_in_the_field_is_kept(self):
        # every carlos-emr postinst already released opens this exact path;
        # renaming it would make a second lock that excludes nothing
        self.assertEqual(util.DB_OWNERSHIP_LOCK,
                         "/var/lib/carlos-emr/.finish-install.lock")

    def test_a_packaged_host_shares_it_with_the_importer(self):
        host = o19host.Host()
        with mock.patch.object(o19host.Host, "is_packaged_host",
                               return_value=True):
            self.assertEqual(host.db_ownership_lock_path(),
                             util.DB_OWNERSHIP_LOCK)

    def test_a_development_database_has_nothing_to_share(self):
        host = o19host.Host()
        with mock.patch.object(o19host.Host, "is_packaged_host",
                               return_value=False):
            self.assertIsNone(host.db_ownership_lock_path())

    @requires_carlos_src
    def test_both_postinsts_lock_the_same_file(self):
        # the postinsts spell it ${STATE}/.finish-install.lock with
        # STATE=/var/lib/carlos-emr; read both and resolve that one variable
        for pkg in ("carlos-emr", "carlos-emr-drugref"):
            with self.subTest(pkg=pkg):
                path = carlos_path("debian", pkg + ".postinst")
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                state = re.search(r'^STATE="?([^"\s]+)"?$', text, re.M)
                lock = re.search(r'^PROVISION_LOCK="\$\{STATE\}/([^"]+)"$',
                                 text, re.M)
                self.assertIsNotNone(state, "no STATE= in " + path)
                self.assertIsNotNone(lock, "no PROVISION_LOCK= in " + path)
                self.assertEqual(
                    os.path.join(state.group(1), lock.group(1)),
                    util.DB_OWNERSHIP_LOCK)


class TestTakeAndRelease(_LockDir):

    def test_it_is_held_once_taken_and_free_once_released(self):
        refused, _ = self.take()
        self.assertFalse(refused)
        self.assertTrue(_held_by_someone_else(self.lock))
        o19import.release_db_ownership_lock()
        self.assertFalse(_held_by_someone_else(self.lock))

    def test_release_without_a_lock_is_harmless(self):
        o19import.release_db_ownership_lock()
        o19import.release_db_ownership_lock()

    def test_taking_it_twice_in_one_process_keeps_one_descriptor(self):
        self.take()
        fd = o19import._DB_OWNERSHIP_LOCK["fd"]
        self.take()
        self.assertEqual(o19import._DB_OWNERSHIP_LOCK["fd"], fd)

    def test_no_path_means_nothing_to_take(self):
        o19import.take_db_ownership_lock(None, self.state_dir)
        self.assertEqual(o19import._DB_OWNERSHIP_LOCK, {})

    def test_the_lock_is_not_inherited_by_child_processes(self):
        # the importer spawns mariadb clients and `systemctl start` for the
        # backup; none of them may keep the database owned after it exits
        self.take()
        fd = o19import._DB_OWNERSHIP_LOCK["fd"]
        self.assertFalse(os.get_inheritable(fd))

    def test_a_held_lock_refuses_and_names_the_provisioning_runs(self):
        self.hold()
        refused, message = self.take()
        self.assertTrue(refused)
        self.assertEqual(o19import._DB_OWNERSHIP_LOCK, {})
        for holder in ("apt transaction", "carlos-emr-provision.service",
                       "finish-install"):
            self.assertIn(holder, message)
        self.assertIn("Nothing has been written", message)

    def test_a_held_lock_with_a_busy_workspace_names_another_import(self):
        self.hold()
        os.makedirs(self.state_dir)
        self.hold(os.path.join(self.state_dir, ".lock"))
        refused, message = self.take()
        self.assertTrue(refused)
        self.assertIn("another carlos-ctl import-o19 is running", message)

    def test_the_workspace_probe_does_not_keep_a_free_lock(self):
        os.makedirs(self.state_dir)
        workspace = os.path.join(self.state_dir, ".lock")
        open(workspace, "w").close()
        self.assertFalse(o19import.workspace_lock_busy(self.state_dir))
        self.assertFalse(_held_by_someone_else(workspace))

    def test_a_missing_workspace_is_not_busy(self):
        self.assertFalse(o19import.workspace_lock_busy(self.state_dir))

    def test_an_unopenable_lock_refuses_rather_than_running_unlocked(self):
        blocker = os.path.join(self.root, "file")
        open(blocker, "w").close()
        self.lock = os.path.join(blocker, "under-a-file.lock")
        refused, message = self.take()
        self.assertTrue(refused)
        self.assertIn("will not start", message)


class TestFinishInstallSide(_LockDir):

    """Lock first, then the guard, on the provisioning side too: while an
    import holds the lock, finish-install never reaches its guard check,
    let alone db-apply-settings, db-users or Flyway."""

    def acquire(self, boot):
        err = io.StringIO()
        with mock.patch.object(provision, "LOCK", self.lock), \
                mock.patch.object(provision, "_LOCK_HANDLE", None), \
                contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                result = provision._acquire_lock(boot)
                handle = provision._LOCK_HANDLE
            except SystemExit:
                return "refused", err.getvalue()
        if handle is not None:
            self.addCleanup(handle.close)
        return result, err.getvalue()

    def test_a_boot_repair_defers_to_a_running_import(self):
        self.take()
        result, _ = self.acquire(boot=True)
        self.assertIs(result, False)

    def test_a_hand_run_repair_is_told_an_import_may_hold_it(self):
        self.take()
        result, message = self.acquire(boot=False)
        self.assertEqual(result, "refused")
        self.assertIn("import-o19", message)

    def test_an_import_cannot_start_inside_a_repair(self):
        result, _ = self.acquire(boot=False)
        self.assertIs(result, True)
        refused, _ = self.take()
        self.assertTrue(refused)

    def test_finish_install_checks_the_guard_only_after_the_lock(self):
        # if the lock is not got, the guard is never consulted and nothing
        # after it runs
        self.take()
        guard = mock.Mock(return_value=None)
        with mock.patch.object(provision, "LOCK", self.lock), \
                mock.patch.object(provision, "_LOCK_HANDLE", None), \
                mock.patch.object(provision, "need_root"), \
                mock.patch.object(provision, "pending", return_value=True), \
                mock.patch.object(provision, "_o19_import_running", guard), \
                mock.patch.object(provision, "_wait_for_db") as db, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(provision.cmd_finish_install(["--boot"]), 0)
        guard.assert_not_called()
        db.assert_not_called()


@unittest.skipUnless(shutil.which("flock"), "flock(1) not installed")
class TestPostinstSide(_LockDir):

    """flock(1), invoked the way both postinsts invoke it."""

    def postinst_flock(self, wait):
        """`exec 9>LOCK; flock -w WAIT 9` in a shell; returns its status."""
        script = 'exec 9>"$1" || exit 2; flock -w "$2" 9'
        return subprocess.run(["sh", "-c", script, "sh", self.lock,
                               str(wait)], check=False).returncode

    def test_a_configure_defers_while_an_import_holds_the_lock(self):
        self.take()
        self.assertNotEqual(self.postinst_flock(0), 0)

    def test_a_configure_proceeds_once_the_import_has_ended(self):
        self.take()
        o19import.release_db_ownership_lock()
        self.assertEqual(self.postinst_flock(0), 0)

    def test_an_import_refuses_while_a_configure_holds_the_lock(self):
        os.makedirs(os.path.dirname(self.lock), exist_ok=True)
        configure = subprocess.Popen(
            ["sh", "-c", 'exec 9>"$1"; flock 9; echo held; sleep 30',
             "sh", self.lock], stdout=subprocess.PIPE, text=True)
        self.addCleanup(configure.stdout.close)
        self.addCleanup(configure.wait)
        self.addCleanup(configure.kill)
        self.assertEqual(configure.stdout.readline().strip(), "held")
        refused, message = self.take()
        self.assertTrue(refused)
        self.assertIn("apt transaction", message)

    def test_the_postinsts_truncating_open_does_not_break_the_lock(self):
        # `exec 9>` truncates the file under a holder; the lock is on the
        # inode, so a configure arriving mid-import still cannot take it
        self.take()
        with open(self.lock, "w", encoding="utf-8"):
            pass
        self.assertNotEqual(self.postinst_flock(0), 0)


class TestTheVerb(_LockDir):

    """`carlos-ctl import-o19` end to end, as far as the lock is
    concerned: taken before the first gate reads the workspace, held
    across the phases, released on every way out, and every mode --
    a real run, --resume, --cleanup, --dry-run -- takes it."""

    BASE = ["--mariadb-arg=--protocol=socket", "--skip-documents"]

    def verb(self, argv, **patches):
        """Run cmd_import_o19 with the host's lock and workspace pointed at
        this test's directory. Returns (exit code or None, stderr)."""
        err = io.StringIO()
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(
            o19host.Host, "db_ownership_lock_path",
            lambda _self: self.lock))
        stack.enter_context(mock.patch.object(o19host, "STATE_DIR",
                                              self.state_dir))
        for name, value in patches.items():
            stack.enter_context(mock.patch.object(o19import, name, value))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(contextlib.redirect_stderr(err))
        with stack:
            try:
                code = o19import.cmd_import_o19(list(self.BASE) + argv)
            except SystemExit as exc:
                code = exc.code if exc.code is not None else 0
        return code, err.getvalue()

    def test_every_mode_refuses_while_another_run_owns_the_database(self):
        self.hold()
        for flags in (["--admin-user", "brk", "--dump", "/x"],
                      ["--admin-user", "brk", "--resume"],
                      ["--cleanup"],
                      ["--dry-run", "--dump", "/x"]):
            with self.subTest(flags=flags):
                intake = mock.Mock()
                code, message = self.verb(
                    flags, _make_ctx=intake, _make_ctx_for_cleanup=intake)
                self.assertNotIn(code, (0, None))
                self.assertIn("owns the database", message)
                intake.assert_not_called()
                # nothing was published for the guard to read, either
                self.assertFalse(os.path.exists(
                    o19import.state_path(self.state_dir)))

    def test_resume_cannot_continue_while_another_import_runs(self):
        # the other run holds both locks, as a live import does
        o19import.save_state(self.state_dir, {
            "phases": {"check-pristine": {"status": "done"},
                       "backup": {"status": "done"}}})
        self.hold()
        self.hold(os.path.join(self.state_dir, ".lock"))
        intake = mock.Mock()
        code, message = self.verb(["--admin-user", "brk", "--resume"],
                                  _make_ctx=intake)
        self.assertNotIn(code, (0, None))
        self.assertIn("another carlos-ctl import-o19 is running", message)
        intake.assert_not_called()

    def test_the_lock_is_held_before_the_first_gate_reads_the_workspace(self):
        seen = {}

        def first_gate(state, state_dir):
            seen["held"] = _held_by_someone_else(self.lock)
            return "stop here"

        code, _ = self.verb(["--admin-user", "brk", "--dump", "/x"],
                            rewound_workspace_refusal=first_gate)
        self.assertNotIn(code, (0, None))
        self.assertTrue(seen["held"])

    def test_it_is_held_across_every_phase_and_released_after(self):
        held = []

        def phase(*_a, **_k):
            held.append(_held_by_someone_else(self.lock))

        code, _ = self.verb(
            ["--admin-user", "brk", "--dump", "/x"],
            load_state=lambda _d: {}, etl_started=lambda _d: False,
            webapp_running_refusal=lambda: None,
            _make_ctx=lambda *a, **k: {"province": "on",
                                       "dev_target": True,
                                       "state_dir": self.state_dir},
            run_p0=phase, run_p3=phase, run_p1=phase, run_p2=phase,
            run_p4=phase, run_p5=phase, run_p6=phase, run_p7=phase)
        self.assertEqual(code, 0)
        self.assertEqual(held, [True] * 8)
        self.assertFalse(_held_by_someone_else(self.lock))

    def test_an_aborted_phase_releases_it(self):
        def aborts(_ctx):
            o19import.die("ETL aborted: simulated")

        code, message = self.verb(
            ["--admin-user", "brk", "--resume"],
            load_state=lambda _d: {"phases": {"backup": {"status": "done"}}},
            etl_started=lambda _d: True,
            rewound_workspace_refusal=lambda _s, _d: None,
            webapp_running_refusal=lambda: None,
            _make_ctx=lambda *a, **k: {"province": "on",
                                       "dev_target": True,
                                       "state_dir": self.state_dir},
            run_p0=lambda c: None, run_p3=lambda c: None,
            run_p1=lambda c: None, run_p2=lambda c: None,
            run_p4=aborts)
        self.assertNotIn(code, (0, None))
        self.assertIn("simulated", message)
        self.assertFalse(_held_by_someone_else(self.lock))
        self.assertEqual(o19import._DB_OWNERSHIP_LOCK, {})

    def test_an_unexpected_exception_releases_it(self):
        def explodes(_ctx):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            self.verb(["--admin-user", "brk", "--dump", "/x"],
                      load_state=lambda _d: {},
                      etl_started=lambda _d: False,
                      webapp_running_refusal=lambda: None,
                      _make_ctx=lambda *a, **k: {"province": "on",
                                                 "dev_target": True},
                      run_p0=explodes)
        self.assertFalse(_held_by_someone_else(self.lock))

    def test_cleanup_holds_it_while_it_drops_and_releases_it_after(self):
        held = []
        code, _ = self.verb(
            ["--cleanup"],
            _make_ctx_for_cleanup=lambda a: {"state_dir": self.state_dir},
            run_cleanup=lambda c: held.append(
                _held_by_someone_else(self.lock)))
        self.assertEqual(code, 0)
        self.assertEqual(held, [True])
        self.assertFalse(_held_by_someone_else(self.lock))

    def test_a_refused_cleanup_releases_it(self):
        def refuses(_ctx):
            o19import.die("--cleanup is allowed only after verify has passed")

        code, _ = self.verb(
            ["--cleanup"],
            _make_ctx_for_cleanup=lambda a: {"state_dir": self.state_dir},
            run_cleanup=refuses)
        self.assertNotIn(code, (0, None))
        self.assertFalse(_held_by_someone_else(self.lock))

    def test_a_finish_install_waiting_on_an_import_gets_in_after_it(self):
        # the whole point: the import ends, and the repair the boot or the
        # operator deferred can now take the database
        def phase(_ctx):
            pass

        self.verb(["--admin-user", "brk", "--dump", "/x"],
                  load_state=lambda _d: {}, etl_started=lambda _d: False,
                  webapp_running_refusal=lambda: None,
                  _make_ctx=lambda *a, **k: {"province": "on",
                                             "dev_target": True,
                                             "state_dir": self.state_dir},
                  run_p0=phase, run_p3=phase, run_p1=phase, run_p2=phase,
                  run_p4=phase, run_p5=phase, run_p6=phase, run_p7=phase)
        with mock.patch.object(provision, "LOCK", self.lock), \
                mock.patch.object(provision, "_LOCK_HANDLE", None):
            self.assertTrue(provision._acquire_lock(boot=True))
            handle = provision._LOCK_HANDLE
        handle.close()


class TestAcrossProcesses(_LockDir):

    """The same exclusion between two real processes, so nothing above
    depends on locks being per-descriptor within one interpreter."""

    def test_a_second_process_cannot_take_it_from_a_running_import(self):
        self.take()
        code = ("import sys\n"
                "from carlos_ctl import o19import\n"
                "o19import.take_db_ownership_lock(sys.argv[1], sys.argv[2])\n")
        cp = subprocess.run(
            [os.environ.get("PYTHON", "python3"), "-c", code, self.lock,
             self.state_dir],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True, text=True, timeout=60, check=False)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("owns the database", cp.stderr)

    def test_it_is_free_the_moment_an_import_process_dies(self):
        code = ("import sys, time\n"
                "from carlos_ctl import o19import\n"
                "o19import.take_db_ownership_lock(sys.argv[1], sys.argv[2])\n"
                "print('held', flush=True)\n"
                "time.sleep(60)\n")
        child = subprocess.Popen(
            [os.environ.get("PYTHON", "python3"), "-c", code, self.lock,
             self.state_dir],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline().strip(), "held")
        self.assertTrue(_held_by_someone_else(self.lock))
        child.kill()
        child.wait(timeout=30)
        deadline = time.monotonic() + 10
        while _held_by_someone_else(self.lock):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
