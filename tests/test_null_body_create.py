import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import mirror


class NullBodyCreateTests(unittest.TestCase):
    def setUp(self):
        secret = patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'})
        secret.start()
        self.addCleanup(secret.stop)

    def test_create_omits_null_body(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice'}
        class Src:
            def request(self, method, url):
                if url.endswith('/releases/latest'): raise HTTPError(url, 404, '', {}, io.BytesIO())
                return {'clone_url': 'https://github.com/alice/demo.git'}
            def pages(self, url):
                return [{'id': 1, 'tag_name': 'v1', 'body': None}] if url.endswith('/releases') else []
        class Dst:
            def __init__(self): self.payload = None; self.patches = []
            def request(self, method, url, data=None):
                if url.endswith('/repos/dest/mirror-alice-demo'): return {'description': mirror.source_marker(source, 'demo')}
                if '/releases/tags/' in url: raise HTTPError(url, 404, '', {}, io.BytesIO())
                if method == 'POST' and url.endswith('/releases'):
                    self.payload = data
                    return {'id': 2}
                if method == 'PATCH' and url.endswith('/releases/2'): self.patches.append(data)
                return {}
            def pages(self, url): return []
        dst = Dst()
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(source, 'demo', 'dest', Src(), dst, 'token')
        self.assertIn('WAITING_SOURCE', dst.payload['body'])
        self.assertIsNone(dst.patches[-1]['body'])
