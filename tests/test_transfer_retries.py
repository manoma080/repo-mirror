import http.client
import io
import socket
import ssl
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from repo_mirror.github_api import API


class TransferRetryTests(unittest.TestCase):
    def test_tls_eof_download_retries(self):
        api = API('token')
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = [b'complete', b'']
        with (patch.object(api.opener, 'open', side_effect=[ssl.SSLEOFError('unexpected EOF'), response]) as request,
              patch('time.sleep') as sleep):
            output = io.BytesIO()
            api.download('https://api.github.com/asset', output)
        self.assertEqual(output.getvalue(), b'complete')
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once()

    def test_source_temporary_dns_failure_retries_and_revalidates(self):
        api = API()
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
        with (patch('socket.getaddrinfo', side_effect=[socket.gaierror(socket.EAI_AGAIN, 'temporary'), private]) as dns,
              patch('time.sleep') as sleep, self.assertRaisesRegex(ValueError, 'not public')):
            api.download('https://example.com/asset', io.BytesIO())
        self.assertEqual(dns.call_count, 2)
        sleep.assert_called_once()

    def test_truncated_content_length_retries_from_start(self):
        def response(body):
            sock = Mock()
            sock.makefile.return_value = io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: 8\r\n\r\n' + body)
            result = http.client.HTTPResponse(sock)
            result.begin()
            return result

        api = API('token')
        output = io.BytesIO(b'prefix')
        output.seek(6)
        with patch.object(api.opener, 'open', side_effect=[response(b'short'), response(b'complete')]) as request, patch('time.sleep'):
            api.download('https://api.github.com/asset', output)
        self.assertEqual(output.getvalue(), b'prefixcomplete')
        self.assertEqual(request.call_count, 2)

    def test_long_retry_after_fails_without_sleeping_or_replaying(self):
        from repo_mirror.repository import upload_with_reconciliation

        for value in ('61', '1e999', 'Wed, 21 Oct 2099 07:28:00 GMT'):
            with self.subTest(value=value):
                error = HTTPError('https://api.github.com/asset', 503, '', {'Retry-After': value}, None)
                api = API('token')
                with (patch.object(api.opener, 'open', side_effect=error) as request,
                      patch('time.sleep') as sleep, self.assertRaises(HTTPError)):
                    api.download('https://api.github.com/asset', io.BytesIO())
                self.assertEqual(request.call_count, 1)
                sleep.assert_not_called()
                dst = Mock()
                dst.upload.side_effect = error
                dst.pages.return_value = []
                with patch('time.sleep') as sleep, self.assertRaises(HTTPError):
                    upload_with_reconciliation(dst, 'url', io.BytesIO(b'data'), 'type', 'assets', 'name')
                self.assertEqual(dst.upload.call_count, 1)
                sleep.assert_not_called()

    def test_wrapped_permanent_dns_failure_is_not_retried(self):
        api = API('token')
        error = URLError(socket.gaierror(socket.EAI_NONAME, 'missing'))
        with (patch.object(api.opener, 'open', side_effect=error) as request,
              patch('time.sleep') as sleep, self.assertRaises(URLError)):
            api.download('https://api.github.com/asset', io.BytesIO())
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_permanent_dns_failure_is_not_retried(self):
        with (patch('socket.getaddrinfo', side_effect=socket.gaierror(socket.EAI_NONAME, 'missing')) as dns,
              patch('time.sleep') as sleep, self.assertRaises(socket.gaierror)):
            API().download('https://example.com/asset', io.BytesIO())
        self.assertEqual(dns.call_count, 1)
        sleep.assert_not_called()

    def test_retry_after_at_limit_is_respected(self):
        from repo_mirror.github_api import transfer_retry_delay

        error = HTTPError('url', 503, '', {'Retry-After': '60'}, None)
        self.assertEqual(transfer_retry_delay(error, 0), 60)

    def test_download_retries_partial_response_from_start(self):
        api = API("token")

        class Partial:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, size):
                if not hasattr(self, "read_once"):
                    self.read_once = True
                    return b"partial"
                raise TimeoutError()

        class Complete:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, size):
                if not hasattr(self, "read_once"):
                    self.read_once = True
                    return b"complete"
                return b""

        output = io.BytesIO()
        with patch.object(api.opener, "open", side_effect=[Partial(), Complete()]), patch("time.sleep"):
            api.download("https://api.github.com/asset", output)
        self.assertEqual(output.getvalue(), b"complete")

    def test_direct_upload_429_retries_through_real_opener(self):
        from email.message import Message
        from urllib.response import addinfourl
        from repo_mirror.repository import upload_with_reconciliation

        api = API('token')
        received = []
        def respond(handler, request):
            received.append(request.data.read())
            headers = Message()
            headers['Retry-After'] = '0'
            response = addinfourl(io.BytesIO(b''), headers, request.full_url,
                                  429 if len(received) == 1 else 200)
            response.msg = 'response'
            return response

        with (patch('urllib.request.HTTPSHandler.https_open', respond),
              patch.object(api, 'pages', return_value=[]), patch('time.sleep')):
            upload_with_reconciliation(api, 'https://uploads.github.com/asset',
                                       io.BytesIO(b'data'), 'type', 'assets', 'name')
        self.assertEqual(received, [b'data', b'data'])

    def test_upload_redirect_back_to_same_url_does_not_mark_or_retry(self):
        from email.message import Message
        from urllib.response import addinfourl
        from repo_mirror.repository import upload_with_reconciliation

        api = API('token')
        calls = []
        def respond(handler, request):
            calls.append(request.full_url)
            headers = Message()
            headers['Retry-After'] = '0'
            if len(calls) == 1:
                headers['Location'] = request.full_url
            response = addinfourl(io.BytesIO(b''), headers, request.full_url,
                                  302 if len(calls) == 1 else 429)
            setattr(response, 'msg', 'response')
            return response

        with (patch('urllib.request.HTTPSHandler.https_open', respond),
              patch.object(api, 'pages', return_value=[]), patch('time.sleep') as sleep,
              self.assertRaises(HTTPError) as raised):
            upload_with_reconciliation(api, 'https://uploads.github.com/asset',
                                       io.BytesIO(b'data'), 'type', 'assets', 'name')
        self.assertFalse(getattr(raised.exception, 'direct_upload_rejection', False))
        self.assertEqual(len(calls), 2)
        sleep.assert_not_called()
        raised.exception.close()

    def test_upload_429_from_opener_without_response_is_not_marked(self):
        api = API('token')
        error = HTTPError('https://uploads.github.com/asset', 429, '', {'Retry-After': '0'}, None)
        with patch.object(api.opener, 'open', side_effect=error), self.assertRaises(HTTPError) as raised:
            api.upload('https://uploads.github.com/asset', io.BytesIO(b'data'), 'type')
        self.assertFalse(getattr(raised.exception, 'direct_upload_rejection', False))

    def test_cooldown_rechecked_after_token_acquisition(self):
        from repo_mirror import github_api
        events = []
        def token():
            events.append('token')
            return 'token'
        api = API(token)
        def wait(host, budget):
            events.append('wait')
        def open_request(request, timeout):
            events.append('open')
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.headers = {}
            response.read.return_value = b''
            return response
        with patch.object(github_api, '_wait_for_cooldown', side_effect=wait), patch.object(api.opener, 'open', side_effect=open_request):
            api.request('GET', 'https://api.github.com/test')
        self.assertEqual(events, ['wait', 'token', 'wait', 'open'])

    def test_upload_5xx_with_delayed_visibility_never_replays(self):
        from repo_mirror.repository import upload_with_reconciliation

        dst = Mock()
        received = []

        def upload(url, data, kind):
            received.append(data.read())
            if len(received) == 1:
                raise HTTPError(url, 502, "failure", {}, None)

        dst.upload.side_effect = upload
        dst.pages.side_effect = [[], [{"name": "name", "state": "uploaded", "url": "asset"}]]
        with patch("time.sleep") as sleep, self.assertRaises(HTTPError):
            upload_with_reconciliation(dst, "url", io.BytesIO(b"data"), "type", "assets", "name")
        self.assertEqual(received, [b"data"])
        sleep.assert_not_called()

    def test_upload_unmarked_429_and_timeout_never_replay(self):
        from repo_mirror.repository import upload_with_reconciliation

        for error in (HTTPError("https://redirect.example/asset", 429, "", {}, None),
                      HTTPError("url", 429, "", {}, None), TimeoutError()):
            with self.subTest(error=error):
                dst = Mock()
                dst.upload.side_effect = error
                dst.pages.return_value = []
                with patch("time.sleep") as sleep, self.assertRaises(type(error)):
                    upload_with_reconciliation(dst, "url", io.BytesIO(b"data"), "type", "assets", "name")
                self.assertEqual(dst.upload.call_count, 1)
                sleep.assert_not_called()

    def test_upload_422_reconciles_matching_bytes(self):
        from repo_mirror.repository import upload_with_reconciliation

        dst = Mock()
        dst.upload.side_effect = HTTPError("url", 422, "failure", {}, None)
        dst.pages.return_value = [{"name": "name", "state": "uploaded", "url": "asset"}]
        dst.download.side_effect = lambda url, output: output.write(b"data")
        upload_with_reconciliation(dst, "url", io.BytesIO(b"data"), "type", "assets", "name")
        self.assertEqual(dst.upload.call_count, 1)

    def test_upload_matching_after_502_does_not_replay(self):
        from repo_mirror.repository import upload_with_reconciliation

        dst = Mock()
        dst.upload.side_effect = HTTPError("url", 502, "failure", {}, None)
        dst.pages.return_value = [{"name": "name", "state": "uploaded", "url": "asset"}]
        dst.download.side_effect = lambda url, output: output.write(b"data")
        upload_with_reconciliation(dst, "url", io.BytesIO(b"data"), "type", "assets", "name")
        self.assertEqual(dst.upload.call_count, 1)

    def test_upload_retry_then_422_reconciles(self):
        from repo_mirror.repository import upload_with_reconciliation

        dst = Mock()
        rejection = HTTPError("url", 429, "", {}, None)
        rejection.direct_upload_rejection = True
        received = []
        def upload(url, data, kind):
            received.append(data.read())
            if len(received) == 1:
                raise rejection
            raise HTTPError("url", 422, "", {}, None)
        dst.upload.side_effect = upload
        dst.pages.side_effect = [[], [{"name": "name", "state": "uploaded", "url": "asset"}]]
        dst.download.side_effect = lambda url, output: output.write(b"data")
        with patch("time.sleep"):
            upload_with_reconciliation(dst, "url", io.BytesIO(b"data"), "type", "assets", "name")
        self.assertEqual(dst.upload.call_count, 2)
        self.assertEqual(received, [b"data", b"data"])

    def test_upload_ambiguous_remote_fails_closed(self):
        from repo_mirror.journal import ReleaseRecoveryError
        from repo_mirror.repository import upload_with_reconciliation

        for assets in (
            [{"name": "name", "state": "starter", "url": "asset"}],
            [{"name": "name", "state": "uploaded", "url": "asset"}] * 2,
            [{"name": "name", "state": "uploaded", "url": "asset"}],
        ):
            with self.subTest(assets=assets):
                dst = Mock()
                dst.upload.side_effect = HTTPError("url", 502, "", {}, None)
                dst.pages.return_value = assets
                dst.download.side_effect = lambda url, output: output.write(b"wrong")
                with self.assertRaises(ReleaseRecoveryError):
                    upload_with_reconciliation(dst, "url", io.BytesIO(b"data"), "type", "assets", "name")
                self.assertEqual(dst.upload.call_count, 1)
                dst.request.assert_not_called()

    def test_upload_exhaustion_is_bounded(self):
        from repo_mirror.repository import upload_with_reconciliation

        dst = Mock()
        def rejected(*args):
            error = HTTPError("url", 429, "", {}, None)
            error.direct_upload_rejection = True
            raise error
        dst.upload.side_effect = rejected
        dst.pages.return_value = []
        with patch("time.sleep") as sleep, self.assertRaises(HTTPError):
            upload_with_reconciliation(dst, "url", io.BytesIO(b"data"), "type", "assets", "name")
        self.assertEqual(dst.upload.call_count, 4)
        self.assertEqual(sleep.call_count, 3)

    def test_nontransient_download_is_not_retried(self):
        api = API("token")
        with (
            patch.object(api.opener, "open", side_effect=HTTPError("url", 404, "", {}, None)) as request,
            patch("time.sleep") as sleep,self.assertRaises(HTTPError)
        ):
            api.download("https://api.github.com/asset", io.BytesIO())
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()
