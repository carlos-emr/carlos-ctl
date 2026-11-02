# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise the code embedded in preinst before the package is unpacked."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

PREINST = Path(__file__).resolve().parents[1] / 'debian/preinst'
SCOPE = {'__name__': 'preinst_test'}
CODE = PREINST.read_text().split("<<'PRE_COPY_UPGRADE' || refuse\n", 1)[1].split('\nPRE_COPY_UPGRADE', 1)[0]
exec(compile(CODE, str(PREINST), 'exec'), SCOPE)


class TestInstallRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'o19-import'
        self.directory.mkdir()
        self.state = {'inputs': {'target_contract_sha256': 'a' * 64,
                                 'schema_map_version': 'o19map-5+12345678'},
                      'phases': {'backup': {'status': 'done'}, 'stage': {'status': 'done'}}}
        self.write_state()

    def write_state(self):
        (self.directory / 'state.json').write_text(json.dumps(self.state))

    def allowed(self):
        return SCOPE['stopped_before_copy'](self.directory)

    def test_stopped_pre_copy_and_rename_only_progress_are_allowed(self):
        self.assertTrue(self.allowed())
        (self.directory / 'etl-progress.json').write_text(json.dumps(
            {'tables': {}, 'renamed_tables': ['consent -> Consent']}))
        self.assertTrue(self.allowed())

    def test_table_or_admin_write_is_refused(self):
        """A table mark or created administrator makes package replacement unsafe."""
        for data in ({'tables': {'Consent': {'started': True}}},
                     {'tables': {}, 'admin_provider_no': '1'}, {'tables': []}):
            (self.directory / 'etl-progress.json').write_text(json.dumps(data))
            self.assertFalse(self.allowed())

    def test_unknown_or_malformed_phases_fail_closed(self):
        """Future or corrupted phases cannot be interpreted as a stopped import."""
        for phases in ({'future-copy': {'status': 'done'}},
                       {'backup': None}, {'stage': []}, {'preflight': 'done'},
                       {'stage': {}}, {'stage': {'status': 'unknown'}}):
            with self.subTest(phases=phases):
                self.state['phases'] = phases
                self.write_state()
                self.assertFalse(self.allowed())

    def test_nonregular_locks_are_refused_before_fork(self):
        """FIFO and symlink paths must neither block nor redirect exclusion."""
        for path in (self.directory.parent / '.finish-install.lock', self.directory / '.lock'):
            for kind in ('fifo', 'symlink'):
                with self.subTest(path=path.name, kind=kind):
                    if path.exists():
                        path.unlink()
                    target = self.directory.parent / 'lock-target'
                    target.write_text('unchanged')
                    if kind == 'fifo':
                        os.mkfifo(path)
                    else:
                        path.symlink_to(target)
                    try:
                        with mock.patch.dict(SCOPE, {'dpkg_identity': lambda _: 'fixed'}), \
                                mock.patch.object(SCOPE['sys'], 'argv', ['preinst', str(self.directory / 'state.json'), '1', '/missing-guard']), \
                                mock.patch.object(SCOPE['os'], 'fork') as fork:
                            with self.assertRaises((OSError, ValueError)):
                                SCOPE['main']()
                            fork.assert_not_called()
                        self.assertEqual(target.read_text(), 'unchanged')
                    finally:
                        path.unlink()

    def test_later_phases_cleanup_and_unbound_state_are_refused(self):
        """Only a recognized pre-copy ledger with package bindings is eligible."""
        for phase in ('etl', 'documents', 'props', 'verify'):
            self.state['phases'][phase] = {'status': 'failed'}
            self.write_state()
            self.assertFalse(self.allowed())
            del self.state['phases'][phase]
        self.state['inputs']['target_contract_sha256'] = None
        self.write_state()
        with self.assertRaises(TypeError):
            self.allowed()

    def test_running_workspace_is_refused_before_fork(self):
        with (self.directory / '.lock').open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with mock.patch.dict(SCOPE, {'dpkg_identity': lambda _: 'fixed'}), \
                    mock.patch.object(SCOPE['sys'], 'argv', ['preinst', str(self.directory / 'state.json'), '1', '/missing-guard']), \
                    mock.patch.object(SCOPE['os'], 'fork') as fork:
                with self.assertRaises(BlockingIOError):
                    SCOPE['main']()
                fork.assert_not_called()

    def test_fresh_import_without_ledger_is_refused_while_it_owns_database(self):
        """An importer can own the database before publishing its first ledger."""
        (self.directory / 'state.json').unlink()
        with (self.directory.parent / '.finish-install.lock').open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with mock.patch.dict(SCOPE, {'dpkg_identity': lambda _: 'fixed'}), \
                    mock.patch.object(SCOPE['sys'], 'argv', ['preinst', str(self.directory / 'state.json'), '1', '/missing-guard']), \
                    mock.patch.object(SCOPE['os'], 'fork') as fork:
                with self.assertRaises(BlockingIOError):
                    SCOPE['main']()
                fork.assert_not_called()

    def test_real_holder_excludes_imports_but_allows_inactive_configure(self):
        """Keep both locks for recovery and workspace exclusion alone otherwise."""
        for state in ('missing', 'completed', 'pre-copy'):
            with self.subTest(state=state):
                ledger = self.directory / 'state.json'
                self.write_state()
                if state == 'missing':
                    ledger.unlink()
                guard = self.directory.parent / 'guard'
                guard.write_text('#!/bin/sh\nexit ' + ('1' if state == 'pre-copy' else '0') + '\n')
                guard.chmod(0o700)
                # A real supervising process, identified exactly as dpkg is,
                # runs the embedded preinst and then probes both shared locks.
                supervisor = '''
import ctypes,fcntl,json,os,subprocess,sys
from pathlib import Path
ctypes.CDLL(None).prctl(15,b'dpkg',0,0,0)
ledger=Path(sys.argv[1]);guard=sys.argv[2]
code=sys.stdin.read()
result=subprocess.run([sys.executable,'-c',code,str(ledger),str(os.getpid()),guard],capture_output=True,text=True,timeout=5)
if result.returncode:raise RuntimeError(result.stderr)
held=[]
for path in (ledger.parent.parent/'.finish-install.lock',ledger.parent/'.lock'):
 with path.open('a') as handle:
  try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB);held.append(False)
  except BlockingIOError:held.append(True)
print(json.dumps(held))
'''
                result = subprocess.run([sys.executable, '-c', supervisor, str(ledger), str(guard)],
                                        input=CODE, capture_output=True, text=True, timeout=8)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), [state == 'pre-copy', True])
                # The holder must also release its workspace after dpkg exits.
                with (self.directory / '.lock').open('a') as handle:
                    deadline = time.monotonic() + 3
                    while True:
                        try:
                            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                self.fail('workspace lock outlived its dpkg supervisor')
                            time.sleep(0.05)
