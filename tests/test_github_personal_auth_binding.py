import io
import json
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from repo_mirror.github_api import API, AppTokenProvider
from repo_mirror.repository import mirror_repository


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class PersonalAuthBindingTests(unittest.TestCase):
    def run_create(self, token, expected='dest', identity='dest'):
        calls = []
        class Opener:
            def open(self, req, timeout):
                path = req.full_url
                calls.append((req.method, path, req.get_header('Authorization')))
                if path.endswith('/user'):
                    if identity is None:
                        raise HTTPError(path, 403, '', {}, None)
                    return Response(json.dumps({'login': identity}).encode())
                if req.method == 'GET': raise HTTPError(path, 404, '', {}, None)
                if path.endswith('/user/repos'):
                    return Response(json.dumps({'full_name': expected + '/copy'}).encode())
                raise RuntimeError('stop after creation')
        class Source:
            def request(self, method, url): return {'clone_url': 'https://github.com/Alice/demo.git'}
        api = API(token)
        api.opener = Opener()
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'Alice',
                  'destinations': {'demo': {'name': 'copy'}}}
        try:
            mirror_repository(source, 'demo', expected, Source(), api, token, 'personal')
        except Exception as exc:
            return calls, exc
        self.fail('expected stop after creation')

    def test_app_matching_owner_creates_without_user_endpoint(self):
        provider = AppTokenProvider('1', 'key', 'dest')
        with patch.object(provider, '_mint', return_value=('app-token', time.time() + 3600)):
            calls, exc = self.run_create(provider, identity=None)
        self.assertEqual(str(exc), 'stop after creation')
        self.assertFalse(any(url.endswith('/user') for _, url, _ in calls))
        self.assertIn(('POST', 'https://api.github.com/user/repos', 'Bearer app-token'), calls)

    def test_app_mismatching_owner_does_not_post(self):
        provider = AppTokenProvider('1', 'key', 'other')
        with patch.object(provider, '_mint', return_value=('app-token', time.time() + 3600)):
            calls, exc = self.run_create(provider)
        self.assertIn('owner does not match', str(exc))
        self.assertFalse(any(method == 'POST' for method, _, _ in calls))

    def test_user_token_matching_and_mismatching(self):
        for identity, allowed in [('DEST', True), ('other', False), (None, False)]:
            with self.subTest(identity=identity):
                calls, exc = self.run_create('user-token', identity=identity)
                self.assertIn(('GET', 'https://api.github.com/user', 'Bearer user-token'), calls)
                posts = [call for call in calls if call[0] == 'POST']
                self.assertEqual(bool(posts), allowed)
                if allowed: self.assertEqual(posts[0][2], 'Bearer user-token')

    def test_refresh_between_identity_and_post_cannot_change_credential(self):
        tokens = iter(['first', 'checked', 'wrong'])
        calls, exc = self.run_create(lambda: next(tokens))
        self.assertEqual(str(exc), 'stop after creation')
        self.assertIn(('GET', 'https://api.github.com/user', 'Bearer checked'), calls)
        self.assertIn(('POST', 'https://api.github.com/user/repos', 'Bearer checked'), calls)

    def test_cached_app_token_remains_bound_to_original_owner(self):
        provider = AppTokenProvider('1', 'key', 'other')
        with patch.object(provider, '_mint', return_value=('old-owner-token', time.time() + 3600)):
            provider()
            provider.owner = 'dest'
            calls, exc = self.run_create(provider)
        self.assertFalse(any(method == 'POST' for method, _, _ in calls))

    def test_app_refresh_after_initial_lookup_uses_refreshed_token(self):
        provider = AppTokenProvider('1', 'key', 'dest')
        def mint(): return ('refreshed', 5000)
        provider.token, provider.expires = 'old', 1301
        with patch.object(provider, '_mint', side_effect=mint), \
             patch('repo_mirror.github_api.time.time', side_effect=[1000, 1002, 1002, 1002]):
            calls, exc = self.run_create(provider, identity=None)
        self.assertEqual(str(exc), 'stop after creation')
        self.assertIn(('POST', 'https://api.github.com/user/repos', 'Bearer refreshed'), calls)
