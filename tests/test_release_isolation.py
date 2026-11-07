import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import mirror

SOURCE = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice'}

class IsolationTests(unittest.TestCase):
    def setUp(self):
        secret = patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'})
        secret.start()
        self.addCleanup(secret.stop)

    def test_missing_tag_does_not_prevent_other_releases(self):
        class Src:
            def request(self, method, url):
                if url.endswith('/releases/latest'): raise HTTPError(url, 404, '', {}, io.BytesIO())
                return {'clone_url': 'https://github.com/alice/demo.git'}
            def pages(self, url):
                if url.endswith('/releases'): return [{'id': 1}, {'id': 2, 'tag_name': 'good'}]
                return []
        class Dst:
            def __init__(self): self.tags = []; self.patches = []
            def request(self, method, url, data=None):
                if url.endswith('/repos/dest/mirror-alice-demo') and method == 'GET':
                    return {'description': mirror.source_marker(SOURCE, 'demo') + ' [repo-mirror:initializing:v1]'}
                if '/releases/tags/' in url:
                    self.tags.append(url.rsplit('/', 1)[-1]); return {'id': 2, 'name': 'old'}
                if method == 'PATCH': self.patches.append((url, data))
                return {}
            def pages(self, url): return []
        dst = Dst()
        with patch.object(mirror, 'mirror_git'), self.assertRaises(Exception):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', Src(), dst, 'token')
        self.assertEqual(dst.tags, ['good'])
        self.assertTrue(any(url.endswith('/releases/2') and 'body' in data and '[repo-mirror:pending:v3]' not in (data['body'] or '') for url, data in dst.patches))

    def test_failure_continues_history_without_completing_initialization(self):
        class Src:
            def request(self, method, url):
                if url.endswith('/releases/latest'): raise HTTPError(url, 404, '', {}, io.BytesIO())
                return {'clone_url': 'https://github.com/alice/demo.git'}
            def pages(self, url):
                if url.endswith('/releases'): return [{'id': 2, 'tag_name': 'good'}, {'id': 1, 'tag_name': 'bad'}]
                if '/1/assets' in url: raise RuntimeError('manifest broken')
                return []
        class Dst:
            def __init__(self): self.tags = []; self.patches = []
            def request(self, method, url, data=None):
                if url.endswith('/repos/dest/mirror-alice-demo') and method == 'GET':
                    return {'description': mirror.source_marker(SOURCE, 'demo') + ' [repo-mirror:initializing:v1]'}
                if '/releases/tags/' in url:
                    self.tags.append(url.rsplit('/', 1)[-1]); return {'id': 2, 'name': 'old'}
                if method == 'PATCH': self.patches.append((url, data))
                return {}
            def pages(self, url): return []
        dst = Dst()
        with patch.object(mirror, 'mirror_git'), self.assertRaises(RuntimeError):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', Src(), dst, 'token')
        self.assertEqual(dst.tags, ['bad', 'good'])
        self.assertTrue(any(url.endswith('/releases/2') and 'body' in data and '[repo-mirror:pending:v3]' not in (data['body'] or '') for url, data in dst.patches))
        self.assertFalse(any('description' in data for _, data in dst.patches))
        self.assertFalse(any(data.get('make_latest') == 'true' for _, data in dst.patches))
