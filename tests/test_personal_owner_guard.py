import unittest
from urllib.error import HTTPError

from repo_mirror.repository import mirror_repository
from repo_mirror.forgejo_git import APIError, ForgejoDestination


class PersonalOwnerGuardTests(unittest.TestCase):
    def test_github_mismatch_never_posts_even_for_public_repository(self):
        class Destination:
            def __init__(self): self.calls = []
            def request(self, method, url, data=None):
                self.calls.append((method, url))
                if url.endswith('/user'): return {'login': 'wrong'}
                if method == 'GET': raise HTTPError(url, 404, '', {}, None)
                return {'full_name': 'wrong/copy'}
        class Source:
            def request(self, method, url): return {'clone_url': 'https://github.com/Alice/demo.git'}
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'Alice',
                  'destinations': {'demo': {'name': 'copy', 'visibility': 'public'}}}
        dst = Destination()
        with self.assertRaises(RuntimeError):
            mirror_repository(source, 'demo', 'dest', Source(), dst, 'secret', 'personal')
        self.assertIn(('GET', 'https://api.github.com/user'), dst.calls)
        self.assertFalse(any(method == 'POST' for method, _ in dst.calls))

    def test_forgejo_mismatch_never_posts(self):
        class API:
            def __init__(self): self.calls = []
            def request(self, method, path, payload=None):
                self.calls.append((method, path))
                if path == '/api/v1/user': return {'login': 'wrong'}
                if method == 'GET': raise APIError(404)
                return {'id': 1, 'full_name': 'wrong/project'}
        api = API()
        with self.assertRaises(RuntimeError):
            ForgejoDestination(api, 'mirror').ensure_repository('project', 'alice/project')
        self.assertIn(('GET', '/api/v1/user'), api.calls)
        self.assertFalse(any(method == 'POST' for method, _ in api.calls))
