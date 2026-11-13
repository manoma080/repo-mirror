import io
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from repo_mirror.repository import mirror_releases
from test_forgejo_destination_adapter import make_adapter


class EmptyBodyMirrorTests(unittest.TestCase):
    def test_interrupted_empty_update_resumes_and_spoof_is_rejected(self):
        for spoof in (False, True):
            with self.subTest(spoof=spoof):
                adapter, client = make_adapter()
                client.release = {'id': 7, 'tag_name': 'v1', 'name': 'name', 'body': 'old',
                                  'draft': False, 'prerelease': False, 'target_commitish': 'main'}
                client.releases = [client.release]
                release = dict(client.release, id=1, body='')
                class Source:
                    def pages(self, url):
                        return [] if url.endswith('/assets') else [release]
                    def request(self, method, url):
                        return release
                original = client._json
                fail = [True]
                def request(method, path, body=None):
                    if method == 'PATCH' and body and body.get('body') == ' ' and fail[0]:
                        fail[0] = False
                        raise RuntimeError('interrupted')
                    return original(method, path, body)
                client._json = request
                def run():
                    return mirror_releases({'type': 'github', 'owner': 'alice'}, 'demo', 'team',
                                           Source(), adapter, 'https://source/repo', adapter.target,
                                           'marker', initializing=True)
                with patch.dict(os.environ, {'MIRROR_TIMESTAMP_SECRET': 'secret'}), \
                     patch('repo_mirror.repository.fetch_source_latest', return_value=None):
                    with self.assertRaises(Exception):
                        run()
                    self.assertIn('[repo-mirror:pending:v3]', client.release['body'])
                    if spoof:
                        client.release['body'] = client.release['body'].replace('"signature":"', '"signature":"0')
                        writes = sum(c[0] in ('PATCH', 'POST') for c in client.calls)
                        with self.assertRaises(Exception):
                            run()
                        self.assertEqual(writes, sum(c[0] in ('PATCH', 'POST') for c in client.calls))
                    else:
                        run()
                        self.assertEqual(client.release['body'], ' ')

    def test_new_and_existing_empty_bodies_finish_with_space(self):
        for body in (None, '', ' ', '  ', '\t', '\n'):
            for exists in (False, True):
                with self.subTest(body=body, exists=exists):
                    adapter, client = make_adapter()
                    client.release = {'id': 7, 'tag_name': 'v1', 'name': 'name', 'body': 'old',
                                      'draft': False, 'prerelease': False, 'target_commitish': 'main'}
                    client.releases = [client.release] if exists else []
                    release = {'id': 1, 'tag_name': 'v1', 'name': 'name', 'body': body,
                               'draft': False, 'prerelease': False, 'target_commitish': 'main'}
                    class Source:
                        def pages(self, url):
                            return [] if url.endswith('/assets') else [release]
                        def request(self, method, url):
                            return release
                    original = client._json
                    def request(method, path, payload=None):
                        if method == 'GET' and '/tags/' in path and not exists:
                            raise HTTPError(path, 404, 'missing', {}, io.BytesIO())
                        return original(method, path, payload)
                    client._json = request
                    with patch.dict(os.environ, {'MIRROR_TIMESTAMP_SECRET': 'secret'}), \
                         patch('repo_mirror.repository.fetch_source_latest', return_value=None):
                        mirror_releases({'type': 'github', 'owner': 'alice'}, 'demo', 'team',
                                        Source(), adapter, 'https://source/repo', adapter.target,
                                        'marker', initializing=True)
                    self.assertEqual(client.release['body'], ' ' if body in (None, '', ' ') else body)
                    self.assertTrue(any(c[0] in ('POST', 'PATCH') and '[repo-mirror:pending:v3]' in str(c[2])
                                        for c in client.calls))
