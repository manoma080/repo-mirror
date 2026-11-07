import base64
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

import mirror

SOURCE = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice'}


class Source:
    def __init__(self):
        self.releases = [{'id': n, 'tag_name': str(n), 'name': 'current'} for n in range(12)]
    def request(self, *args):
        if args[1].endswith('/releases/latest'):
            raise __import__('urllib.error', fromlist=['HTTPError']).HTTPError(args[1], 404, '', {}, None)
        return {'clone_url': 'https://github.com/alice/demo.git'}
    def pages(self, url): return self.releases if url.endswith('/releases') else []


class Destination:
    def __init__(self, pending_tags=()):
        digest = hashlib.sha256(b'[]').hexdigest()
        marker = '[repo-mirror:pending:v2]' + base64.urlsafe_b64encode(json.dumps(['current', digest]).encode()).decode()
        self.releases = {str(n): {'id': n, 'tag_name': str(n), 'name': marker if str(n) in pending_tags else 'old'} for n in range(12)}
        self.patched = []
    def pages(self, url):
        if url.endswith('/releases'): return list(self.releases.values())
        return []
    def request(self, method, url, data=None):
        if url.endswith('/repos/dest/mirror-alice-demo'): return {'description': mirror.source_marker(SOURCE, 'demo')}
        if '/releases/tags/' in url: return self.releases[url.rsplit('/', 1)[-1]].copy()
        if method == 'PATCH' and '/releases/' in url:
            tag = url.rsplit('/', 1)[-1]
            self.patched.append((tag, data.copy()))
            self.releases[tag].update(data)
        return {}


class RetryScopeTests(unittest.TestCase):
    def setUp(self):
        secret = patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'})
        secret.start()
        self.addCleanup(secret.stop)

    def test_pending_release_restores_current_source_name(self):
        dst = Destination(['0'])
        src = Source()
        src.releases[0]['name'] = 'renamed'
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
        self.assertEqual(dst.releases['0']['name'], 'renamed')

    def test_old_failed_asset_upload_is_retried_and_journal_cleared(self):
        class AssetSource(Source):
            def pages(self, url):
                if url.endswith('/releases'): return self.releases
                if url.endswith('/11/assets'):
                    return [{'name': 'file', 'size': 1, 'browser_download_url': 'https://github.com/file'}]
                return []
            def download(self, url, out): out.write(b'x')
        class AssetDestination(Destination):
            def __init__(self):
                super().__init__()
                self.releases['11']['upload_url'] = 'https://uploads.github.com/x{?name}'
                self.assets = []
            def pages(self, url):
                if url.endswith('/11/assets'): return self.assets
                return super().pages(url)
            def upload(self, url, data, content_type):
                self.content = data.read()
                self.assets.append({'name': 'file', 'size': len(self.content), 'state': 'uploaded', 'url': 'https://api.github.com/file'})
            def download(self, url, out): out.write(self.content)
        dst = AssetDestination()
        with tempfile.TemporaryDirectory() as directory:
            journal = mirror.FailureJournal(directory + '/failures.json')
            key = journal.key(SOURCE, 'demo', 'dest', '11', 'file')
            journal.set(key, True)
            with patch.object(mirror, 'mirror_git'):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', AssetSource(), dst, 'secret-token', journal=journal)
            self.assertNotIn(key, journal.failures)
            with open(journal.path) as stream:
                self.assertNotIn('secret-token', stream.read())
        self.assertEqual(dst.assets[0]['name'], 'file')

    def test_old_release_with_journal_entry_is_retried(self):
        dst = Destination()
        with tempfile.TemporaryDirectory() as directory:
            journal = mirror.FailureJournal(directory + '/failures.json')
            journal.set(journal.key(SOURCE, 'demo', 'dest', '11', 'file'), True)
            with patch.object(mirror, 'mirror_git'):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', Source(), dst, 'token', journal=journal)
        self.assertEqual(dst.releases['11']['name'], 'current')
        self.assertNotIn('10', [tag for tag, _ in dst.patched])

    def test_old_pending_release_is_resumed_without_updating_other_old_releases(self):
        dst = Destination(['11'])
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', Source(), dst, 'token')
        self.assertEqual(dst.releases['11']['name'], 'current')
        self.assertNotIn('10', [tag for tag, _ in dst.patched])
