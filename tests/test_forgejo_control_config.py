import base64
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request

from repo_mirror.forgejo_git import APIError
from repo_mirror.migrations import blob_sha
from test_migrations_journal import SECRET, entry


class ContentsAPI:
    def __init__(self):
        self.repo = dict(id=42, full_name='forgejo-owner/control', private=True, has_actions=False)
        self.files = {'config.json': b'{"before": true}\n'}
        self.requests = []
        self.conflict = False
        self.ignore_update = False

    def request(self, method, path, payload=None):
        self.requests.append((method, path, payload))
        prefix = '/api/v1/repos/forgejo-owner/control'
        if path == prefix and method == 'GET':
            return self.repo.copy()
        if not path.startswith(prefix + '/contents/'):
            raise AssertionError(path)
        name, _, query = path.removeprefix(prefix + '/contents/').partition('?')
        if name not in self.files:
            raise APIError(404)
        data = self.files[name]
        if method == 'GET' and query == 'ref=refs/heads/main':
            return dict(type='file', sha=blob_sha(data), encoding='base64',
                        content=base64.b64encode(data).decode())
        if method == 'PUT' and not query:
            assert payload is not None
            if self.conflict or payload['sha'] != blob_sha(data):
                raise APIError(409)
            if payload['branch'] != 'main':
                raise AssertionError(payload['branch'])
            if not self.ignore_update:
                self.files[name] = base64.b64decode(payload['content'], validate=True)
            return {}
        raise AssertionError((method, path))


class ForgejoControlConfigTests(unittest.TestCase):
    def setUp(self):
        from repo_mirror.forgejo_control_config import ForgejoFile
        self.api = ContentsAPI()
        self.config = ForgejoFile(self.api, 'forgejo-owner', 'control', 'config.json', expected_id=42)

    def test_replace_publishes_exact_bytes_under_blob_sha(self):
        before = self.config.read()
        after = b'{"after": true}\n'
        self.config.replace(before, after)
        self.assertEqual(self.config.read(), after)
        writes = [payload for method, _, payload in self.api.requests if method == 'PUT']
        self.assertEqual([write['sha'] for write in writes], [blob_sha(before)])

    def test_builder_selects_forgejo_for_both_manifest_and_signed_journal(self):
        from repo_mirror.forgejo_migration import build_migration
        args = SimpleNamespace(forgejo_config_repo='forgejo-owner/control', forgejo_config_id=42,
                               config_repo=None, owner='forgejo-owner', port=3000,
                               source_app_owner='mirror-owner',
                               token_file='unused', app_id='123', private_key_file='github.pem')
        self.api.files['migrations.json'] = b'{"version": 1, "migrations": {}, "signatures": {}}'
        with (patch('repo_mirror.forgejo_control_config.ForgejoControlAPI', return_value=self.api),
              patch('repo_mirror.forgejo_sync.read_secure_file', return_value=('test-fixture', None)),
              patch('repo_mirror.github_api.AppTokenProvider') as provider,
              patch('repo_mirror.github_api.SourceAPI') as source):
            tx = build_migration(args, SECRET)
            assert tx is not None
            provider.assert_called_once_with('123', 'test-fixture', 'mirror-owner', 'personal', role='source')
            source.return_value.request.return_value = {'id': 1234}
            tx.source('https://github.com', 'newowner', 'repo')
            source.assert_called_once_with(provider.return_value)
            source.return_value.restrict_origin.assert_called_once_with('https://api.github.com')
        self.assertEqual(tx.config_path, 'config.json')
        self.assertEqual(tx.cas.read(), self.api.files['config.json'])
        tx.store.record(entry())
        self.assertEqual(tx.store.get('https://github.com', 1234), entry())
        tampered = json.loads(self.api.files['migrations.json'])
        next(iter(tampered['migrations'].values()))['phase'] = 'renamed'
        self.api.files['migrations.json'] = json.dumps(tampered).encode()
        with self.assertRaises(ValueError):
            tx.pending()

    def test_stale_snapshot_does_not_write(self):
        self.api.files['config.json'] = b'changed by another writer'
        with self.assertRaisesRegex(ValueError, 'file changed'):
            self.config.replace(b'older snapshot', b'new data')
        self.assertFalse(any(method == 'PUT' for method, _, _ in self.api.requests))

    def test_conflict_is_not_retried(self):
        self.api.conflict = True
        with self.assertRaisesRegex(ValueError, 'file changed'):
            self.config.replace(self.config.read(), b'new data')
        self.assertEqual(sum(method == 'PUT' for method, _, _ in self.api.requests), 1)

    def test_success_without_persisted_bytes_is_rejected(self):
        self.api.ignore_update = True
        with self.assertRaisesRegex(ValueError, 'readback mismatch'):
            self.config.replace(self.config.read(), b'new data')

    def test_repository_metadata_must_match_before_reading_files(self):
        for change in ({'id': 43}, {'id': True}, {'private': False}, {'has_actions': True},
                       {'full_name': 'forgejo-owner/replacement'}):
            with self.subTest(change=change):
                original = self.api.repo.copy()
                self.api.repo.update(change)
                self.api.requests.clear()
                with self.assertRaisesRegex(ValueError, 'repository identity'):
                    self.config.read()
                self.assertEqual(len(self.api.requests), 1)
                self.api.repo = original

    def test_missing_journal_is_not_treated_as_empty(self):
        from repo_mirror.forgejo_control_config import ForgejoMigrations
        from repo_mirror.migrations_store import MigrationsStore
        store = MigrationsStore(ForgejoMigrations(self.api, 'forgejo-owner', 'control', expected_id=42), SECRET)
        with self.assertRaises(APIError) as missing:
            store.pending()
        self.assertEqual(missing.exception.status, 404)

    def test_legacy_builder_keeps_github_config_and_local_journal(self):
        from repo_mirror.forgejo_authoritative_config import GitHubConfigCAS
        from repo_mirror.forgejo_migration import build_migration
        from repo_mirror.migrations_store import FileMigrations
        args = SimpleNamespace(config_repo='legacy/config', owner='forgejo-owner', port=3000,
                               token_file='unused', journal_dir='/unused')
        with (patch.dict(os.environ, {'GITHUB_TOKEN': 'fixture-github'}),
              patch('repo_mirror.forgejo_sync.read_secure_file', return_value=('test-fixture', None))):
            tx = build_migration(args, SECRET)
        assert tx is not None
        self.assertIsInstance(tx.cas, GitHubConfigCAS)
        self.assertIsInstance(tx.store.backend, FileMigrations)
        self.assertEqual(tx.config_path, 'config-forgejo.json')

    def test_forgejo_builder_requires_a_valid_config_repo_id(self):
        from repo_mirror.forgejo_migration import build_migration
        for invalid_id in (None, True, 0, '42'):
            with self.subTest(invalid_id=invalid_id), self.assertRaisesRegex(ValueError, 'repository ID'):
                build_migration(SimpleNamespace(forgejo_config_repo='forgejo-owner/control',
                                                forgejo_config_id=invalid_id), SECRET)

    def test_legacy_config_does_not_follow_foreign_redirects(self):
        from repo_mirror.forgejo_migration import build_migration
        from repo_mirror.github_api import OriginRedirect
        args = SimpleNamespace(config_repo='legacy/config', owner='forgejo-owner', port=3000,
                               token_file='unused', journal_dir='/unused')
        with (patch.dict(os.environ, {'GITHUB_TOKEN': 'fixture-github'}),
              patch('repo_mirror.forgejo_sync.read_secure_file', return_value=('test-fixture', None))):
            tx = build_migration(args, SECRET)
        assert tx is not None
        redirect = next(handler for handler in tx.cas.api.opener.handlers if isinstance(handler, OriginRedirect))
        with self.assertRaisesRegex(ValueError, 'outside configured origin'):
            redirect.redirect_request(Request(tx.cas.url), None, 302, '', {}, 'https://other.example/config.json')

    def test_redirect_is_refused_without_a_second_request(self):
        from repo_mirror.forgejo_control_config import ForgejoControlAPI
        api = ForgejoControlAPI('http://127.0.0.1:3000', 'unused')
        contents = '/api/v1/repos/forgejo-owner/control/contents/config.json?ref=refs/heads/main'
        with patch('repo_mirror.forgejo_control_config.http.client.HTTPConnection') as connection:
            conn = connection.return_value
            conn.getresponse.return_value.status = 307
            conn.getresponse.return_value.getheader.return_value = 'https://other.example/config.json'
            with self.assertRaises(APIError) as redirected:
                api.request('GET', contents, token='test-fixture')
            self.assertEqual(redirected.exception.status, 307)
            connection.assert_called_once_with('127.0.0.1', 3000, timeout=30)
            conn.request.assert_called_once_with('GET', contents, body=None, headers={
                'Authorization': 'token test-fixture', 'Content-Type': 'application/json',
            })
            conn.close.assert_called_once()
