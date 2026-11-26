import unittest
import json
import os
import tempfile
import io
import urllib.error
from contextlib import redirect_stderr
from email.message import Message
from unittest.mock import patch, ANY
from repo_mirror import forgejo_sync
from repo_mirror.forgejo_releases import SyncConflict
from repo_mirror.forgejo_git import APIError

URL = 'https://github.com/mirror-owner/mirror-fixture-org-d-fixture-repo'


class RunnerTests(unittest.TestCase):
    def setUp(self):
        source_api = patch('repo_mirror.forgejo_sync.SourceAPI', autospec=True)
        self.github = source_api.start()
        self.addCleanup(source_api.stop)
        self.github.return_value.pages.return_value = [
            {'id': 1, 'tag_name': 'v1', 'name': 'Version 1', 'body': 'Release notes',
             'draft': False, 'prerelease': False, 'assets': []}
        ]
        self.github.return_value.request.return_value = dict(self.github.return_value.pages.return_value[0])

    @patch.dict(os.environ, {'GITHUB_TOKEN': 'test'})
    def test_recovery_secret_length_preflight(self):
        from contextlib import nullcontext
        with tempfile.TemporaryDirectory() as directory:
            config = os.path.join(directory, 'config')
            token = os.path.join(directory, 'token')
            secret = os.path.join(directory, 'secret')
            with open(config, 'w') as f: json.dump([URL], f)
            with open(token, 'w') as f: f.write('test-token')
            os.chmod(token, 0o600)
            args = ['--config', config, '--source', URL, '--token-file', token,
                    '--timestamp-secret-file', secret, '--journal-dir', directory,
                    '--owner', 'local', '--use-github-token']
            with open(secret, 'w') as f: f.write('x' * 31 + '\n')
            os.chmod(secret, 0o600)
            before = set(os.listdir(directory))
            with patch('repo_mirror.forgejo_sync.LocalForgejoAPI') as api, \
                 patch('repo_mirror.forgejo_sync.destination_locks') as locks, \
                 redirect_stderr(io.StringIO()):
                self.assertEqual(forgejo_sync.main(args), 2)
                api.assert_not_called()
                locks.assert_not_called()
            self.assertEqual(set(os.listdir(directory)), before)
            with open(secret, 'w') as f: f.write('x' * 32 + '\n')
            with patch('repo_mirror.forgejo_sync.LocalForgejoAPI'), \
                 patch('repo_mirror.forgejo_sync.destination_locks', return_value=nullcontext()), \
                 patch('repo_mirror.forgejo_sync.journal_lock', return_value=nullcontext(directory)), \
                 patch('repo_mirror.forgejo_sync._sync_requested', return_value=0) as sync:
                self.assertEqual(forgejo_sync.main(args), 0)
                self.assertEqual(sync.call_args.args[5], 'x' * 32)

    @patch.dict(os.environ, {'GITHUB_TOKEN': 'test'})
    def test_interrupted_initialization_resumes_then_becomes_incremental(self):
        class API:
            def __init__(self, *args):
                self.repo = None
            def token(self): return 'token'
            def request(self, method, path, payload=None):
                if path == '/api/v1/user': return {'login': 'local'}
                if method == 'POST':
                    self.repo = dict(payload, id=1, full_name='local/mirror-fixture-org-d-fixture-repo')
                elif method == 'PATCH':
                    self.repo.update(payload)
                elif self.repo is None:
                    raise APIError(404)
                return dict(self.repo)
        api = API()
        with tempfile.TemporaryDirectory() as directory:
            config, secret = os.path.join(directory, 'config'), os.path.join(directory, 'secret')
            with open(config, 'w') as f: json.dump([URL], f)
            with open(secret, 'w') as f: f.write('synthetic-recovery-fixture-value-32-bytes')
            os.chmod(secret, 0o600)
            token_file = os.path.join(directory, 'token')
            with open(token_file, 'w') as token_stream: token_stream.write('local-token')
            os.chmod(token_file, 0o600)
            argv = ['--config', config, '--source', URL, '--token-file', token_file, '--owner', 'local',
                    '--use-github-token', '--timestamp-secret-file', secret, '--journal-dir', directory]
            modes = []
            def releases(*args, **kwargs):
                modes.append(kwargs['initializing'])
                if len(modes) == 1: raise RuntimeError('interrupted')
            with patch('repo_mirror.forgejo_sync.LocalForgejoAPI', return_value=api), \
                 patch('repo_mirror.forgejo_sync.sync_refs', return_value=0), \
                 patch('repo_mirror.forgejo_sync.snapshot_refs', return_value=(None, {}, {})), \
                 patch('repo_mirror.forgejo_sync.ForgejoDestinationAdapter'), \
                 patch('repo_mirror.forgejo_sync.mirror_releases', side_effect=releases):
                self.assertEqual(forgejo_sync.main(argv), 1)
                self.assertIn('[repo-mirror:initializing:v1]', api.repo['description'])
                self.assertEqual(forgejo_sync.main(argv), 0)
                self.assertEqual(forgejo_sync.main(argv), 0)
            self.assertEqual(modes, [True, True, False])

    def test_private_key_rejects_insecure_file_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            key = os.path.join(directory, 'key')
            link = os.path.join(directory, 'link')
            with open(key, 'w') as f:
                f.write('fake')
            os.chmod(key, 0o644)
            with self.assertRaises(ValueError):
                forgejo_sync.read_private_key(key)
            os.chmod(key, 0o600)
            os.symlink(key, link)
            with self.assertRaises(OSError):
                forgejo_sync.read_private_key(link)

    def test_app_credentials_are_shared_as_refreshable_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = os.path.join(directory, 'secret')
            with open(secret, 'w') as f: f.write('synthetic-recovery-fixture-value-32-bytes')
            os.chmod(secret, 0o600)
            token_file = os.path.join(directory, 'token')
            with open(token_file, 'w') as token_stream: token_stream.write('local-token')
            os.chmod(token_file, 0o600)
            config = os.path.join(directory, 'config')
            key = os.path.join(directory, 'key')
            with open(config, 'w') as f:
                json.dump([URL], f)
            with open(key, 'w') as f:
                f.write('fake-key')
            os.chmod(key, 0o600)
            provider = lambda: 'fake-token'
            with patch('repo_mirror.forgejo_sync.AppTokenProvider', return_value=provider) as app, \
                 patch('repo_mirror.forgejo_sync.LocalForgejoAPI'), \
                 patch('repo_mirror.forgejo_sync.sync_refs', return_value=0) as refs, \
                 patch('repo_mirror.forgejo_sync.snapshot_refs', return_value=(None, {}, {})), \
                 patch('repo_mirror.forgejo_sync.ForgejoDestinationAdapter'), \
                 patch('repo_mirror.forgejo_sync.SourceAPI') as github, \
                 patch('repo_mirror.forgejo_sync.mirror_releases'), \
                 patch('repo_mirror.forgejo_sync.ForgejoDestination.begin_initialization', return_value=False):
                github.return_value.pages.return_value = []
                github.return_value.request.side_effect = urllib.error.HTTPError('https://api.github.com', 404, 'not found', Message(), None)
                result = forgejo_sync.main(['--config', config, '--source', URL, '--token-file', token_file,
                                            '--timestamp-secret-file', secret, '--journal-dir', directory, '--owner', 'local', '--app-id', '123', '--private-key-file', key, '--source-app-owner', 'mirror-owner'])
            self.assertEqual(result, 0)
            app.assert_called_once_with('123', 'fake-key', 'mirror-owner')
            self.assertIs(refs.call_args.kwargs['source_token'], provider)
            self.assertIs(github.call_args.args[0], provider)
            github.return_value.pages.assert_called_once_with('https://api.github.com/repos/mirror-owner/mirror-fixture-org-d-fixture-repo/releases')

    def run_main(self, configured, sources):
        with tempfile.TemporaryDirectory() as directory, tempfile.NamedTemporaryFile(mode='w', suffix='.json') as f:
            secret = os.path.join(directory, 'secret')
            with open(secret, 'w') as key: key.write('synthetic-recovery-fixture-value-32-bytes')
            os.chmod(secret, 0o600)
            token_file = os.path.join(directory, 'token')
            with open(token_file, 'w') as token_stream: token_stream.write('local-token')
            os.chmod(token_file, 0o600)
            json.dump(configured, f)
            f.flush()
            args = ['--config', f.name, '--token-file', token_file, '--owner', 'forgejo-owner', '--use-github-token', '--timestamp-secret-file', secret, '--journal-dir', directory]
            for source in sources:
                args += ['--source', source]
            with patch('repo_mirror.forgejo_sync.ForgejoDestination.begin_initialization', return_value=False):
                return forgejo_sync.main(args)

    @patch('repo_mirror.forgejo_sync.ForgejoDestinationAdapter')
    @patch.dict(os.environ, {'GITHUB_TOKEN': 'test'})
    @patch('repo_mirror.forgejo_sync.mirror_releases')
    @patch('repo_mirror.forgejo_sync.sync_refs', return_value=1)
    @patch('repo_mirror.forgejo_sync.LocalForgejoAPI')
    @patch('repo_mirror.forgejo_sync.snapshot_refs', return_value=(None, {}, {}))
    def test_runs_exact_source_and_both_syncs(self, snapshot, api, refs, releases, adapter):
        api.return_value.token.return_value = 'test-local'
        self.assertEqual(self.run_main([URL], [URL]), 0)
        self.assertEqual(refs.call_args.args[1:], ('mirror-fixture-org-d-fixture-repo', 'mirror-owner/mirror-fixture-org-d-fixture-repo'))
        releases.assert_called_once()
        args, kwargs = releases.call_args
        self.assertEqual(args[0], {'type': 'github', 'base_url': 'https://github.com', 'owner': 'mirror-owner'})
        self.assertEqual(args[5], 'https://api.github.com/repos/mirror-owner/mirror-fixture-org-d-fixture-repo')
        self.assertFalse(kwargs['initializing'])
        self.assertIsNotNone(kwargs['journal'])

    @patch('repo_mirror.forgejo_sync.LocalForgejoAPI')
    def test_unconfigured_or_duplicate_sources_do_not_write(self, api):
        self.assertEqual(self.run_main([URL], [URL + '-other']), 2)
        self.assertEqual(self.run_main([URL], [URL, URL]), 2)
        api.assert_not_called()

    @patch.dict(os.environ, {'GITHUB_TOKEN': 'test'})
    @patch('repo_mirror.forgejo_sync.sync_refs', side_effect=RuntimeError('secret'))
    @patch('repo_mirror.forgejo_sync.LocalForgejoAPI')
    def test_sync_failure_is_nonzero(self, api, refs):
        self.assertEqual(self.run_main([URL], [URL]), 1)
        self.github.return_value.pages.assert_called_once_with('https://api.github.com/repos/mirror-owner/mirror-fixture-org-d-fixture-repo/releases')
        refs.assert_called_once()

    @patch.dict(os.environ, {'GITHUB_TOKEN': 'test'})
    @patch('repo_mirror.forgejo_sync.LocalForgejoAPI')
    @patch('repo_mirror.forgejo_sync.sync_refs')
    @patch('repo_mirror.forgejo_sync.mirror_releases')
    def test_unsafe_source_preflight_blocks_ref_and_release_writes(self, releases, refs, api):
        self.github.return_value.pages.return_value = [
            {'id': 1, 'tag_name': 'v1', 'name': 'Version 1',
             'body': '[repo-mirror:pending:v3]', 'draft': False, 'assets': []}
        ]
        self.assertEqual(self.run_main([URL], [URL]), 1)
        self.github.return_value.pages.assert_called_once_with('https://api.github.com/repos/mirror-owner/mirror-fixture-org-d-fixture-repo/releases')
        refs.assert_not_called()
        releases.assert_not_called()
        api.return_value.request.assert_not_called()

    @patch.dict(os.environ, {'GITHUB_TOKEN': 'test'})
    @patch('repo_mirror.forgejo_sync.LocalForgejoAPI')
    def test_conflicts_are_actionable_and_unknown_errors_are_redacted(self, api):
        for error, expected in [(SyncConflict('release metadata collision'), 'release metadata collision'),
                                (RuntimeError('secret https://host/token'), 'unexpected sync failure')]:
            output = io.StringIO()
            with self.subTest(error=error), patch('repo_mirror.forgejo_sync.sync_refs', side_effect=error), redirect_stderr(output):
                self.assertEqual(self.run_main([URL], [URL]), 1)
                self.assertIn(expected, output.getvalue())
                self.assertNotIn('secret', output.getvalue())
                self.assertNotIn('https://host', output.getvalue())

    @patch.dict(os.environ, {'GITHUB_TOKEN': 'test'})
    @patch('repo_mirror.forgejo_sync.LocalForgejoAPI')
    @patch('repo_mirror.forgejo_sync.sync_refs', return_value=0)
    def test_ref_changes_during_release_sync_fail(self, refs, api):
        import subprocess
        oid, other = 'a' * 40, 'b' * 40
        for changed_side in ('source', 'destination', 'stable'):
            state = {'releases_done': False}
            def run(args, **kwargs):
                source = any('github.com/' in a for a in args)
                changed = state['releases_done'] and changed_side != 'stable' and (source == (changed_side == 'source'))
                value = other if changed else oid
                if '--symref' in args:
                    output = f'ref: refs/heads/main\tHEAD\n{oid}\tHEAD\n'.encode()
                else:
                    output = f'{oid}\trefs/heads/main\n{value}\trefs/tags/v1\n'.encode()
                self.assertNotIn('push', args)
                return subprocess.CompletedProcess(args, 0, output, b'')
            api.return_value.base_url = 'http://127.0.0.1:3000'
            api.return_value.token.return_value = 'local'
            api.return_value.request.return_value = {'id': 1, 'full_name': 'forgejo-owner/mirror-fixture-org-d-fixture-repo',
                'private': True, 'has_actions': False, 'default_branch': 'main',
                'description': 'repo-mirror-source: github.com/mirror-owner/mirror-fixture-org-d-fixture-repo'}
            def releases(*args, **kwargs):
                state['releases_done'] = True
            with self.subTest(side=changed_side), patch('repo_mirror.forgejo_sync.ForgejoDestinationAdapter'), patch('repo_mirror.forgejo_sync.mirror_releases', side_effect=releases), \
                 patch('repo_mirror.forgejo_git.subprocess.run', side_effect=run):
                self.assertEqual(self.run_main([URL], [URL]), 0 if changed_side == 'stable' else 1)
                self.assertTrue(state['releases_done'])

    @patch.dict(os.environ, {'GITHUB_TOKEN': 'test', 'MIRROR_TIMESTAMP_SECRET': 'unrelated'})
    @patch('repo_mirror.forgejo_sync.ForgejoDestinationAdapter')
    @patch('repo_mirror.forgejo_sync.LocalForgejoAPI')
    @patch('repo_mirror.forgejo_sync.sync_refs', return_value=0)
    @patch('repo_mirror.forgejo_sync.snapshot_refs', return_value=(None, {}, {}))
    def test_journal_persists_and_local_secret_is_scoped(self, snapshot, refs, api, adapter):
        with tempfile.TemporaryDirectory() as directory:
            config, secret = os.path.join(directory, 'config'), os.path.join(directory, 'secret')
            with open(config, 'w') as f: json.dump([URL], f)
            with open(secret, 'w') as f: f.write('synthetic-recovery-fixture-value-32-bytes')
            os.chmod(secret, 0o600)
            token_file = os.path.join(directory, 'token')
            with open(token_file, 'w') as token_stream: token_stream.write('local-token')
            os.chmod(token_file, 0o600)
            argv = ['--config', config, '--source', URL, '--token-file', token_file, '--owner', 'local',
                    '--use-github-token', '--timestamp-secret-file', secret, '--journal-dir', directory]
            def release(*args, **kwargs):
                self.assertEqual(os.environ['MIRROR_TIMESTAMP_SECRET'], 'synthetic-recovery-fixture-value-32-bytes')
                journal = kwargs['journal']
                key = journal.key(args[0], args[1], args[2], 'v1', 'asset')
                if not journal.failures:
                    journal.set(key, True)
                else:
                    self.assertIn(key, journal.failures)
            with patch('repo_mirror.forgejo_sync.mirror_releases', side_effect=release), \
                 patch('repo_mirror.forgejo_sync.ForgejoDestination.begin_initialization', return_value=False):
                self.assertEqual(forgejo_sync.main(argv), 0)
                self.assertEqual(forgejo_sync.main(argv), 0)
            self.assertEqual(os.environ['MIRROR_TIMESTAMP_SECRET'], 'unrelated')

    def test_configuration_os_errors_do_not_expose_paths(self):
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(forgejo_sync.main(['--config', '/secret-token/not-found', '--source', URL, '--token-file', 'x', '--owner', 'x', '--timestamp-secret-file', 'x', '--journal-dir', 'x']), 2)
        self.assertNotIn('secret-token', output.getvalue())

    def test_safe_error_categories(self):
        cases = [
            (APIError(403), 'Forgejo API authentication or authorization failed'),
            (urllib.error.HTTPError('https://secret/token', 404, 'secret', Message(), None), 'remote API resource not found'),
            (urllib.error.URLError('secret'), 'network or API transport failure'),
            (RuntimeError('destination ref diverged or tag moved; refusing update'), 'destination ref diverged or tag moved; refusing update'),
            (SyncConflict('release metadata collision secret'), 'unexpected sync failure'),
        ]
        for error, expected in cases:
            with self.subTest(error=error):
                self.assertEqual(forgejo_sync.safe_reason(error), expected)



class SourceTests(unittest.TestCase):
    def test_any_github_repository_is_accepted(self):
        # Parity with the GitHub runner: any configured GitHub source works, not
        # only the mirror-* repositories owned by the mirror account.
        cases = {
            'https://github.com/mirror-owner/mirror-fixture-org-d-fixture-repo': 'mirror-owner/mirror-fixture-org-d-fixture-repo',
            'https://github.com/other/mirror-x': 'other/mirror-x',
            'https://github.com/octocat/Hello-World': 'octocat/Hello-World',
            'https://github.com/some-user/repo.name_1': 'some-user/repo.name_1',
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(forgejo_sync.parse_source(url), expected)

    def test_forgejo_sources_keep_the_host_qualified_identity(self):
        self.assertEqual(forgejo_sync.parse_source('https://git.example/owner/repo'),
                         'https://git.example/owner/repo')

    def test_malformed_sources_are_rejected(self):
        for invalid in ('https://github.com.evil/owner/repo', 'https://github.com/owner/repo.git',
                        'https://github.com/owner/repo?x=1', 'https://github.com/owner/repo/extra',
                        'https://github.com/owner', 'http://github.com/owner/repo',
                        'https://user:pw@github.com/owner/repo', 'https://github.com/owner/',
                        'https://github.com/owner/..'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                forgejo_sync.parse_source(invalid)
