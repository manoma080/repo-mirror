import io
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from repo_mirror.forgejo_releases import ForgejoReleases, SyncConflict
from repo_mirror.repository import upload_with_reconciliation


class Response:
    status = 200
    headers = {'Content-Length': '3'}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, size=-1):
        data, self.data = self.data, b''
        return data
    data = b'abc'


class TransferRegressionTests(unittest.TestCase):
    def test_untrusted_asset_paths_are_rejected_without_request(self):
        client = ForgejoReleases('owner/repo', 'secret')
        urls = [
            'https://host/other/repo/releases/download/v1/a.txt',
            'https://host/owner/repo/releases/download/../a.txt',
            'https://host/owner/repo/releases/download/%2e%2e/a.txt',
            'https://host/owner/repo/releases/download/v1/other.txt',
            'https://host/owner/repo/releases/download/v1%2f..%2fadmin/a.txt',
            'https://host/owner/repo/releases/download/v1/a.txt?x=1',
            'https://user@host/owner/repo/releases/download/v1/a.txt',
        ]
        client._json = lambda method, path, value=None: {'id': 7, 'tag_name': 'v1'}
        with patch.object(client.opener, 'open') as opened:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(SyncConflict):
                    asset = {'id': 9, 'name': 'a.txt', 'size': 3, 'browser_download_url': url}
                    client._listed_assets['9'] = ('7', dict(asset))
                    client.download(asset, io.BytesIO())
            opened.assert_not_called()

    def test_slash_tag_asset_is_bound_to_listed_release(self):
        client = ForgejoReleases('owner/repo', 'secret', port=3000)
        asset = {'id': 9, 'name': 'a.txt', 'size': 3,
                 'browser_download_url': 'https://public.example/owner/repo/releases/download/team%2Fv1/a.txt'}
        def get(method, path, value=None):
            if path.endswith('/assets'):
                return [asset]
            return {'id': 7, 'tag_name': 'team/v1'}
        client._json = get
        listed = client.list_assets({'id': 7})[0]
        with patch.object(client.opener, 'open', return_value=Response()) as opened:
            output = io.BytesIO()
            client.download(listed, output)
        self.assertEqual(output.getvalue(), b'abc')
        self.assertEqual(opened.call_args.args[0].full_url,
                         'http://127.0.0.1:3000/owner/repo/releases/download/team%2Fv1/a.txt')

    def test_wrong_slash_tag_and_ambiguous_encodings_are_rejected(self):
        client = ForgejoReleases('owner/repo', 'secret')
        client._json = lambda method, path, value=None: {'id': 7, 'tag_name': 'team/v1'}
        paths = ['other%2Fv1/a.txt', 'team/v1/a.txt', 'team%252Fv1/a.txt',
                 'team%2F..%2Fv1/a.txt', 'team%2Fv1%2F/a.txt',
                 'team%2Fv1/a%2Ftxt', 'team%2Fv1/a.txt%0d']
        with patch.object(client.opener, 'open') as opened:
            for path in paths:
                asset = {'id': 9, 'name': 'a.txt', 'size': 3,
                         'browser_download_url': 'https://host/owner/repo/releases/download/' + path}
                client._listed_assets['9'] = ('7', dict(asset))
                with self.subTest(path=path), self.assertRaises(SyncConflict):
                    client.download(asset, io.BytesIO())
            opened.assert_not_called()

    def test_asset_requires_prior_listing_and_matching_identity(self):
        client = ForgejoReleases('owner/repo', 'secret')
        asset = {'id': 9, 'name': 'a.txt', 'size': 3,
                 'browser_download_url': 'https://host/owner/repo/releases/download/team%2Fv1/a.txt'}
        with patch.object(client.opener, 'open') as opened:
            with self.assertRaises(SyncConflict):
                client.download(asset, io.BytesIO())
            client._listed_assets['9'] = ('7', dict(asset))
            with self.assertRaises(SyncConflict):
                client.download(dict(asset, name='other'), io.BytesIO())
            opened.assert_not_called()

    def test_http_error_is_not_hidden_from_shared_recovery(self):
        client = ForgejoReleases('owner/repo', 'secret')
        error = HTTPError('http://127.0.0.1', 422, 'conflict', {}, None)
        with patch.object(client.opener, 'open', side_effect=error):
            with self.assertRaises(HTTPError) as caught:
                client.upload({'id': 1}, 'a', io.BytesIO(b'abc'), 3)
        self.assertIs(caught.exception, error)

    def test_public_url_is_rebound_to_loopback(self):
        client = ForgejoReleases('owner/repo', 'secret', port=3000)
        class Opener:
            def open(self, request, timeout):
                self.url = request.full_url
                return Response()
        client.opener = Opener()
        output = io.BytesIO()
        asset = {'id': 9, 'name': 'a.txt', 'size': 3,
                 'browser_download_url': 'https://public.example/owner/repo/releases/download/v1/a.txt'}
        client._listed_assets['9'] = ('7', dict(asset))
        client._json = lambda method, path, value=None: {'id': 7, 'tag_name': 'v1'}
        client.download(asset, output)
        self.assertEqual(output.getvalue(), b'abc')
        self.assertEqual(client.opener.url, 'http://127.0.0.1:3000/owner/repo/releases/download/v1/a.txt')

    def test_upload_ambiguous_error_reconciles_existing_asset(self):
        client = ForgejoReleases('owner/repo', 'secret')
        class Opener:
            def open(self, request, timeout):
                raise URLError('connection lost')
        client.opener = Opener()
        class Destination:
            def upload(self, url, data, content_type):
                client.upload({'id': 1}, 'a', data, 3)
            def pages(self, url):
                return [{'name': 'a', 'state': 'uploaded', 'url': 'asset'}]
            def download(self, url, output):
                output.write(b'abc')
        upload_with_reconciliation(Destination(), 'upload', io.BytesIO(b'abc'), 'text/plain', 'assets', 'a')

    def test_multipart_preserves_content_type(self):
        client = ForgejoReleases('owner/repo', 'secret')
        class Opener:
            def open(self, request, timeout):
                self.body = b''.join(request.data)
                response = Response()
                response.data = b'{}'
                return response
        client.opener = Opener()
        client.upload({'id': 1}, 'a', io.BytesIO(b'abc'), 3, 'text/plain')
        self.assertIn(b'Content-Type: text/plain\r\n', client.opener.body)
