import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repo_mirror import cli
from repo_mirror.sources import source_marker


class GitLabGitHubCliTests(unittest.TestCase):
    def test_nested_project_mirrors_with_pinned_anonymous_adapter(self):
        source = {'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'group/sub'}
        destination = {'id': 9, 'description': source_marker(source, 'project'), 'default_branch': 'main'}
        config_doc = {
            'destinations': {'main': {'account_type': 'personal'}},
            'repositories': [{'source': {'type': 'gitlab', 'url': 'https://gitlab.com/group/sub/project', 'id': 42},
                              'mirrors': {'main': {'id': 9}}}],
        }
        source_urls = []
        def source_get(client, url):
            source_urls.append(url)
            if '/releases?' in url:
                return [], {}
            return ({'id': 42, 'visibility': 'public', 'path_with_namespace': 'group/sub/project',
                     'http_url_to_repo': 'https://gitlab.com/group/sub/project.git', 'default_branch': 'main'}, {})
        def destination_request(client, method, url, data=None, **kwargs):
            return destination
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps(config_doc))
            with patch.dict('os.environ', {'GH_TOKEN': 'destination-secret', 'DESTINATION_OWNER': 'dest',
                                           'MIRROR_TIMESTAMP_SECRET': 'fixture-secret'}, clear=True), \
                 patch('repo_mirror.github_api.API.request', destination_request), \
                 patch('repo_mirror.gitlab_source_adapter.GitLabSourceAdapter._get', source_get), \
                 patch('repo_mirror.repository.mirror_git') as git:
                cli.main(['--config', str(config)])
        self.assertEqual(git.call_args.args[0], 'https://gitlab.com/group/sub/project.git')
        self.assertTrue(any('/api/v4/projects/42/releases?' in url for url in source_urls), source_urls)
        self.assertTrue(all(url.startswith('https://gitlab.com/api/v4/projects/') for url in source_urls))

    def test_group_discovery_is_anonymous_and_origin_bound(self):
        from io import BytesIO
        from urllib.request import OpenerDirector

        doc = {
            'destinations': {'main': {'account_type': 'personal'}},
            'repositories': [],
            'auto_discoveries': [{'type': 'gitlab', 'url': 'https://gitlab.com/group', 'id': 7}],
        }
        raw = json.dumps(doc).encode()

        class Store:
            def read(self):
                return raw, 'a' * 40

        requests = []

        class Response(BytesIO):
            headers = {}

        def open_request(opener, request, timeout=None):
            requests.append(request)
            if request.full_url.endswith('/groups/group'):
                return Response(json.dumps({'id': 7, 'visibility': 'public', 'full_path': 'group'}).encode())
            return Response(b'[]')

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_bytes(raw)
            with patch.dict('os.environ', {'GH_TOKEN': 'destination-secret', 'DESTINATION_OWNER': 'dest',
                                           'MIRROR_TIMESTAMP_SECRET': 'fixture-secret'}, clear=True), \
                 patch('repo_mirror.github_migration.store_from_environment', return_value=Store()), \
                 patch('repo_mirror.github_migration.transaction_from_environment', return_value=None), \
                 patch.object(OpenerDirector, 'open', open_request):
                cli.main(['--config', str(config)])
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(r.full_url.startswith('https://gitlab.com/api/v4/groups/') for r in requests))
        self.assertTrue(all(r.get_header('Authorization') is None for r in requests))

    def test_group_discovery_rejects_cross_origin_redirect(self):
        from email.message import Message
        from io import BytesIO
        from urllib.request import HTTPSHandler
        from urllib.response import addinfourl

        doc = {
            'destinations': {'main': {'account_type': 'personal'}},
            'repositories': [],
            'auto_discoveries': [{'type': 'gitlab', 'url': 'https://gitlab.com/group', 'id': 7}],
        }
        raw = json.dumps(doc).encode()

        class Store:
            def read(self):
                return raw, 'a' * 40

        urls = []

        def open_request(handler, request):
            urls.append(request.full_url)
            headers = Message()
            headers['Location'] = 'https://evil.example/projects'
            response = addinfourl(BytesIO(b''), headers, request.full_url, 302)
            response.msg = 'Found'
            self.addCleanup(response.close)
            return response

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_bytes(raw)
            with patch.dict('os.environ', {'GH_TOKEN': 'destination-secret', 'DESTINATION_OWNER': 'dest',
                                           'MIRROR_TIMESTAMP_SECRET': 'fixture-secret'}, clear=True), \
                 patch('repo_mirror.github_migration.store_from_environment', return_value=Store()), \
                 patch('repo_mirror.github_migration.transaction_from_environment', return_value=None), \
                 patch.object(HTTPSHandler, 'https_open', open_request):
                with self.assertRaises(ValueError):
                    cli.main(['--config', str(config)])
        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].startswith('https://gitlab.com/'))

    def test_destination_pin_mismatch_stops_before_git_or_writes(self):
        source = {'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'group'}
        config_doc = {
            'destinations': {'main': {'account_type': 'personal'}},
            'repositories': [{'source': {'type': 'gitlab', 'url': 'https://gitlab.com/group/project', 'id': 42},
                              'mirrors': {'main': {'id': 9}}}],
        }
        requests = []

        def request(client, method, url, data=None, **kwargs):
            requests.append((method, url))
            return {'id': 10, 'description': source_marker(source, 'project')}

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps(config_doc))
            with patch.dict('os.environ', {'GH_TOKEN': 'destination-secret', 'DESTINATION_OWNER': 'dest',
                                           'MIRROR_TIMESTAMP_SECRET': 'fixture-secret'}, clear=True), \
                 patch('repo_mirror.github_api.API.request', request), \
                 patch('repo_mirror.repository.mirror_git') as git:
                with self.assertRaises(RuntimeError):
                    cli.main(['--config', str(config)])
        self.assertGreaterEqual(len(requests), 1)
        self.assertTrue(all(method == 'GET' and url.startswith('https://api.github.com/')
                            for method, url in requests), requests)
        git.assert_not_called()

    def test_source_pin_mismatch_stops_before_destination_writes(self):
        source = {'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'group'}
        config_doc = {
            'destinations': {'main': {'account_type': 'personal'}},
            'repositories': [{'source': {'type': 'gitlab', 'url': 'https://gitlab.com/group/project', 'id': 42},
                              'mirrors': {'main': {'id': 9}}}],
        }
        destination = {'id': 9, 'description': source_marker(source, 'project'), 'default_branch': 'main'}
        requests = []

        def destination_request(client, method, url, data=None, **kwargs):
            requests.append((method, url))
            return destination

        def source_get(client, url):
            requests.append(('SOURCE', url))
            return ({'id': 43, 'visibility': 'public', 'path_with_namespace': 'group/project',
                     'http_url_to_repo': 'https://gitlab.com/group/project.git', 'default_branch': 'main'}, {})

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps(config_doc))
            with patch.dict('os.environ', {'GH_TOKEN': 'destination-secret', 'DESTINATION_OWNER': 'dest',
                                           'MIRROR_TIMESTAMP_SECRET': 'fixture-secret'}, clear=True), \
                 patch('repo_mirror.github_api.API.request', destination_request), \
                 patch('repo_mirror.gitlab_source_adapter.GitLabSourceAdapter._get', source_get), \
                 patch('repo_mirror.repository.mirror_git') as git:
                with self.assertRaises(RuntimeError):
                    cli.main(['--config', str(config)])
            self.assertTrue(any(method == 'SOURCE' for method, _ in requests), requests)
            self.assertFalse(any(method not in ('GET', 'SOURCE') for method, _ in requests), requests)
            git.assert_not_called()
