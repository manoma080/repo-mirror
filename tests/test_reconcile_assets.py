import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import mirror

SOURCE = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice'}

class Source:
    def request(self, *args): return {'clone_url': 'https://github.com/alice/demo.git'}
    def pages(self, url):
        if url.endswith('/releases'): return [{'id': 1, 'tag_name': 'v1', 'name': 'new', 'body': 'notes'}]
        return [{'name': 'changed', 'size': 3, 'browser_download_url': 'https://github.com/changed'}, {'name': 'new', 'size': 1, 'browser_download_url': 'https://github.com/new'}]
    def download(self, url, out): out.write(b'new' if url.endswith('changed') else b'n')

class Destination:
    def __init__(self):
        self.release = {'id': 2, 'name': 'old', 'upload_url': 'https://uploads.github.com/x{?name}'}
        self.assets = {'changed': b'old', 'removed': b'keep'}
        self.fail = True
        self.deleted = []
    def request(self, method, url, data=None):
        if url.endswith('/repos/dest/mirror-alice-demo'): return {'description': mirror.source_marker(SOURCE, 'demo')}
        if '/releases/tags/' in url: return self.release.copy()
        if method == 'PATCH' and url.endswith('/releases/2'): self.release.update(data); return self.release.copy()
        if method == 'DELETE':
            self.deleted.append('changed'); del self.assets['changed']
        return {}
    def pages(self, url):
        return [{'id': i, 'name': n, 'size': len(b), 'state': 'uploaded', 'url': 'https://api.github.com/assets/'+n} for i,(n,b) in enumerate(self.assets.items(), 1)]
    def download(self, url, out): out.write(self.assets[url.rsplit('/', 1)[-1]])
    def upload(self, url, data, content_type):
        if self.fail: raise RuntimeError('interrupted')
        self.assets[url.split('name=')[1]] = data.read()

class ReconcileTests(unittest.TestCase):
    def test_source_removed_before_replacement_is_never_deleted(self):
        class ChangingSource(Source):
            def __init__(self): self.count = 0
            def pages(self, url):
                if not url.endswith('/releases'):
                    self.count += 1
                    if self.count > 1: return []
                return super().pages(url)
        dst = Destination()
        with patch.object(mirror, 'mirror_git'):
            with self.assertRaises(mirror.ReleaseRecoveryError):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', ChangingSource(), dst, 'token')
        self.assertEqual(dst.deleted, [])
        self.assertEqual(dst.assets['changed'], b'old')

    def test_replacement_interruption_retry_and_removed_asset_safety(self):
        dst = Destination()
        with tempfile.TemporaryDirectory() as directory, patch.object(mirror, 'mirror_git'):
            path = Path(directory) / 'failures.json'
            journal = mirror.FailureJournal(path)
            with self.assertRaises(RuntimeError):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', Source(), dst, 'token', journal=journal)
            self.assertEqual(dst.deleted, ['changed'])
            self.assertEqual(dst.assets['removed'], b'keep')
            self.assertTrue(mirror.FailureJournal(path).failures)
            dst.fail = False
            mirror.mirror_repository(SOURCE, 'demo', 'dest', Source(), dst, 'token', journal=mirror.FailureJournal(path))
            self.assertEqual(dst.assets, {'changed': b'new', 'new': b'n', 'removed': b'keep'})
            self.assertEqual(dst.release['name'], 'new')
            self.assertEqual(dst.release['body'], 'notes')
            self.assertFalse(mirror.FailureJournal(path).failures)
