# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Real process locks around the import dispatcher, without clinical SQL.

Runs against whichever carlos_ctl is importable, so CI also runs it against
the installed package. The dispatcher tests point the host's
db_ownership_lock_path() at a temporary file; tests/test_db_ownership_lock.py
covers the packaged-host path and the postinsts' flock(1) side.
"""
import contextlib
import io
import errno
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from carlos_ctl import o19host, o19import, o19props, provision

PEER = '''
import fcntl, sys
from carlos_ctl import provision
path, mode = sys.argv[1:]
if mode in ('boot', 'manual'):
    provision.LOCK = path
    sys.exit(0 if provision._acquire_lock(boot=mode == 'boot') else 4)
with open(path, 'a') as handle:
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(3)
    if mode == 'hold':
        print('ready', flush=True)
        sys.stdin.readline()
'''


class _LockedHost(o19host.Host):
    """A host whose provisioning lock is a temporary file."""

    def __init__(self, lock_path):
        self._lock_path = lock_path

    def db_ownership_lock_path(self):
        return self._lock_path


class TestImportOwnershipLock(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='carlos-ownership-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = str(self.root / 'o19-import')
        self.lock = self.root / '.finish-install.lock'
        self.events = []
        self.observe = lambda stage: None
        self.addCleanup(o19import.release_db_ownership_lock)

    def peer(self, mode='probe'):
        return subprocess.run([sys.executable, '-c', PEER, str(self.lock), mode],
                              capture_output=True, text=True, timeout=10)

    @contextlib.contextmanager
    def external_owner(self):
        process = subprocess.Popen([sys.executable, '-u', '-c', PEER,
                                    str(self.lock), 'hold'], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 10)[0],
                            'lock holder did not become ready')
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            yield process
        finally:
            try:
                process.communicate('release\n', timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)

    @contextlib.contextmanager
    def dispatcher(self, flags=(), preflight=False):
        """Keep the real CLI gates and lock; replace only phase work/SQL."""
        ctx = {'province': 'on', 'dev_target': True, 'state_dir': self.workspace}
        state = {'phases': {'check-pristine': {'status': 'done'}}} if '--resume' in flags else {}

        def phase(name, result=None):
            def call(*args, **kwargs):
                self.events.append(name)
                self.observe(name)
                return result
            return call

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(o19host, 'STATE_DIR', self.workspace))
            if not isinstance(o19import.HOST, _LockedHost):
                stack.enter_context(mock.patch.object(
                    o19import, 'HOST', _LockedHost(str(self.lock))))
            stack.enter_context(mock.patch.object(o19import.os, 'geteuid', return_value=0))
            stack.enter_context(mock.patch.object(o19import, 'load_state', return_value=state))
            stack.enter_context(mock.patch.object(o19import, 'etl_started', return_value=False))
            stack.enter_context(mock.patch.object(o19import, 'webapp_running_refusal', return_value=None))
            for name in ('_make_ctx', '_make_ctx_for_cleanup'):
                stack.enter_context(mock.patch.object(o19import, name, side_effect=phase(name, ctx)))
            for name in ('run_p0', 'run_p3', 'run_p1', 'run_p4', 'run_p5', 'run_p6',
                         'run_p7', 'run_cleanup', 'run_p0_capacity'):
                stack.enter_context(mock.patch.object(o19import, name, side_effect=phase(name)))
            stack.enter_context(mock.patch.object(o19import, 'run_p2',
                                side_effect=phase('run_p2', {'verdict': 'go', 'exit_code': 0})))
            stack.enter_context(mock.patch.object(o19props, 'run_props', side_effect=phase('run_props')))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            if preflight:
                yield lambda: o19import.cmd_o19_preflight(list(flags))
            else:
                yield lambda: o19import.cmd_import_o19(['--admin-user', 'MigrationAdmin', *flags])

    def refused(self, flags=(), preflight=False):
        with self.external_owner(), self.dispatcher(flags, preflight) as run:
            with self.assertRaises(SystemExit) as refusal:
                run()
            self.assertNotEqual(refusal.exception.code, 0)
        self.assertEqual(self.events, [], 'a refused import must not enter setup or any phase')
        self.assertEqual(self.peer().returncode, 0)

    def test_fresh_import_refuses_provisioning_owner_before_setup(self):
        self.refused()

    def test_resume_reacquires_database_ownership_before_setup(self):
        self.refused(['--resume'])

    def test_cleanup_refuses_provisioning_owner_before_setup(self):
        self.refused(['--cleanup'])

    def test_dry_run_cannot_stage_while_provisioning_owns_database(self):
        self.refused(['--dry-run'])

    def test_preflight_cannot_stage_while_provisioning_owns_database(self):
        self.refused(preflight=True)

    def assert_provisioning_excluded(self, stage):
        for mode, expected in [('probe', 3), ('boot', 4), ('manual', 1)]:
            with self.subTest(stage=stage, peer=mode):
                result = self.peer(mode)
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_import_excludes_configure_and_both_provisioning_modes_through_p7(self):
        self.observe = self.assert_provisioning_excluded
        with self.dispatcher() as run:
            self.assertEqual(run(), 0)
        self.assertEqual(self.events, ['_make_ctx', 'run_p0', 'run_p3', 'run_p1',
                                      'run_p2', 'run_p4', 'run_p5', 'run_p6', 'run_p7'])
        self.assertEqual(self.peer('manual').returncode, 0)

    def test_cleanup_holds_ownership_until_cleanup_returns_then_releases(self):
        self.observe = self.assert_provisioning_excluded
        with self.dispatcher(['--cleanup']) as run:
            self.assertEqual(run(), 0)
        self.assertEqual(self.events, ['_make_ctx_for_cleanup', 'run_cleanup'])
        self.assertEqual(self.peer().returncode, 0)

    def test_cleanup_failure_releases_ownership(self):
        for stage in ('_make_ctx_for_cleanup', 'run_cleanup'):
            with self.subTest(stage=stage):
                def observe(current):
                    self.assertEqual(self.peer().returncode, 3)
                    if current == stage:
                        raise SystemExit(9)
                self.observe = observe
                with self.dispatcher(['--cleanup']) as run, self.assertRaises(SystemExit):
                    run()
                self.assertEqual(self.peer('manual').returncode, 0)

    def test_import_uses_the_deployment_ownership_lock(self):
        self.lock = self.root / 'port-provisioning.lock'
        lock_path = str(self.lock)

        class Port(_LockedHost):
            def __init__(self):
                super().__init__(lock_path)

        self.observe = self.assert_provisioning_excluded
        with mock.patch.object(o19import, 'HOST', Port()), self.dispatcher() as run:
            self.assertEqual(run(), 0)
        self.assertFalse((self.root / '.finish-install.lock').exists())
        self.assertEqual(self.peer().returncode, 0)

    def test_preflight_holds_ownership_for_staging_and_report_then_releases(self):
        self.observe = self.assert_provisioning_excluded
        with self.dispatcher(preflight=True) as run:
            self.assertEqual(run(), 0)
        self.assertEqual(self.events, ['_make_ctx', 'run_p0_capacity', 'run_p1', 'run_p2'])
        self.assertEqual(self.peer().returncode, 0)

    def test_setup_and_phase_aborts_release_ownership(self):
        for stage in ('_make_ctx', 'run_p0', 'run_p1', 'run_p4', 'run_p7'):
            for exception in (SystemExit(9), KeyboardInterrupt(), RuntimeError('phase failure')):
                with self.subTest(stage=stage, error=type(exception).__name__):
                    def observe(current):
                        self.assertEqual(self.peer().returncode, 3)
                        if current == stage:
                            raise exception
                    self.observe = observe
                    with self.dispatcher() as run, self.assertRaises(type(exception)):
                        run()
                    self.assertEqual(self.peer('manual').returncode, 0)

    @contextlib.contextmanager
    def ownership(self):
        o19import.take_db_ownership_lock(str(self.lock), self.workspace)
        try:
            yield
        finally:
            o19import.release_db_ownership_lock()

    def test_lock_inode_and_contents_survive_success_and_failure(self):
        self.lock.write_text('another process may already have this inode open')
        original = self.lock.stat().st_ino
        with self.ownership():
            self.assertEqual(self.peer().returncode, 3)
        with self.assertRaises(RuntimeError):
            with self.ownership():
                raise RuntimeError('abort')
        self.assertEqual(self.lock.stat().st_ino, original)
        self.assertEqual(self.lock.read_text(), 'another process may already have this inode open')

    def test_a_second_take_in_one_process_keeps_the_single_owner(self):
        # The importer holds one descriptor per process; taking it again (an
        # import entry point reached twice) must neither self-deadlock nor
        # open a second descriptor that could drop the first one's lock.
        with self.ownership():
            o19import.take_db_ownership_lock(str(self.lock), self.workspace)
            self.assertEqual(self.peer().returncode, 3)
        self.assertEqual(self.peer().returncode, 0)

    def test_unsafe_lock_files_are_refused_without_modifying_their_target(self):
        target = self.root / 'untouched'
        target.write_text('keep')
        for kind in ('symlink', 'fifo', 'directory'):
            with self.subTest(kind=kind), contextlib.redirect_stderr(io.StringIO()):
                if kind == 'symlink':
                    self.lock.symlink_to(target)
                elif kind == 'fifo':
                    os.mkfifo(self.lock)
                else:
                    self.lock.mkdir()
                try:
                    with self.assertRaises(SystemExit):
                        with self.ownership():
                            self.fail('non-regular lock entered the operation')
                    self.assertEqual(target.read_text(), 'keep')
                finally:
                    self.lock.rmdir() if kind == 'directory' else self.lock.unlink()

    def test_acquisition_errors_close_the_opened_descriptor(self):
        for error in (BlockingIOError(errno.EAGAIN, 'busy'), OSError(errno.EIO, 'I/O failure')):
            opened = []
            real_open = os.open
            def capture(*args, **kwargs):
                descriptor = real_open(*args, **kwargs)
                opened.append(descriptor)
                return descriptor
            with self.subTest(error=type(error).__name__), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.object(o19import.os, 'open', side_effect=capture), \
                    mock.patch('fcntl.flock', side_effect=error):
                with self.assertRaises(SystemExit):
                    with self.ownership():
                        self.fail('acquisition failed but the operation ran')
            self.assertEqual(len(opened), 1)
            with self.assertRaises(OSError) as closed:
                os.fstat(opened[0])
            self.assertEqual(closed.exception.errno, errno.EBADF)

    def test_process_termination_releases_database_ownership(self):
        script = """
import sys
from carlos_ctl import o19import
o19import.take_db_ownership_lock(sys.argv[1], sys.argv[2])
print('ready', flush=True)
sys.stdin.readline()
"""
        process = subprocess.Popen([sys.executable, '-u', '-c', script,
                                    str(self.lock), self.workspace],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 10)[0])
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            self.assertEqual(self.peer('boot').returncode, 4)
            process.terminate()
            process.wait(timeout=5)
            self.assertEqual(self.peer('manual').returncode, 0)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_debian_host_and_provisioner_name_the_same_lock(self):
        # Parent state storage is shared even though import owns a child workspace.
        with mock.patch.object(o19host.Host, 'is_packaged_host', return_value=True):
            self.assertEqual(o19host.Host().db_ownership_lock_path(), provision.LOCK)


if __name__ == '__main__':
    unittest.main()
