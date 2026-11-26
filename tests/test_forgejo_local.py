import unittest
from unittest.mock import patch

from repo_mirror import forgejo_local


class ForgejoTests(unittest.TestCase):
    def test_local_api_reads_token_from_file_and_uses_loopback(self):
        with patch('pathlib.Path.read_text', return_value='secret\n'), patch('http.client.HTTPConnection') as factory:
            response = factory.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"login":"forgejo-owner"}'
            self.assertEqual(forgejo_local.local_api('api/v1/user', '/tmp/token'), {'login': 'forgejo-owner'})
            factory.assert_called_once_with('127.0.0.1', 3300, timeout=30)
            factory.return_value.request.assert_called_once_with('GET', '/api/v1/user', headers={'Authorization': 'token secret'})

    def test_rejects_absolute_or_traversing_paths(self):
        for path in ('http://evil.example/api', '//evil.example/api', '../api'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                forgejo_local.local_api(path, '/tmp/token')


class ReleaseTests(unittest.TestCase):
    def test_release_assets_are_never_excluded_by_name(self):
        assets = [{'name': 'repo-mirror-manifest.json', 'size': 4, 'state': 'uploaded'},
                  {'name': 'TIMESTAMPS.txt', 'size': 3, 'state': 'uploaded'}]
        self.assertEqual(forgejo_local.source_assets(assets), assets)

    def test_unavailable_and_duplicate_assets_fail_closed(self):
        for assets in ([{'name': 'x', 'state': 'starter'}],
                       [{'name': 'x', 'state': 'uploaded'}, {'name': 'x', 'state': 'uploaded'}]):
            with self.subTest(assets=assets), self.assertRaises(ValueError):
                forgejo_local.source_assets(assets)


class IdentityTests(unittest.TestCase):
    def test_rejects_source_identity_with_path_or_url_injection(self):
        for value in ('owner/repo/extra', '../repo', 'owner/repo.git', 'owner/repo?x=1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                forgejo_local.source_identity(value)
        self.assertEqual(forgejo_local.source_identity('owner/mirror-Example'), ('owner', 'mirror-Example'))
