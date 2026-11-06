import unittest
import io
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import mirror

class RegressionTests(unittest.TestCase):
    def test_default_branch_is_updated_after_push(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice'}
        class Source:
            def request(self, *args): return {'clone_url': 'https://github.com/alice/demo.git', 'default_branch': 'main'}
            def pages(self, url): return []
        class Destination:
            def __init__(self): self.calls = []
            def request(self, method, url, data=None):
                self.calls.append((method, url, data))
                if method == 'GET': return {'description': mirror.source_marker(source, 'demo'), 'default_branch': 'old'}
                return {}
        dst = Destination()
        with patch.object(mirror, 'mirror_git', side_effect=lambda a,b,c,callback: callback()):
            mirror.mirror_repository(source, 'demo', 'dest', Source(), dst, 'token')
        self.assertTrue(any(method == 'PATCH' and data == {'default_branch': 'main'} for method, url, data in dst.calls))

    def test_empty_source_never_pushes(self):
        from types import SimpleNamespace
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(stdout=b'')
        with patch('subprocess.run', side_effect=run):
            mirror.mirror_git('https://src.example/a.git', 'https://github.com/x/a.git', 'token')
        self.assertFalse(any('push' in args for args in calls))

    def test_push_does_not_prune_old_default_before_github_switch(self):
        from types import SimpleNamespace
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(stdout=b'refs/heads/main\n')
        with patch('subprocess.run', side_effect=run):
            mirror.mirror_git('https://src.example/a.git', 'https://github.com/x/a.git', 'token')
        push = next(args for args in calls if 'push' in args)
        self.assertNotIn('--prune', push)

    def test_destination_asset_download_has_bounded_read(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *args):
                if not args: raise AssertionError('unbounded read')
                if getattr(self, 'done', False): return b''
                self.done = True
                return b'x'
        api = mirror.API('token')
        with patch.object(api.opener, 'open', return_value=Response()):
            output = io.BytesIO()
            api.download('https://api.github.com/asset', output)
            self.assertEqual(output.getvalue(), b'x')

    def test_source_asset_rejects_multicast_address(self):
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('224.0.0.1', 443))]):
            with self.assertRaises(ValueError):
                mirror.API().download('https://multicast.example/asset', io.BytesIO())

    def test_source_asset_rejects_private_address_before_connect(self):
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaises(ValueError):
                mirror.API().download('https://internal.example/asset', io.BytesIO())

    def test_forgejo_pagination_short_server_capped_page(self):
        api = mirror.API()
        calls = []
        def request(method, url, **kw):
            page = int(parse_qs(urlparse(url).query)['page'][0])
            calls.append(page)
            return [{'id': page}] if page <= 2 else []
        with patch.object(api, 'request', side_effect=request):
            self.assertEqual(api.pages('https://forge.example/api/v1/repos/a/b/releases'), [{'id': 1}, {'id': 2}])
        self.assertEqual(calls, [1, 2, 3])
