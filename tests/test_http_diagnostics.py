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
        for secret in ('password', 'evil.example', 'token-in-path', 'query-secret', 'secret-reason', 'secret-token'):
            self.assertNotIn(secret, message)
        self.assertNotIn('rate limit', message.lower())

    def test_invalid_http_code_is_not_logged(self):
        error = HTTPError('https://example.com', 'secret-code', '', {}, None)
        self.assertEqual(mirror.error_detail(error), 'HTTPError')
        error.close()
