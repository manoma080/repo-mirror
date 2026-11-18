import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import build_opener


class Handler(BaseHTTPRequestHandler):
    requests = []
    def log_message(self, *args):
        pass
    def do_GET(self):
        self.requests.append((self.path, dict(self.headers)))
        if self.path == '/api/v4/projects/group%2Fsub%2Fproject':
            data = {'id': 42, 'visibility': 'public', 'path_with_namespace': 'group/sub/project'}
        elif self.path == '/api/v4/projects/42/releases?per_page=100&page=1':
            data = [{'tag_name': 'old', 'released_at': '2024-01-01T00:00:00Z', 'assets': {'links': [], 'sources': [{'format': 'zip'}]}}]
        elif self.path == '/api/v4/projects/42/releases?per_page=100&page=2':
            data = [{'tag_name': 'feature/a', 'released_at': '2025-01-01T00:00:00Z', 'assets': {'links': [{'id': 7, 'name': 'binary', 'url': 'https://example.org/file'}], 'sources': []}}]
        elif self.path == '/api/v4/projects/42/releases/feature%2Fa':
            data = {'tag_name': 'feature/a', 'released_at': '2025-01-01T00:00:00Z', 'assets': {'links': [{'id': 7, 'name': 'binary', 'url': 'https://example.org/file'}], 'sources': []}}
        else:
            self.send_error(404)
            return
        body = json.dumps(data).encode()
        self.send_response(200)
        if self.path.endswith('per_page=100&page=1'):
            self.send_header('X-Next-Page', '2')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class GitLabAdapterTest(unittest.TestCase):
    def test_rejects_external_redirect_before_contacting_target(self):
        from repo_mirror.gitlab_source_adapter import GitLabSourceAdapter
        from urllib.request import BaseHandler
        from urllib.error import HTTPError
        class Redirect(BaseHandler):
            handler_order = 100
            def https_open(self, req):
                raise HTTPError(req.full_url, 302, 'redirect', {'Location': 'https://untrusted.invalid/steal'}, None)
        adapter = GitLabSourceAdapter('https://gitlab.example', 'group/project', 42, handlers=[Redirect()])
        with self.assertRaisesRegex(ValueError, 'cross-origin'):
            adapter.project()

    def test_project_metadata_over_anonymous_http_transport(self):
        from repo_mirror.gitlab_source_adapter import GitLabSourceAdapter
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        # Explicit transport injection allows a local HTTP test without weakening
        # the adapter's HTTPS-only production origin validation.
        from urllib.request import HTTPHandler
        class LocalHTTP(HTTPHandler):
            handler_order = 100
            def https_open(self, req):
                return self.do_open(__import__('http.client').client.HTTPConnection, req)
        adapter = GitLabSourceAdapter(f'https://127.0.0.1:{server.server_port}', 'group/sub/project', 42, handlers=[LocalHTTP()])
        self.assertEqual(adapter.project()['id'], 42)
        self.assertNotIn('Authorization', Handler.requests[-1][1])
        releases = adapter.releases()
        self.assertEqual([r['tag_name'] for r in releases], ['old', 'feature/a'])
        self.assertEqual(adapter.latest()['tag_name'], 'feature/a')
        release = adapter.release('feature/a')
        self.assertEqual(adapter.links(release)[0]['id'], 7)
        self.assertNotIn('id', release)
        self.assertEqual(adapter.snapshot(release)['tag_name'], 'feature/a')

    def test_engine_protocol_normalizes_and_revalidates(self):
        from repo_mirror.gitlab_source_adapter import GitLabSourceAdapter
        adapter = GitLabSourceAdapter('https://gitlab.example', 'group/project', 42)
        item = {'tag_name': 'v/1', 'name': 'Title', 'description': 'Notes',
                'released_at': '2025-01-01T00:00:00Z', 'assets': {'links': []}}
        calls = []
        def get(url):
            calls.append(url)
            if url == adapter.base:
                return {'id': 42, 'visibility': 'public', 'path_with_namespace': 'group/project'}, {}
            return ([item] if '/releases?' in url else item), {}
        with patch.object(adapter, '_get', side_effect=get):
            self.assertEqual(adapter.release_scope, ('https://gitlab.example', 42))
            self.assertEqual(adapter.release_identity_kind, 'tag')
            listed = adapter.list_releases()
            self.assertEqual(listed[0]['body'], 'Notes')
            self.assertFalse(listed[0]['draft'])
            self.assertNotIn('id', listed[0])
            self.assertEqual(adapter.get_release('v/1'), listed[0])
            self.assertEqual(adapter.list_release_assets('v/1'), [])
            self.assertGreaterEqual(calls.count(adapter.base), 3)

    def test_tag_proof_compares_exact_annotated_object(self):
        from repo_mirror.gitlab_source_adapter import GitLabSourceAdapter
        adapter = GitLabSourceAdapter('https://gitlab.example', 'group/project', 42)
        sha = 'a' * 40
        class Destination:
            def request(self, method, url):
                self.requested = (method, url)
                return {'ref': 'refs/tags/v/1', 'object': {'type': 'tag', 'sha': sha}}
        dest = Destination()
        def get(url):
            if url == adapter.base:
                return {'id': 42, 'visibility': 'public', 'path_with_namespace': 'group/project'}, {}
            return {'name': 'v/1', 'message': 'annotation', 'target': sha}, {}
        with patch.object(adapter, '_get', side_effect=get):
            self.assertEqual(adapter.verify_release_tag('v/1', dest, 'https://api.example/repos/x/y'), ('tag', sha))
        self.assertTrue(dest.requested[1].endswith('/git/ref/tags/v%2F1'))

    def test_link_pagination_and_pin_revalidation(self):
        from repo_mirror.gitlab_source_adapter import GitLabSourceAdapter
        adapter = GitLabSourceAdapter('https://gitlab.example', 'group/project', 42)
        project_calls = []
        def get(url):
            if url == adapter.base:
                project_calls.append(url)
                return {'id': 42 if len(project_calls) == 1 else 43, 'visibility': 'public', 'path_with_namespace': 'group/project'}, {}
            return [], {'Link': '<https://gitlab.example/api/v4/projects/42/releases?per_page=100&page=2>; rel="next"'}
        with patch.object(adapter, '_get', side_effect=get):
            with self.assertRaisesRegex(ValueError, 'identity'):
                adapter.list_releases()
        self.assertEqual(len(project_calls), 2)

    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'})
    def test_real_adapter_runs_release_engine(self):
        from repo_mirror.gitlab_source_adapter import GitLabSourceAdapter
        from repo_mirror.repository import mirror_releases
        from test_forgejo_space_body_integration import GitHubAPI, GitHubShapedDestination
        sha = 'a' * 40
        adapter = GitLabSourceAdapter('https://gitlab.example', 'group/project', 42)
        item = {'tag_name': 'v1', 'name': 'Title', 'description': 'Notes',
                'released_at': '2025-01-01T00:00:00Z', 'assets': {'links': []}}
        def get(url):
            if url == adapter.base:
                return {'id': 42, 'visibility': 'public', 'path_with_namespace': 'group/project'}, {}
            if '/repository/tags/' in url:
                return {'name': 'v1', 'message': None, 'target': sha, 'commit': {'id': sha}}, {}
            return ([item] if '/releases?' in url else item), {}
        api = GitHubAPI(existing=False)
        dst = GitHubShapedDestination('team/project', 'token', client=api)
        original = dst.request
        def request(method, url, *args, **kwargs):
            if '/git/ref/tags/' in url:
                return {'ref': 'refs/tags/v1', 'object': {'type': 'commit', 'sha': sha}}
            return original(method, url, *args, **kwargs)
        with patch.object(adapter, '_get', side_effect=get), patch.object(dst, 'request', side_effect=request):
            mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'project', 'team', adapter, dst,
                            'unused', dst.target, 'marker', initializing=True)
        self.assertEqual(api.release['body'], 'Notes')
