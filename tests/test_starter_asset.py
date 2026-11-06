import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import mirror
from test_mirror import pending


class StarterAssetTests(unittest.TestCase):
    def test_zero_byte_starter_requires_manual_removal_even_for_empty_source(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice', 'repos': ['demo']}
        class Src:
            def request(self, *args):
                if args[1].endswith('/releases/latest'):
                    raise __import__('urllib.error', fromlist=['HTTPError']).HTTPError(args[1], 404, '', {}, None)
                return {'clone_url': 'https://github.com/alice/demo.git'}
            def pages(self, url):
                if url.endswith('/releases'): return [{'id': 1, 'tag_name': 'v1'}]
                return [{'name': 'empty', 'size': 0, 'browser_download_url': 'https://github.com/empty'}]
            def download(self, url, output): pass
        class Dst:
            def __init__(self): self.calls = []
            def request(self, method, url, data=None):
                self.calls.append((method, url))
                if url.endswith('/repos/dest/mirror-alice-demo'): return {'description': mirror.source_marker(source, 'demo')}
                if '/releases/tags/' in url: return {'id': 2, 'name': pending('empty', 0, 'https://github.com/empty')}
                return {}
            def pages(self, url): return [{'name': 'empty', 'size': 0, 'state': 'starter', 'url': 'https://api.github.com/a'}]
            def download(self, url, output): pass
            def upload(self, *args): self.calls.append(('UPLOAD', ''))
        dst = Dst()
        with patch.object(mirror, 'mirror_git'):
            with self.assertRaisesRegex(RuntimeError, '(?i)starter.*manual'):
                mirror.mirror_repository(source, 'demo', 'dest', Src(), dst, 'token')
        self.assertFalse(any(method in ('DELETE', 'UPLOAD', 'PATCH') for method, url in dst.calls))

    def test_main_reports_changed_manifest_recovery_without_source_data(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice', 'repos': ['demo']}
        class Src:
            def request(self, *args):
                if args[1].endswith('/releases/latest'):
                    raise __import__('urllib.error', fromlist=['HTTPError']).HTTPError(args[1], 404, '', {}, None)
                return {'clone_url': 'https://github.com/alice/demo.git'}
            def pages(self, url):
                if url.endswith('/releases'): return [{'id': 1, 'tag_name': 'https://example.test/?token=secret'}]
                return [{'name': 'secret-file', 'size': 1, 'browser_download_url': 'https://github.com/secret'}]
        class Dst:
            def request(self, method, url, data=None):
                if '/releases/tags/' in url: return {'id': 2, 'name': pending('other', 1, 'https://github.com/other')}
                if url.endswith('/repos/dest/mirror-alice-demo'): return {'description': mirror.source_marker(source, 'demo')}
                return {}
        stderr = io.StringIO()
        with tempfile.NamedTemporaryFile(mode='w') as f:
            json.dump({'destination_account_type': 'personal', 'sources': [source]}, f); f.flush()
            with patch.dict(os.environ, {'GH_TOKEN': 'token', 'DESTINATION_OWNER': 'dest'}), patch.object(mirror, 'SourceAPI', return_value=Src()), patch.object(mirror, 'API', side_effect=[object(), Dst()]), patch.object(mirror, 'mirror_git'), redirect_stderr(stderr):
                with self.assertRaises(RuntimeError): mirror.main(['--config', f.name])
        self.assertIn('assets changed', stderr.getvalue())
        self.assertIn('manual', stderr.getvalue())
        self.assertIn('repository 1', stderr.getvalue())
        self.assertNotIn('mirror-alice-demo', stderr.getvalue())
        self.assertNotIn('secret', stderr.getvalue())

    def test_main_reports_manual_action_for_starter_asset(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice', 'repos': ['demo']}
        stderr = io.StringIO()
        with tempfile.NamedTemporaryFile(mode='w') as f:
            json.dump({'destination_account_type': 'personal', 'sources': [source]}, f); f.flush()
            with patch.dict(os.environ, {'GH_TOKEN': 'token', 'DESTINATION_OWNER': 'dest'}), patch.object(mirror, 'get_existing', return_value=None), patch.object(mirror, 'mirror_repository', side_effect=mirror.StarterAssetError()), redirect_stderr(stderr):
                with self.assertRaises(RuntimeError): mirror.main(['--config', f.name])
        self.assertRegex(stderr.getvalue().lower(), 'starter.*manual')
        self.assertIn('repository 1', stderr.getvalue())
        self.assertNotIn('mirror-alice-demo', stderr.getvalue())
