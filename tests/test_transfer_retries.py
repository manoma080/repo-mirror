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

    def test_upload_5xx_retries_with_rewound_body(self):
        from repo_mirror.repository import upload_with_reconciliation

        dst = Mock()
        received = []

        def upload(url, data, kind):
            received.append(data.read())
            if len(received) == 1:
                raise HTTPError(url, 502, "failure", {}, None)

        dst.upload.side_effect = upload
        dst.pages.return_value = []
        with patch("time.sleep"):
            upload_with_reconciliation(dst, "url", io.BytesIO(b"data"), "type", "assets", "name")
        self.assertEqual(received, [b"data", b"data"])

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
        dst.upload.side_effect = [HTTPError("url", 503, "", {}, None), HTTPError("url", 422, "", {}, None)]
        dst.pages.side_effect = [[], [{"name": "name", "state": "uploaded", "url": "asset"}]]
        dst.download.side_effect = lambda url, output: output.write(b"data")
        with patch("time.sleep"):
            upload_with_reconciliation(dst, "url", io.BytesIO(b"data"), "type", "assets", "name")
        self.assertEqual(dst.upload.call_count, 2)

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
        dst.upload.side_effect = lambda *args: (_ for _ in ()).throw(HTTPError("url", 503, "", {}, None))
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
