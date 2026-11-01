# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise the code embedded in preinst before the package is unpacked."""
import fcntl
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

PREINST = Path(__file__).resolve().parents[1] / 'debian/preinst'
SCOPE = {'__name__': 'preinst_test'}
exec(compile(PREINST.read_text().split("<<'PRE_COPY_UPGRADE' || refuse\n", 1)[1]
             .split('\nPRE_COPY_UPGRADE', 1)[0], str(PREINST), 'exec'), SCOPE)


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
                                mock.patch.object(SCOPE['sys'], 'argv', ['preinst', str(self.directory / 'state.json'), '1']), \
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
                    mock.patch.object(SCOPE['sys'], 'argv', ['preinst', str(self.directory / 'state.json'), '1']), \
                    mock.patch.object(SCOPE['os'], 'fork') as fork:
                with self.assertRaises(BlockingIOError):
                    SCOPE['main']()
                fork.assert_not_called()
