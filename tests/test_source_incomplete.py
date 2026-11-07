import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import mirror


class SourceIncompleteTests(unittest.TestCase):
    def setUp(self):
        secret = patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'})
        secret.start()
        self.addCleanup(secret.stop)

    def test_starter_checksum_is_skipped_and_release_finalized(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice'}
        class Src:
            def request(self, method, url):
                if url.endswith('/releases/latest'): raise HTTPError(url, 404, '', {}, io.BytesIO())
                return {'clone_url': 'https://github.com/alice/demo.git'}
            def pages(self, url):
                if url.endswith('/releases'): return [{'id': 1, 'tag_name': 'v1'}]
                return [{'name': 'SHA256SUMS', 'size': 0, 'state': 'starter', 'browser_download_url': 'https://github.com/file'}]
            def download(self, *args): raise AssertionError('must not download starter')
        class Dst:
            def __init__(self): self.calls = []
            def request(self, method, url, data=None):
                self.calls.append((method, url))
                if url.endswith('/repos/dest/mirror-alice-demo'): return {'description': mirror.source_marker(source, 'demo')}
                if '/releases/tags/' in url: return {'id': 2, 'name': 'old'}
                return {}
            def pages(self, url): return []
        dst = Dst()
        class Journal:
            failures = {}
            def key(self, *args): return 'key'
            def set(self, *args): raise AssertionError('starter must not be journaled')
            def retry_order(self, source, name, owner, tag, assets): return assets
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(source, 'demo', 'dest', Src(), dst, 'token', journal=Journal())
        self.assertEqual([method for method, url in dst.calls if '/releases' in url and method != 'GET'], ['PATCH', 'PATCH', 'PATCH'])
