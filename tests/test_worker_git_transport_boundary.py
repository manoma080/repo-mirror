"""Exercise worker-selected identities through the real Git transport helpers."""
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from repo_mirror.forgejo_sync import main


class WorkerGitTransportBoundaryTests(unittest.TestCase):
    def run_case(self, configured, canonical, github=False):
        source_url = canonical + '.git'
        destination_url = 'http://127.0.0.1:3300/dest/copy.git'
        github_token, local_token = 'github-secret', 'local-secret'
        original_marker = configured.removeprefix('https://')
        repository = {'id': 22, 'full_name': 'dest/copy', 'private': True,
                      'description': 'repo-mirror-source: ' + original_marker + ' [repo-mirror:initialized:v1]',
                      'has_actions': False, 'default_branch': 'main'}
        calls = []

        def source_request(client, method, url, body=None, headers=None, **kwargs):
            self.assertEqual(method, 'GET')
            if '/releases/latest' in url:
                raise HTTPError(url, 404, 'not found', Message(), None)
            if '/releases?' in url:
                return b'[]'
            self.assertNotIn('?', url)
            owner, name = canonical.rsplit('/', 2)[-2:]
            return json.dumps({'id': 11, 'full_name': owner + '/' + name,
                               'html_url': canonical, 'clone_url': source_url}).encode()

        def local_request(api, method, path, payload=None, **kwargs):
            self.assertEqual((method, path), ('GET', '/api/v1/repos/dest/copy'))
            self.assertEqual(api.token(), local_token)
            return dict(repository)

        def git(args, **kwargs):
            calls.append((list(args), dict(kwargs['env'])))
            return subprocess.CompletedProcess(args, 0, b'', b'')

        config = {'destinations': {'local': {}}, 'repositories': [
            {'source': {'url': configured, 'id': 11}, 'mirrors': {'local': {'name': 'copy', 'id': 22}}}]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps(config))
            token = root / 'token'
            token.write_text(local_token)
            token.chmod(0o600)
            secret = root / 'secret'
            secret.write_text('fixture-signing-secret-0123456789abcdef')
            secret.chmod(0o600)
            journal = root / 'journal'
            journal.mkdir(mode=0o700)
            argv = ['--config', str(manifest), '--source', configured, '--token-file', str(token),
                    '--timestamp-secret-file', str(secret), '--journal-dir', str(journal), '--owner', 'dest']
            if github:
                argv.append('--use-github-token')
            stderr = io.StringIO()
            with patch.dict('os.environ', {'GITHUB_TOKEN': github_token}), \
                 patch('repo_mirror.github_api.API._request', new=source_request), \
                 patch('repo_mirror.forgejo_git.LocalForgejoAPI.request', new=local_request), \
                 patch('repo_mirror.forgejo_git.subprocess.run', side_effect=git), \
                 redirect_stderr(stderr), redirect_stdout(io.StringIO()):
                result = main(argv)
            self.assertEqual(result, 0, stderr.getvalue())
            self.assertEqual(json.loads(manifest.read_text()), config)

        source_calls = [(args, env) for args, env in calls if source_url in args]
        destination_calls = [(args, env) for args, env in calls if destination_url in args]
        self.assertGreaterEqual(len(source_calls), 6)  # sync plus both snapshots
        self.assertGreaterEqual(len(destination_calls), 3)
        for args, env in source_calls:
            self.assertEqual(env['MIRROR_TOKEN'], github_token if github else '')
            self.assertEqual(env['MIRROR_USER'], 'x-access-token')
            self.assertNotIn('GITHUB_TOKEN', env)
            self.assertNotIn(local_token, repr(args) + repr(env))
            self.assertIn('http.followRedirects=false', args)
        for args, env in destination_calls:
            self.assertEqual(env['MIRROR_TOKEN'], local_token)
            self.assertEqual(env['MIRROR_USER'], 'oauth2')
            self.assertNotIn('GITHUB_TOKEN', env)
            self.assertNotIn(github_token, repr(args) + repr(env))
        git_urls = {arg for args, _ in calls for arg in args if arg.startswith(('https://', 'http://'))}
        self.assertEqual(git_urls, {source_url, destination_url})
        if configured != canonical:
            prefix = 'FORGEJO_RELOCATION: '
            reports = [json.loads(line[len(prefix):]) for line in stderr.getvalue().splitlines()
                       if line.startswith(prefix)]
            self.assertEqual(reports, [{'url': configured, 'canonical': canonical,
                                        'source_id': 11, 'destination_id': 22}])

    def test_public_forgejo_origin_with_nondefault_port(self):
        self.run_case('https://git.example:8443/old/repo', 'https://git.example:8443/old/repo')

    def test_relocated_public_forgejo(self):
        self.run_case('https://git.example/old/repo', 'https://git.example/new/renamed')

    def test_relocated_public_forgejo_keeps_origin_and_port(self):
        self.run_case('https://git.example:8443/old/repo', 'https://git.example:8443/new/renamed')

    def test_github_source_still_uses_github_token(self):
        self.run_case('https://github.com/owner/repo', 'https://github.com/owner/repo', github=True)
