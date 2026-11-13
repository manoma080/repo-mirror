import unittest
import json
import os
import tempfile
import io
import urllib.error
from contextlib import redirect_stderr
from email.message import Message
from unittest.mock import patch
from repo_mirror import forgejo_sync
from repo_mirror.forgejo_releases import SyncConflict
from repo_mirror.forgejo_git import APIError

URL = 'https://github.com/manoma080/mirror-Gezine-CivSwitch'


class RunnerTests(unittest.TestCase):
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
                 patch('repo_mirror.forgejo_sync.GitHubReleases') as github, \
                 patch('repo_mirror.forgejo_sync.sync_releases'):
                result = forgejo_sync.main(['--config', config, '--source', URL, '--token-file', 'x',
                                            '--owner', 'local', '--app-id', '123', '--private-key-file', key])
            self.assertEqual(result, 0)
            app.assert_called_once_with('123', 'fake-key', 'manoma080')
            self.assertIs(refs.call_args.kwargs['source_token'], provider)
            self.assertIs(github.call_args.args[1], provider)

    def run_main(self, configured, sources):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json') as f:
            json.dump(configured, f)
            f.flush()
            args = ['--config', f.name, '--token-file', '/not-used', '--owner', 'labadmin', '--use-github-token']
            for source in sources:
                args += ['--source', source]
            return forgejo_sync.main(args)

    @patch.dict(os.environ, {'GITHUB_TOKEN': 'test'})
    @patch('repo_mirror.forgejo_sync.sync_releases')
    @patch('repo_mirror.forgejo_sync.sync_refs', return_value=1)
    @patch('repo_mirror.forgejo_sync.LocalForgejoAPI')
    @patch('repo_mirror.forgejo_sync.snapshot_refs', return_value=(None, {}, {}))
    def test_runs_exact_source_and_both_syncs(self, snapshot, api, refs, releases):
        api.return_value.token.return_value = 'test-local'
        self.assertEqual(self.run_main([URL], [URL]), 0)
        self.assertEqual(refs.call_args.args[1:], ('mirror-Gezine-CivSwitch', 'manoma080/mirror-Gezine-CivSwitch'))
        releases.assert_called_once()

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
            api.return_value.base_url = 'http://127.0.0.1:3300'
            api.return_value.token.return_value = 'local'
            api.return_value.request.return_value = {'full_name': 'labadmin/mirror-Gezine-CivSwitch',
                'private': True, 'has_actions': False, 'default_branch': 'main',
                'description': 'repo-mirror-source: github.com/manoma080/mirror-Gezine-CivSwitch'}
            def releases(*args):
                state['releases_done'] = True
            with self.subTest(side=changed_side), patch('repo_mirror.forgejo_sync.sync_releases', side_effect=releases), \
                 patch('repo_mirror.forgejo_git.subprocess.run', side_effect=run):
                self.assertEqual(self.run_main([URL], [URL]), 0 if changed_side == 'stable' else 1)
                self.assertTrue(state['releases_done'])

    def test_configuration_os_errors_do_not_expose_paths(self):
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(forgejo_sync.main(['--config', '/secret-token/not-found', '--source', URL, '--token-file', 'x', '--owner', 'x']), 2)
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
    def test_source_validation_preserves_name(self):
        self.assertEqual(forgejo_sync.parse_source('https://github.com/manoma080/mirror-Gezine-CivSwitch'), 'manoma080/mirror-Gezine-CivSwitch')
        for invalid in ('https://github.com.evil/manoma080/mirror-x', 'https://github.com/manoma080/mirror-x.git', 'https://github.com/manoma080/mirror-x?x=1', 'https://github.com/other/mirror-x', 'https://github.com/manoma080/foo'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                forgejo_sync.parse_source(invalid)
