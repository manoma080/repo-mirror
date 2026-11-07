import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch
from urllib.error import URLError

import mirror
from test_reconcile_assets import Source, Destination, SOURCE


class ProgressTests(unittest.TestCase):
    def setUp(self):
        secret = patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'})
        secret.start()
        self.addCleanup(secret.stop)

    def test_main_reports_counts_and_never_logs_destination(self):
        config = {'destination_account_type': 'personal', 'sources': [dict(SOURCE, repos=['demo'])]}
        output = io.StringIO()
        with tempfile.NamedTemporaryFile(mode='w') as f:
            json.dump(config, f); f.flush()
            with patch.dict(os.environ, {'GH_TOKEN': 'secret-token', 'DESTINATION_OWNER': 'private-owner'}), patch.object(mirror, 'get_existing', return_value=None), patch.object(mirror, 'mirror_repository'), redirect_stderr(output):
                mirror.main(['--config', f.name])
        text = output.getvalue()
        for part in ('source 1', 'discovered=1', 'repository 1/1', 'completed', 'summary', 'succeeded=1', 'failed=0'):
            self.assertIn(part, text)
        self.assertNotIn('secret-token', text)
        self.assertNotIn('private-owner', text)

    def test_release_asset_events_and_no_untrusted_values(self):
        dst = Destination(); dst.fail = False
        output = io.StringIO()
        with patch.object(mirror, 'mirror_git'), redirect_stderr(output):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', Source(), dst, 'secret-token')
        text = output.getvalue()
        for part in ('releases', 'release 1/1', 'asset', 'updated', 'uploaded', 'completed'):
            self.assertIn(part, text)
        self.assertNotIn('secret-token', text)
        self.assertNotIn('https://', text)

    def test_http_retry_reports_attempt_without_exception_or_url(self):
        api = mirror.API()
        api.opener.open = unittest.mock.Mock(side_effect=[URLError('secret\nhttps://signed'), URLError('secret'), URLError('secret'), URLError('secret')])
        output = io.StringIO()
        with patch.object(mirror.time, 'sleep'), redirect_stderr(output):
            with self.assertRaises(URLError):
                api._request('GET', 'https://example.com/?token=secret')
        self.assertIn('retry', output.getvalue())
        self.assertNotIn('secret', output.getvalue())
        self.assertNotIn('https://', output.getvalue())

    def test_asset_progress_uses_ordinals_not_untrusted_names(self):
        class HostileSource(Source):
            def pages(self, url):
                values = super().pages(url)
                if url.endswith('/releases'):
                    values[0]['tag_name'] = 'secret\n\x1b[31m'
                return values

        dst = Destination(); dst.fail = False
        output = io.StringIO()
        with patch.object(mirror, 'mirror_git'), redirect_stderr(output):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', HostileSource(), dst, 'secret-token')
        text = output.getvalue()
        self.assertIn('asset 1/2 started', text)
        self.assertNotIn('secret', text)
        self.assertNotIn('\x1b', text)
