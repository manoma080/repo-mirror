import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from repo_mirror import forgejo_sync
from repo_mirror.forgejo_git import APIError, ForgejoDestination, sync_refs, snapshot_refs
from repo_mirror.forgejo_destination_adapter import ForgejoDestinationAdapter
from test_forgejo_destination_adapter import FakeClient

URL = 'https://github.com/manoma080/mirror-test'

class TestForgejoRunPin(unittest.TestCase):
    def test_runner_rejects_same_provenance_replacement(self):
        for phase in ['after_git', 'before_release', 'before_completion']:
            with self.subTest(phase=phase), TemporaryDirectory() as directory:
                self._check_runner_rejects_same_provenance_replacement(Path(directory), phase)

    def _check_runner_rejects_same_provenance_replacement(self, tmp_path, phase):
        class API:
            def __init__(self):
                self.repo = None
                self.patches = []
            def request(self, method, path, payload=None):
                if method == 'POST':
                    self.repo = dict(payload, id=1, full_name='local/mirror-test')
                elif method == 'PATCH':
                    self.patches.append(dict(payload))
                    self.repo.update(payload)
                elif self.repo is None:
                    raise APIError(404)
                return dict(self.repo)
            def replace(self):
                self.repo['id'] = 2
        api = API()
        for filename, data in [('config', json.dumps([URL])), ('token', 'token'), ('secret', 'synthetic-recovery-fixture-value-32-bytes')]:
            p = tmp_path / filename
            p.write_text(data)
            p.chmod(0o600)
        argv = ['--config', str(tmp_path/'config'), '--source', URL, '--token-file', str(tmp_path/'token'),
                '--timestamp-secret-file', str(tmp_path/'secret'), '--journal-dir', str(tmp_path),
                '--owner', 'local', '--use-github-token']
        def sync(*a, **kw):
            if phase == 'after_git': api.replace()
            return 0
        def snapshot(*a, **kw):
            return (None, {}, {})
        def releases(*args, **kw):
            if phase == 'before_release':
                api.replace()
                args[4].request('POST', args[4].target + '/releases', {'tag_name': 'v1'})
            if phase == 'before_completion': api.replace()
        client = FakeClient()
        client.base = '/api/v1/repos/local/mirror-test'
        def adapter(owner_repo, token, port, **kw):
            return ForgejoDestinationAdapter(owner_repo, token, port, client=client, **kw)
        with patch.dict(os.environ, {'GITHUB_TOKEN': 'source'}), \
             patch.object(forgejo_sync, 'LocalForgejoAPI', return_value=api), \
             patch.object(forgejo_sync, 'sync_refs', side_effect=sync), \
             patch.object(forgejo_sync, 'snapshot_refs', side_effect=snapshot), \
             patch.object(forgejo_sync, 'ForgejoDestinationAdapter', side_effect=adapter), \
             patch.object(forgejo_sync, 'mirror_releases', side_effect=releases), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            assert forgejo_sync.main(argv) == 1
        assert not any('initialized:v1' in p.get('description', '') for p in api.patches)
        assert not client.calls


    def test_release_mutations_are_blocked_by_guard(self):
        for operation in ['create', 'update', 'delete', 'asset_update', 'asset_delete', 'upload']:
            with self.subTest(operation=operation):
                self._check_release_mutations_are_blocked_by_guard(operation)

    def _check_release_mutations_are_blocked_by_guard(self, operation):
        client = FakeClient()
        def guard():
            raise RuntimeError('repository replaced')
        adapter = ForgejoDestinationAdapter('team/project', 'token', client=client, guard=guard)
        adapter._assets['9'] = ('7', {'id': 9})
        target = adapter.target
        operations = {
            'create': lambda: adapter.request('POST', target + '/releases', {'tag_name': 'v1'}),
            'update': lambda: adapter.request('PATCH', target + '/releases/7', {'name': 'x'}),
            'delete': lambda: adapter.request('DELETE', target + '/releases/7'),
            'asset_update': lambda: adapter.request('PATCH', target + '/releases/assets/9', {'name': 'x'}),
            'asset_delete': lambda: adapter.request('DELETE', target + '/releases/assets/9'),
            'upload': lambda: adapter.upload(target + '/releases/7/assets?name=a', io.BytesIO(b'a'), 'text/plain'),
        }
        with self.assertRaisesRegex(RuntimeError, 'repository replaced'):
            operations[operation]()
        assert client.calls == []


    def test_release_patch_rechecks_after_read_before_write(self):
        client = FakeClient()
        def guard():
            if client.calls:
                raise RuntimeError('repository replaced')
        adapter = ForgejoDestinationAdapter('team/project', 'token', client=client, guard=guard)
        with self.assertRaisesRegex(RuntimeError, 'repository replaced'):
            adapter.request('PATCH', adapter.target + '/releases/7', {'name': 'x'})
        assert [call[0] for call in client.calls] == ['GET']


    def test_pinned_operations_never_adopt_or_recreate(self):
        for operation in ['sync', 'snapshot', 'complete']:
            for change in ['id', 'state', 'missing']:
                with self.subTest(change=change, operation=operation):
                    self._check_pinned_operations_never_adopt_or_recreate(change, operation)

    def _check_pinned_operations_never_adopt_or_recreate(self, change, operation):
        marker = 'repo-mirror-source: github.com/manoma080/mirror-test'
        initial = marker + ' [repo-mirror:initializing:v1]'
        class API:
            base_url = 'http://127.0.0.1:3300'
            calls = []
            def request(self, method, path, payload=None):
                self.calls.append(method)
                if change == 'missing':
                    raise APIError(404)
                return {'id': 2 if change == 'id' else 1, 'full_name': 'local/mirror-test',
                        'private': True, 'has_actions': False,
                        'description': marker + ' [repo-mirror:initialized:v1]' if change == 'state' else initial}
        api = API()
        dest = ForgejoDestination(api, 'local')
        args = (dest, 'mirror-test', 'manoma080/mirror-test')
        with self.assertRaises((RuntimeError, APIError)):
            if operation == 'sync':
                sync_refs(*args, source_token='source', expected=(1, initial))
            elif operation == 'snapshot':
                snapshot_refs(*args, source_token='source', expected=(1, initial))
            else:
                dest.complete_initialization(*args[1:], expected=(1, initial))
        assert api.calls == ['GET']
