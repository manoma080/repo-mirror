import gc
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch
from urllib.error import HTTPError

import mirror
from test_reconcile_assets import SOURCE, Source, Destination


class LogSecretsTests(unittest.TestCase):
    def test_untrusted_identifiers_are_not_logged(self):
        secret = 'ghp_' + 'A' * 36
        config = {'destination_account_type': 'personal', 'sources': [dict(SOURCE, owner=secret, repos=[secret])]}
        output = io.StringIO()
        with tempfile.NamedTemporaryFile(mode='w') as f:
            json.dump(config, f); f.flush()
            with patch.dict(os.environ, {'GH_TOKEN': 'token', 'DESTINATION_OWNER': 'dest'}), patch.object(mirror, 'get_existing', return_value=None), patch.object(mirror, 'mirror_repository'), redirect_stderr(output):
                mirror.main(['--config', f.name])
        self.assertNotIn(secret, output.getvalue())
        self.assertIn('source 1/1', output.getvalue())
        self.assertIn('repository 1/1', output.getvalue())

    def test_release_and_asset_names_are_not_logged(self):
        secret = 'github_pat_' + 'A' * 50
        class SecretSource(Source):
            def pages(self, url):
                values = super().pages(url)
                if url.endswith('/releases'):
                    values[0]['tag_name'] = secret
                else:
                    values[0]['name'] = secret
                return values
        output = io.StringIO()
        with patch.object(mirror, 'mirror_git'), redirect_stderr(output):
            with self.assertRaises(RuntimeError):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', SecretSource(), Destination(), 'token')
        self.assertNotIn(secret, output.getvalue())
        self.assertIn('release 1/1', output.getvalue())
        self.assertIn('asset 1/2', output.getvalue())

    def test_journal_upload_http_failure_reports_safe_status(self):
        class FailedDestination(Destination):
            def upload(self, url, data, content_type):
                raise HTTPError('https://example.test/?token=secret', 403, 'secret', {}, io.BytesIO())
        gc.collect()  # Dispose of unrelated HTTPError fixtures before capturing stderr.
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, patch.object(mirror, 'mirror_git'), redirect_stderr(output):
            journal = mirror.FailureJournal(os.path.join(directory, 'journal'))
            with self.assertRaises(RuntimeError):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', Source(), FailedDestination(), 'token', journal=journal)
        self.assertIn('HTTP 403', output.getvalue())
        self.assertNotIn('secret', output.getvalue())
