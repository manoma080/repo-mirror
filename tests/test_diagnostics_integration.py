import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import mirror
from test_reconcile_assets import SOURCE, Source, Destination


class DiagnosticsIntegrationTests(unittest.TestCase):
    def run_main(self, source, destination):
        config = {'destination_account_type': 'organization', 'sources': [{**SOURCE, 'repos': ['demo']}]}
        output = io.StringIO()
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8') as f:
            json.dump(config, f)
            f.flush()
            with patch.dict(os.environ, {'GH_TOKEN': 'secret-token', 'DESTINATION_OWNER': 'dest', 'MIRROR_TIMESTAMP_SECRET': 'test-key'}, clear=True), \
                 patch.object(mirror, 'SourceAPI', return_value=source), \
                 patch.object(mirror, 'API', side_effect=[object(), destination]), \
                 patch.object(mirror, 'mirror_git'), contextlib.redirect_stderr(output):
                with self.assertRaisesRegex(RuntimeError, '1 repositories failed'):
                    mirror.main(['--config', f.name])
        text = output.getvalue()
        for secret in ('secret-token', 'secret-reason', 'secret-body', 'secret-path', 'https://'):
            self.assertNotIn(secret, text)
        self.assertIn('summary sources=1 discovery_failed=0 repositories=1 succeeded=0 failed=1', text)
        return text

    def test_release_create_422_is_named_in_main_summary(self):
        error = HTTPError('https://host/secret-path', 422, 'secret-reason', {}, io.BytesIO(b'secret-body'))
        self.addCleanup(error.close)
        api = mirror.API('secret-token')
        api.opener.open = MagicMock(side_effect=error)

        class CreateFailure(Destination):
            def request(self, method, url, data=None):
                if '/releases/tags/' in url:
                    raise HTTPError('https://host/secret-path', 404, 'secret-reason', {}, None)
                if method == 'POST' and url.endswith('/releases'):
                    return api.request(method, url, data)
                return super().request(method, url, data)
            def pages(self, url):
                return []

        text = self.run_main(Source(), CreateFailure())
        self.assertIn('mirror failed for repository 1 (source 1/1, alice/demo): HTTP 422 (destination release create)', text)
        self.assertEqual(api.opener.open.call_count, 1)
        self.assertEqual(api.opener.open.call_args.args[0].get_method(), 'POST')

    def test_release_create_auth_refresh_failure_is_not_a_release_post(self):
        import subprocess
        calls = []
        errors = []
        self.addCleanup(lambda: [error.close() for error in errors])
        class AuthOpener:
            def open(self, request, timeout):
                calls.append(request.get_method())
                error = HTTPError(request.full_url + '?secret-path', 503, 'secret-reason', {}, None)
                errors.append(error)
                raise error
        provider = mirror.AppTokenProvider('1', 'key', 'dest')
        api = mirror.API(provider)
        api.opener.open = MagicMock(side_effect=AssertionError('release POST must not occur'))
        class RefreshFailure(Destination):
            def request(self, method, url, data=None):
                if '/releases/tags/' in url:
                    raise HTTPError('https://host/secret-path', 404, 'secret-reason', {}, None)
                if method == 'POST' and url.endswith('/releases'):
                    return api.request(method, url, data)
                return super().request(method, url, data)
            def pages(self, url):
                return []
        with patch('mirror.build_opener', return_value=AuthOpener()), \
             patch('mirror.subprocess.run', return_value=subprocess.CompletedProcess([], 0, stdout=b'sig')), \
             patch('mirror.time.sleep') as sleep:
            text = self.run_main(Source(), RefreshFailure())
        self.assertIn('HTTP 503 (destination app authentication GET)', text)
        self.assertNotIn('destination release create', text)
        self.assertEqual(calls, ['GET'] * 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2, 4])
        api.opener.open.assert_not_called()

    def test_source_download_404_reaches_main_summary(self):
        class DownloadFailure(Source):
            download = mirror.API.download
            role = 'source'
            token = None

        response = MagicMock(status=404, headers={})
        conn = MagicMock()
        conn.getresponse.return_value = response
        destination = Destination()
        with patch.object(mirror.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))]), \
             patch.object(mirror.socket, 'socket'), patch.object(mirror.ssl, 'create_default_context'), \
             patch.object(mirror.http.client, 'HTTPSConnection', return_value=conn):
            text = self.run_main(DownloadFailure(), destination)
        self.assertIn('mirror failed for repository 1 (source 1/1, alice/demo): HTTP 404 (source asset GET)', text)
        self.assertEqual(conn.getresponse.call_count, 1)
        self.assertEqual(destination.deleted, [])
