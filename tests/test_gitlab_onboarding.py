import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from repo_mirror import adhoc, cli
from repo_mirror.sources import source_marker

URL = 'https://gitlab.com/group/sub/project'

class GitLabOnboardingTests(unittest.TestCase):
    def test_fresh_process_retries_fail_before_publication_or_creation(self):
        cfg = {'destinations': {'d': {'account_type': 'personal'}}, 'repositories': []}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(cfg))
            env = dict(os.environ, DESTINATION_OWNER='bot')
            env.pop('MIRROR_APP_ID', None)
            env.pop('MIRROR_APP_PRIVATE_KEY', None)
            for _ in range(2):
                result = subprocess.run([sys.executable, '-m', 'repo_mirror.adhoc', '--config', str(path),
                                         '--urls', URL, '--auto-pin'], env=env, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('mirrorbot app credentials required', result.stderr)
                self.assertEqual(json.loads(path.read_text()), cfg)

    def test_named_self_hosted_gitlab_auto_pin_preserves_source_type(self):
        cfg = {'destinations': {'d': {'account_type': 'personal'}}, 'repositories': [
            {'source': {'url': 'https://gitlab.example/group/sub/old', 'type': 'gitlab'}, 'mirrors': {'d': {}}}]}
        adhoc.add_to_config(cfg, 'https://gitlab.example/group/sub/new')
        self.assertEqual(cfg['repositories'][1]['source']['type'], 'gitlab')

    def test_autopin_cli_calls_onboarding(self):
        cfg = {'destinations': {'d': {'account_type': 'personal'}}, 'repositories': []}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(cfg))
            with patch.dict('os.environ', {'DESTINATION_OWNER': 'bot', 'MIRROR_APP_ID': '1',
                                          'MIRROR_APP_PRIVATE_KEY': 'key'}, clear=True), \
                 patch('repo_mirror.adhoc.add_to_config_pinned', return_value=False) as onboard, \
                 patch('repo_mirror.github_api.AppTokenProvider', return_value=object()):
                adhoc.main(['--config', str(path), '--urls', URL, '--auto-pin'])
                onboard.assert_called_once()
            self.assertEqual(json.loads(path.read_text()), cfg)

    def test_autopin_cli_persists_gitlab_project_and_destination_ids(self):
        class Source:
            def request(self, method, url, *args):
                self_url = 'https://gitlab.com/api/v4/projects/group%2Fsub%2Fproject'
                assert method == 'GET' and url == self_url, url
                return {'id': 42, 'visibility': 'public', 'path_with_namespace': 'group/sub/project',
                        'http_url_to_repo': URL + '.git'}
        class Destination:
            repo = None
            def request(self, method, url, payload=None):
                if url.endswith('/actions/permissions'):
                    return {'enabled': False} if method == 'GET' else None
                if url.endswith('/user'):
                    return {'login': 'bot'}
                if method == 'GET':
                    if self.repo is None:
                        raise HTTPError(url, 404, 'missing', {}, None)
                    return self.repo.copy()
                assert method == 'POST'
                self.repo = {'id': 9, 'full_name': 'bot/' + payload['name'], 'description': payload['description']}
                return self.repo.copy()
        cfg = {'destinations': {'d': {'account_type': 'personal'}}, 'repositories': []}
        dst = Destination()
        app = object()
        class FakeAPI:
            def __new__(cls, token=None):
                return dst if token is app else Source()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(cfg))
            with patch.dict('os.environ', {'DESTINATION_OWNER': 'bot', 'MIRROR_APP_ID': '1',
                                          'MIRROR_APP_PRIVATE_KEY': 'key'}, clear=True), \
                 patch('repo_mirror.github_api.AppTokenProvider', return_value=app), \
                 patch('repo_mirror.github_api.SourceAPI', return_value=Source()), \
                 patch('repo_mirror.github_api.API', FakeAPI):
                adhoc.main(['--config', str(path), '--urls', URL, '--auto-pin'])
            entry = json.loads(path.read_text())['repositories'][0]
        self.assertEqual(entry['source']['id'], 42)
        self.assertEqual(entry['mirrors']['d']['id'], 9)
        self.assertEqual(dst.repo['id'], 9)


    def test_explicit_missing_gitlab_pin_fails_without_writes(self):
        calls = []
        def missing(client, method, url, *args, **kwargs):
            calls.append(method)
            raise HTTPError(url, 404, 'missing', {}, None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps({
                'graphql': False,
                'destinations': {'d': {'account_type': 'personal'}},
                'repositories': [{'source': {'type': 'gitlab', 'url': URL}, 'mirrors': {'d': {}}}],
            }))
            with patch.dict('os.environ', {'GH_TOKEN': 'token', 'DESTINATION_OWNER': 'bot',
                                          'MIRROR_TIMESTAMP_SECRET': 'secret'}, clear=True), \
                 patch('repo_mirror.github_api.API.request', missing), \
                 patch('repo_mirror.repository.mirror_git') as git:
                with self.assertRaises(RuntimeError):
                    cli.main(['--config', str(path)])
            git.assert_not_called()
        self.assertTrue(calls)
        self.assertTrue(all(method == 'GET' for method in calls))

    def test_autopin_rejects_private_and_changed_historical_id_before_writes(self):
        class Source:
            def __init__(self, visibility):
                self.visibility = visibility
            def request(self, method, url, *args):
                return {'id': 43, 'visibility': self.visibility, 'path_with_namespace': 'group/sub/project',
                        'http_url_to_repo': URL + '.git'}
        class Destination:
            calls = []
            def request(self, method, url, *args):
                self.calls.append(method)
                raise AssertionError('destination should not be queried')
        for visibility in ('private', 'public'):
            with self.subTest(visibility=visibility):
                cfg = {'destinations': {'d': {'account_type': 'personal'}},
                       'repositories': [{'source': {'url': URL, 'id': 42}, 'mirrors': {'d': {'id': 9}}}]}
                dst = Destination()
                with self.assertRaises(ValueError):
                    adhoc.add_to_config_pinned(cfg, URL, Source(visibility), Source(visibility), dst,
                                               owner='bot', account_type='personal')
                self.assertEqual(dst.calls, [])


class GitLabDiscoveryIdentityTests(unittest.TestCase):
    def test_discovery_excludes_private_without_exposing_name(self):
        from repo_mirror.gitlab_onboarding_forgejo import discover_gitlab_projects
        source = {'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'group/sub'}
        class Source:
            def pages(self, url):
                return [{'visibility': 'private', 'path': 'secret'},
                        {'id': 42, 'path': 'project', 'path_with_namespace': 'group/sub/project',
                         'visibility': 'public', 'http_url_to_repo': URL + '.git'}]
        self.assertEqual(discover_gitlab_projects(source, Source()), {'project': 42})

    def test_discovery_retains_validated_direct_project_identity(self):
        from repo_mirror.gitlab_onboarding_forgejo import discover_gitlab_projects
        source = {"type": "gitlab", "base_url": "https://gitlab.com", "owner": "group/sub"}
        class Source:
            def pages(self, url):
                self.url = url
                return [{"id": 42, "path": "project", "path_with_namespace": "group/sub/project",
                         "visibility": "public", "http_url_to_repo": URL + ".git"}]
        api = Source()
        self.assertEqual(discover_gitlab_projects(source, api), {"project": 42})
        self.assertIn("include_subgroups=false", api.url)
        self.assertIn("with_shared=false", api.url)

    def test_entrypoint_one_time_gitlab_refuses_without_durable_onboarding(self):
        with tempfile.TemporaryDirectory() as directory:
            class Destination:
                repo = None
                def request(self, method, url, payload=None):
                    raise AssertionError('destination should not be queried')
            dst = Destination()
            path = Path(directory) / 'config.json'
            original = {"destinations": {"d": {"account_type": "personal"}}, "repositories": []}
            path.write_text(json.dumps(original))
            env = {"GH_TOKEN": "token", "DESTINATION_OWNER": "bot", "MIRROR_TIMESTAMP_SECRET": "secret",
                   "MIRROR_SOURCE_URLS": URL}
            with patch.dict('os.environ', env, clear=True), \
                 patch('repo_mirror.cli.mirror_repository') as mirror:
                with self.assertRaisesRegex(ValueError, 'durable GitLab onboarding'):
                    cli.main(['--config', str(path)])
            mirror.assert_not_called()
            self.assertIsNone(dst.repo)
            self.assertEqual(json.loads(path.read_text()), original)

    def test_discovery_rejects_non_direct_or_private_projects(self):
        from repo_mirror.gitlab_onboarding_forgejo import discover_gitlab_projects
        source = {"type": "gitlab", "base_url": "https://gitlab.com", "owner": "group/sub"}
        valid = {"id": 42, "path": "project", "visibility": "public",
                 "path_with_namespace": "group/sub/project", "http_url_to_repo": URL + ".git"}
        for override in ({"path_with_namespace": "group/sub/child/project"}, {"id": True}):
            with self.subTest(override=override):
                class Source:
                    def pages(self, url): return [{**valid, **override}]
                with self.assertRaises(ValueError): discover_gitlab_projects(source, Source())
