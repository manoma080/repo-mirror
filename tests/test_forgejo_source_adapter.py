import io
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from repo_mirror.forgejo_source_adapter import ForgejoSourceAdapter, _AssetRedirect


class SourceAdapterTests(unittest.TestCase):
    @patch('time.sleep')
    def test_retry_classification_limits_and_retry_after(self, sleep):
        url = 'https://github.com/acme/project/releases/download/v1/SHA256SUMS'
        asset = {'id': 42, 'size': 3, 'name': 'SHA256SUMS', 'browser_download_url': url}
        for error, expected, delay in (
            (URLError('offline'), 4, None),
            (TimeoutError(), 4, None),
            (HTTPError(url, 429, '', {'Retry-After': '10'}, None), 4, 10),
            (HTTPError(url, 503, '', {}, None), 4, None),
            (HTTPError(url, 404, '', {}, None), 1, None),
            (HTTPError(url, 429, '', {'Retry-After': '61'}, None), 1, None),
            (ValueError('unsafe asset redirect'), 1, None),
        ):
            with self.subTest(error=error):
                sleep.reset_mock()
                api, client = Mock(), Mock()
                api.pages.return_value = [asset]
                adapter = ForgejoSourceAdapter('acme/project', 'secret', api=api, client=client)
                adapter.pages(adapter.base + '/releases/1/assets')
                def fail(asset, output, **kwargs):
                    output.write(b'partial')
                    raise error
                client._download_once.side_effect = fail
                output = io.BytesIO(b'prefix')
                output.seek(6)
                with self.assertRaises(type(error)):
                    adapter.download(url, output)
                self.assertEqual(client._download_once.call_count, expected)
                self.assertEqual(sleep.call_count, expected - 1)
                self.assertEqual(output.getvalue(), b'prefix')
                if delay:
                    self.assertTrue(all(call.args[0] == delay for call in sleep.call_args_list))

    @patch('time.sleep')
    def test_short_checksum_response_retries(self, sleep):
        api = Mock()
        asset = {'id': 42, 'size': 3, 'name': 'SHA256SUMS', 'browser_download_url': 'https://github.com/acme/project/releases/download/v1/SHA256SUMS'}
        api.pages.return_value = [asset]
        adapter = ForgejoSourceAdapter('acme/project', 'secret', api=api)
        adapter.pages(adapter.base + '/releases/1/assets')
        class Response(io.BytesIO):
            status = 200
            headers = {'Content-Length': '3'}
        adapter.client.api.opener.open = Mock(side_effect=[Response(b'a'), Response(b'abc')])
        output = io.BytesIO()
        adapter.download(asset['browser_download_url'], output)
        self.assertEqual(output.getvalue(), b'abc')
        self.assertEqual(adapter.client.api.opener.open.call_count, 2)
        sleep.assert_called_once()

    @patch('time.sleep')
    def test_transient_error_discards_partial_data_and_refreshes_token(self, sleep):
        api = Mock()
        asset = {'id': 42, 'size': 3, 'name': 'SHA256SUMS', 'browser_download_url': 'https://github.com/acme/project/releases/download/v1/SHA256SUMS'}
        api.pages.return_value = [asset]
        tokens = iter(['first', 'second'])
        adapter = ForgejoSourceAdapter('acme/project', lambda: next(tokens), api=api)
        adapter.pages(adapter.base + '/releases/1/assets')
        requests = []

        class Broken(io.BytesIO):
            status = 200
            headers = {'Content-Length': '3'}
            def read(self, size=-1):
                chunk = super().read(size)
                if not chunk:
                    raise URLError('offline')
                return chunk

        class Response(io.BytesIO):
            status = 200
            headers = {'Content-Length': '3'}

        def open_request(request, timeout):
            requests.append(request)
            return Broken(b'x') if len(requests) == 1 else Response(b'abc')

        adapter.client.api.opener.open = open_request
        output = io.BytesIO(b'prefix-old')
        output.seek(6)
        adapter.download(asset['browser_download_url'], output)
        self.assertEqual(output.getvalue(), b'prefixabc')
        self.assertEqual([r.get_header('Authorization') for r in requests], ['Bearer first', 'Bearer second'])
        sleep.assert_called_once()

    def test_listed_asset_download_uses_github_api_identity(self):
        api = Mock()
        asset = {'id': 42, 'size': 3, 'name': 'SHA256SUMS', 'browser_download_url': 'https://github.com/acme/project/releases/download/v1/SHA256SUMS'}
        api.pages.return_value = [asset]
        client = Mock()
        adapter = ForgejoSourceAdapter('acme/project', lambda: 'token', api=api, client=client)
        self.assertEqual(adapter.pages('https://api.github.com/repos/acme/project/releases/1/assets'), [asset])
        output = io.BytesIO()
        adapter.download(asset['browser_download_url'], output)
        client._download_once.assert_called_once_with(asset, output, retry_truncated=True)
        with self.assertRaises(ValueError):
            adapter.download('https://evil.example/asset', output)

    def test_real_client_download_refreshes_token_and_uses_api_url(self):
        tokens = iter(['first', 'second'])
        api = Mock()
        asset = {'id': 42, 'size': 3, 'name': 'SHA256SUMS', 'browser_download_url': 'https://github.com/acme/project/releases/download/v1/SHA256SUMS'}
        api.pages.return_value = [asset]
        adapter = ForgejoSourceAdapter('acme/project', lambda: next(tokens), api=api)
        requests = []

        class Response(io.BytesIO):
            status = 200
            headers = {'Content-Length': '3'}

        def open_request(request, timeout):
            requests.append(request)
            return Response(b'abc')

        adapter.client.api.opener.open = open_request
        adapter.pages(adapter.base + '/releases/1/assets')
        for _ in range(2):
            output = io.BytesIO()
            adapter.download(asset['browser_download_url'], output)
            self.assertEqual(output.getvalue(), b'abc')
        self.assertEqual([r.full_url for r in requests], [adapter.base + '/releases/assets/42'] * 2)
        self.assertEqual([r.get_header('Authorization') for r in requests], ['Bearer first', 'Bearer second'])
        self.assertEqual(requests[0].get_header('Accept'), 'application/octet-stream')

    def test_redirect_only_to_storage_and_strips_authorization(self):
        handler = _AssetRedirect()
        request = Request('https://api.github.com/repos/acme/project/releases/assets/42', headers={'Authorization': 'Bearer secret'})
        redirect = handler.redirect_request(request, None, 302, 'Found', {}, 'https://release-assets.githubusercontent.com/file?signature=x')
        self.assertIsNone(redirect.get_header('Authorization'))
        for url in ('https://evil.example/file', 'http://release-assets.githubusercontent.com/file', 'https://127.0.0.1/file', 'https://api.github.com/other'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                handler.redirect_request(request, None, 302, 'Found', {}, url)

    def test_rejects_invalid_identity_and_ambiguous_browser_mapping(self):
        api = Mock()
        adapter = ForgejoSourceAdapter('acme/project', 'token', api=api, client=Mock())
        for asset in (
            {'id': True, 'browser_download_url': 'https://github.com/acme/project/releases/download/v1/a'},
            {'id': 1, 'browser_download_url': 'https://github.com/other/project/releases/download/v1/a'},
        ):
            api.pages.return_value = [asset]
            with self.subTest(asset=asset), self.assertRaises((ValueError, RuntimeError)):
                adapter.pages(adapter.base + '/releases/1/assets')
        url = 'https://github.com/acme/project/releases/download/v1/a'
        api.pages.return_value = [{'id': 1, 'browser_download_url': url}, {'id': 2, 'browser_download_url': url}]
        with self.assertRaises(ValueError):
            adapter.pages(adapter.base + '/releases/1/assets')
