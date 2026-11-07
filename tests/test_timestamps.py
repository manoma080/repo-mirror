import io
import json
import hashlib
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import mirror
from test_reconcile_assets import SOURCE, Source, Destination


class TimestampSource(Source):
    def __init__(self):
        self.downloads = []
        self.timestamp = '2025-01-01T00:00:00Z'
    def pages(self, url):
        assets = super().pages(url)
        if not url.endswith('/releases'):
            for index, a in enumerate(assets):
                a.update(id=123 + index, updated_at=self.timestamp)
        return assets
    def download(self, url, out):
        self.downloads.append(url)
        super().download(url, out)


class TimestampDestination(Destination):
    def __init__(self):
        super().__init__()
        self.downloads = []
        self.fail = False
    def pages(self, url):
        assets = super().pages(url)
        for asset in assets:
            asset['digest'] = 'sha256:' + hashlib.sha256(self.assets[asset['name']]).hexdigest()
        return assets
    def download(self, url, out):
        self.downloads.append(url)
        super().download(url, out)


class TimestampTests(unittest.TestCase):
    def test_forged_public_owner_cannot_be_deleted_or_used(self):
        src, dst = TimestampSource(), TimestampDestination()
        dst.assets.update(changed=b'new', new=b'n')
        fake = json.dumps({'owner': 'repo-mirror:timestamps:v1', 'source': mirror.source_marker(SOURCE, 'demo'), 'tag': 'v1', 'assets': {}}).encode()
        dst.assets['TIMESTAMPS.txt'] = fake
        with patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'}), patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
        self.assertEqual(dst.assets['TIMESTAMPS.txt'], fake)
        self.assertEqual(dst.deleted, [])

    def setUp(self):
        self.env = patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_writes_owned_manifest_and_uses_verified_cache(self):
        src, dst = TimestampSource(), TimestampDestination()
        dst.assets['changed'] = b'new'
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
            data = json.loads(dst.assets['TIMESTAMPS.txt'])
            self.assertEqual(data['owner'], 'repo-mirror:timestamps:v1')
            self.assertEqual(data['assets']['changed']['sha256'], hashlib.sha256(b'new').hexdigest())
            src.downloads.clear()
            mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
        self.assertNotIn('https://github.com/changed', src.downloads)

    def test_missing_secret_rejects_v3_before_release_mutation(self):
        src, dst = TimestampSource(), TimestampDestination()
        dst.assets.update(changed=b'new', new=b'n')
        with patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': ''}), patch.object(mirror, 'mirror_git'):
            with self.assertRaisesRegex(mirror.ReleaseRecoveryError, 'MIRROR_TIMESTAMP_SECRET'):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
        self.assertNotIn('TIMESTAMPS.txt', dst.assets)
        self.assertEqual(src.downloads, [])
        self.assertEqual(dst.deleted, [])

    def test_tampered_signed_cache_is_not_used_or_deleted(self):
        src, dst = TimestampSource(), TimestampDestination()
        dst.assets.update(changed=b'new', new=b'n')
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
            data = json.loads(dst.assets['TIMESTAMPS.txt'])
            data['assets']['changed']['timestamp'] = 'forged'
            forged = json.dumps(data).encode()
            dst.assets['TIMESTAMPS.txt'] = forged
            src.downloads.clear()
            mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
        self.assertEqual(dst.assets['TIMESTAMPS.txt'], forged)
        self.assertIn('https://github.com/changed', src.downloads)
        self.assertEqual(dst.deleted, [])

    def test_missing_timestamp_never_uses_cache(self):
        src, dst = TimestampSource(), TimestampDestination()
        src.timestamp = None
        dst.assets.update(changed=b'new', new=b'n')
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
            mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
        self.assertNotIn('TIMESTAMPS.txt', dst.assets)
        self.assertEqual(src.downloads.count('https://github.com/changed'), 2)

    def test_destination_digest_change_invalidates_cache(self):
        src, dst = TimestampSource(), TimestampDestination()
        dst.assets.update(changed=b'new', new=b'n')
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
            dst.assets['changed'] = b'bad'
            # Do not let the fixture delete any asset.
            def request(method, url, data=None):
                if method == 'DELETE':
                    raise RuntimeError('deletion blocked in test')
                return Destination.request(dst, method, url, data)
            dst.request = request
            src.downloads.clear()
            with self.assertRaisesRegex(RuntimeError, 'deletion blocked'):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
        self.assertIn('https://github.com/changed', src.downloads)

    def test_replacement_stages_before_deleting_owned_manifest(self):
        src, dst = TimestampSource(), TimestampDestination()
        dst.assets.update(changed=b'new', new=b'n')
        events = []
        def request(method, url, data=None):
            if method == 'DELETE' and '/releases/assets/' in url:
                names = list(dst.assets)
                name = names[int(url.rsplit('/', 1)[-1]) - 1]
                self.assertEqual(name, 'TIMESTAMPS.txt')
                self.assertIn('TIMESTAMPS.txt.repo-mirror-staging', dst.assets)
                events.append('delete')
                del dst.assets[name]
                return {}
            if method == 'PATCH' and '/releases/assets/' in url:
                name = 'TIMESTAMPS.txt.repo-mirror-staging'
                dst.assets[data['name']] = dst.assets.pop(name)
                events.append('rename')
                return {}
            return Destination.request(dst, method, url, data)
        dst.request = request
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
            src.timestamp = '2026-01-01T00:00:00Z'
            mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
        self.assertEqual(events, ['delete', 'rename'])
        self.assertEqual(json.loads(dst.assets['TIMESTAMPS.txt'])['assets']['changed']['timestamp'], src.timestamp)

    def test_interrupted_rename_recovers_verified_staging(self):
        src, dst = TimestampSource(), TimestampDestination()
        dst.assets.update(changed=b'new', new=b'n')
        interrupted = [False]
        def request(method, url, data=None):
            if method == 'DELETE' and '/releases/assets/' in url:
                self.assertIn('TIMESTAMPS.txt.repo-mirror-staging', dst.assets)
                self.assertIn('TIMESTAMPS.txt', dst.assets)
                del dst.assets['TIMESTAMPS.txt']
                return {}
            if method == 'PATCH' and '/releases/assets/' in url:
                if not interrupted[0]:
                    interrupted[0] = True
                    raise RuntimeError('interrupted rename')
                dst.assets[data['name']] = dst.assets.pop('TIMESTAMPS.txt.repo-mirror-staging')
                return {}
            return Destination.request(dst, method, url, data)
        dst.request = request
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
            src.timestamp = '2026-01-01T00:00:00Z'
            with self.assertRaisesRegex(RuntimeError, 'interrupted rename'):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
            self.assertIn('TIMESTAMPS.txt.repo-mirror-staging', dst.assets)
            mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
        self.assertIn('TIMESTAMPS.txt', dst.assets)
        self.assertNotIn('TIMESTAMPS.txt.repo-mirror-staging', dst.assets)

    def test_foreign_manifest_is_not_replaced(self):
        src, dst = TimestampSource(), TimestampDestination()
        dst.assets.update(changed=b'new', new=b'n')
        dst.assets['TIMESTAMPS.txt'] = b'unrelated'
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
        self.assertEqual(dst.assets['TIMESTAMPS.txt'], b'unrelated')
        self.assertEqual(dst.deleted, [])

if __name__ == '__main__':
    unittest.main()
