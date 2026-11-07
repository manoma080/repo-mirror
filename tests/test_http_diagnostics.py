import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import mirror


class HTTPDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        secret = patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'})
        secret.start()
        self.addCleanup(secret.stop)
    def test_sync_http_error_reports_status_without_untrusted_url_or_reason(self):
        config = {'destination_account_type': 'personal', 'sources': [
            {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice', 'repos': ['demo']}]}
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8') as f:
            json.dump(config, f)
            f.flush()
            error = HTTPError('https://user:password@evil.example/token-in-path?access_token=query-secret',
                              403, 'secret-reason', {}, None)
            stderr = io.StringIO()
            with patch.dict(os.environ, {'GH_TOKEN': 'secret-token', 'DESTINATION_OWNER': 'dest'}), \
                 patch.object(mirror, 'get_existing', return_value=None), \
                 patch.object(mirror, 'mirror_repository', side_effect=error), \
                 contextlib.redirect_stderr(stderr):
                with self.assertRaises(RuntimeError):
                    mirror.main(['--config', f.name])
            error.close()
        message = stderr.getvalue()
        self.assertIn('HTTP 403', message)
        self.assertIn('mirror failed for repository 1 (source 1/1, alice/demo)', message)
        for secret in ('password', 'evil.example', 'token-in-path', 'query-secret', 'secret-reason', 'secret-token'):
            self.assertNotIn(secret, message)
        self.assertNotIn('rate limit', message.lower())

    def test_invalid_http_code_is_not_logged(self):
        error = HTTPError('https://example.com', 'secret-code', '', {}, None)
        self.assertEqual(mirror.error_detail(error), 'HTTPError')
        error.close()


class NewDiagnosticsTests(unittest.TestCase):
    def test_api_422_has_safe_operation_and_preserves_exception(self):
        api = mirror.API()
        error = HTTPError('https://example.test/?secret=token', 422, 'secret', {}, None)
        with patch.object(api.opener, 'open', side_effect=error):
            with self.assertRaises(HTTPError) as raised:
                api.request('POST', 'https://example.test/api')
        self.assertIs(raised.exception, error)
        self.assertEqual(mirror.error_detail(error), 'HTTP 422 (source API POST)')
        error.close()

    def test_source_download_status_is_reported_without_changing_exception_type(self):
        from unittest.mock import MagicMock
        response = MagicMock(status=404)
        response.headers = {}
        conn = MagicMock()
        conn.getresponse.return_value = response
        with patch.object(mirror.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))]), \
             patch.object(mirror.socket, 'socket'), patch.object(mirror.ssl, 'create_default_context'), \
             patch.object(mirror.http.client, 'HTTPSConnection', return_value=conn):
            with self.assertRaises(RuntimeError) as raised:
                mirror.API().download('https://example.test/secret?token=secret', io.BytesIO())
        self.assertEqual(mirror.error_detail(raised.exception), 'HTTP 404 (source asset GET)')

    def test_untrusted_rate_headers_cannot_break_diagnostics(self):
        class BadHeaders:
            def get(self, key):
                raise RuntimeError('secret')
        error = HTTPError('https://example.test', 403, 'secret', BadHeaders(), None)
        error.mirror_operation = ('source', 'API', 'POST')
        self.assertEqual(mirror.error_detail(error), 'HTTP 403 (source API POST)')
        error.close()

    def test_app_auth_failure_keeps_http_error_and_operation(self):
        from types import SimpleNamespace
        error = HTTPError('https://example.test/?token=secret', 422, 'secret', {}, None)
        with patch('mirror.subprocess.run', return_value=SimpleNamespace(stdout=b'signature')), \
             patch('mirror.build_opener') as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(HTTPError) as raised:
                mirror.AppTokenProvider('123', 'key', 'dest')._mint()
        self.assertIs(raised.exception, error)
        self.assertEqual(mirror.error_detail(error), 'HTTP 422 (destination app authentication GET)')
        error.close()
