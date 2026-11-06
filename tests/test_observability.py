import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch
from urllib.error import URLError

import mirror
from test_reconcile_assets import Source, Destination, SOURCE


class ObservabilityTests(unittest.TestCase):
    def test_public_names_and_elapsed_are_logged(self):
        output = io.StringIO()
        dst = Destination(); dst.fail = False
        with patch.object(mirror, 'mirror_git'), redirect_stderr(output):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', Source(), dst, 'secret-token')
        text = output.getvalue()
        self.assertIn('source_repo=alice/demo', text)
        self.assertIn('tag=', text)
        self.assertIn('asset_name=', text)
        self.assertIn('elapsed=', text)
        self.assertIn('reason=', text)
        self.assertNotIn('secret-token', text)
        self.assertNotIn('https://', text)

    def test_hostile_labels_are_suppressed(self):
        for value in ('v1\n::warning::evil', 'ghp_abcdefghijklmnopqrstuvwxyz1234567890',
                      'https://host/file?signature=secret', '\x1b[31m', 'secret-token'):
            self.assertEqual(mirror.safe_label(value), '[redacted]')
        self.assertEqual(mirror.safe_label('v1.2.3'), 'v1.2.3')

    def test_repository_failure_still_logs_name_and_elapsed(self):
        output = io.StringIO()
        class Broken(Source):
            def request(self, *args):
                raise RuntimeError('sensitive details')
        with patch.object(mirror, 'mirror_git'), redirect_stderr(output):
            with self.assertRaises(RuntimeError):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', Broken(), Destination(), 'opaquevalue123456789')
        self.assertIn('source_repo=alice/demo', output.getvalue())
        self.assertIn('elapsed=', output.getvalue())
        self.assertNotIn('sensitive details', output.getvalue())

    def test_explicit_credentials_are_not_exposed_as_labels(self):
        output = io.StringIO()
        class Hostile(Source):
            def pages(self, url):
                values = super().pages(url)
                if url.endswith('/releases'):
                    values[0]['tag_name'] = 'opaquevalue123456789'
                return values
        dst = Destination(); dst.fail = False
        with patch.object(mirror, 'mirror_git'), redirect_stderr(output):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', Hostile(), dst, 'opaquevalue123456789')
        self.assertNotIn('opaquevalue123456789', output.getvalue())

    def test_failed_request_is_timed_without_url_or_exception(self):
        api = mirror.API()
        api.opener.open = unittest.mock.Mock(side_effect=URLError('private'))
        output = io.StringIO()
        with patch.object(mirror.time, 'sleep'), redirect_stderr(output):
            with self.assertRaises(URLError):
                api._request('GET', 'https://example.com/?token=private')
        self.assertEqual(output.getvalue().count('network target=source'), 4)
        self.assertIn('elapsed=', output.getvalue())
        self.assertNotIn('private', output.getvalue())
        self.assertNotIn('https://', output.getvalue())
