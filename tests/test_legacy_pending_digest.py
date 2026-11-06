import base64
import hashlib
import json
import unittest
from unittest.mock import patch

import mirror
from test_published_digest import DigestSource, DigestDestination, digest
from test_reconcile_assets import SOURCE


def legacy_marker(version, asset):
    fields = ('name', 'size', 'id', 'updated_at', 'browser_download_url')
    manifest = [[asset.get(k) for k in fields]]
    fingerprint = hashlib.sha256(json.dumps(manifest, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
    return '[repo-mirror:pending:' + version + ']' + base64.urlsafe_b64encode(json.dumps(['old', fingerprint]).encode()).decode()


class LegacyPendingTests(unittest.TestCase):
    def run_mirror(self, src, dst):
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')

    def test_digest_marker_before_checksum_content_hash_requires_byte_verification(self):
        content = (hashlib.sha256(b'new').hexdigest() + '  changed\n').encode()
        class Source(DigestSource):
            def pages(self, url):
                assets = super().pages(url)
                if not url.endswith('/releases'):
                    assets.append({'name': 'SHA256SUMS', 'size': len(content),
                                   'browser_download_url': 'https://github.com/sums'})
                return assets
            def download(self, url, out):
                if url.endswith('/sums'):
                    self.downloads.append(url)
                    out.write(content)
                else:
                    super().download(url, out)
        class Destination(DigestDestination):
            def pages(self, url):
                assets = super().pages(url)
                for asset in assets:
                    asset['digest'] = digest(self.assets[asset['name']])
                return assets
        for version in ('v1', 'v2'):
            for matching in (True, False):
                with self.subTest(version=version, matching=matching):
                    src, dst = Source(digest(b'new')), Destination(digest(b'new'))
                    dst.assets['SHA256SUMS'] = content if matching else b'x' * len(content)
                    entries = []
                    for asset in src.pages('assets'):
                        entry = [asset.get(k) for k in ('name', 'size', 'id', 'updated_at', 'browser_download_url')]
                        if asset.get('digest') is not None:
                            entry.append(asset['digest'].lower())
                        entries.append(entry)
                    entries.sort(key=lambda a: str(a[0]))
                    fingerprint = hashlib.sha256(json.dumps(entries, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
                    marker = '[repo-mirror:pending:' + version + ']' + base64.urlsafe_b64encode(json.dumps(['old', fingerprint]).encode()).decode()
                    dst.release['name'] = marker
                    if matching:
                        self.run_mirror(src, dst)
                        self.assertEqual(dst.release['name'], 'new')
                        self.assertIn('https://api.github.com/assets/SHA256SUMS', dst.downloads)
                    else:
                        with self.assertRaises(mirror.ReleaseRecoveryError):
                            self.run_mirror(src, dst)
                        self.assertEqual(dst.release['name'], marker)
                    self.assertEqual(dst.deleted, [])

    def test_legacy_v1_v2_resume_and_upgrade_before_finalize(self):
        for version in ('v1', 'v2'):
            with self.subTest(version=version):
                src = DigestSource(digest(b'new'))
                dst = DigestDestination(digest(b'new'))
                dst.release['name'] = legacy_marker(version, src.pages('assets')[0])
                patches = []
                original = dst.request
                def request(method, url, data=None):
                    if method == 'PATCH' and url.endswith('/releases/2'):
                        patches.append(data.copy())
                    return original(method, url, data)
                dst.request = request
                self.run_mirror(src, dst)
                self.assertEqual(len(patches), 2)
                self.assertTrue(patches[0]['name'].startswith('[repo-mirror:pending:' + version + ']'))
                self.assertNotEqual(patches[0]['name'], legacy_marker(version, src.pages('assets')[0]))
                self.assertEqual(dst.release['name'], 'new')

    def test_prior_digest_bearing_marker_with_created_at_migrates_safely(self):
        class CreatedSource(DigestSource):
            def pages(self, url):
                assets = super().pages(url)
                if not url.endswith('/releases'):
                    for asset in assets:
                        asset['created_at'] = '2024-01-01T00:00:00Z'
                return assets
        for version in ('v1', 'v2'):
            with self.subTest(version=version):
                src = CreatedSource(digest(b'new'))
                dst = DigestDestination(digest(b'new'))
                asset = src.pages('assets')[0]
                entry = [asset.get(k) for k in ('name', 'size', 'id', 'updated_at', 'browser_download_url')]
                entry.append(asset['digest'].lower())
                fingerprint = hashlib.sha256(json.dumps([entry], ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
                dst.release['name'] = '[repo-mirror:pending:' + version + ']' + base64.urlsafe_b64encode(json.dumps(['old', fingerprint]).encode()).decode()
                self.run_mirror(src, dst)
                self.assertEqual(dst.release['name'], 'new')
                self.assertEqual(dst.deleted, [])

    def test_prior_digest_marker_with_changed_destination_refuses_upgrade(self):
        class CreatedSource(DigestSource):
            def pages(self, url):
                assets = super().pages(url)
                if not url.endswith('/releases'):
                    for asset in assets:
                        asset['created_at'] = '2024-01-01T00:00:00Z'
                return assets
        src, dst = CreatedSource(digest(b'new')), DigestDestination(None)
        dst.assets = {'changed': b'old'}
        asset = src.pages('assets')[0]
        entry = [asset.get(k) for k in ('name', 'size', 'id', 'updated_at', 'browser_download_url')]
        entry.append(asset['digest'].lower())
        fingerprint = hashlib.sha256(json.dumps([entry], ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
        marker = '[repo-mirror:pending:v2]' + base64.urlsafe_b64encode(json.dumps(['old', fingerprint]).encode()).decode()
        dst.release['name'] = marker
        with self.assertRaises(mirror.ReleaseRecoveryError):
            self.run_mirror(src, dst)
        self.assertEqual(dst.release['name'], marker)
        self.assertEqual(dst.deleted, [])

    def test_changed_legacy_fields_are_never_upgraded(self):
        src = DigestSource(digest(b'new'))
        dst = DigestDestination(digest(b'new'))
        asset = src.pages('assets')[0]
        asset['id'] = 99
        marker = legacy_marker('v2', asset)
        dst.release['name'] = marker
        with self.assertRaises(mirror.ReleaseRecoveryError):
            self.run_mirror(src, dst)
        self.assertEqual(dst.release['name'], marker)
        self.assertEqual(dst.deleted, [])

    def test_missing_or_different_destination_requires_manual_recovery(self):
        for content in (None, b'old'):
            with self.subTest(content=content):
                src = DigestSource(digest(b'new'))
                dst = DigestDestination(None)
                dst.assets = {} if content is None else {'changed': content}
                marker = legacy_marker('v2', src.pages('assets')[0])
                dst.release['name'] = marker
                with self.assertRaises(mirror.ReleaseRecoveryError):
                    self.run_mirror(src, dst)
                self.assertEqual(dst.release['name'], marker)
                self.assertEqual(dst.deleted, [])

    def test_digest_change_before_upgrade_preserves_original_marker(self):
        class ChangingSource(DigestSource):
            def download(self, url, out):
                super().download(url, out)
                self.metadata = digest(b'zzz')
        src = ChangingSource(digest(b'new'))
        dst = DigestDestination(digest(b'new'))
        marker = legacy_marker('v1', src.pages('assets')[0])
        dst.release['name'] = marker
        with self.assertRaises(mirror.ReleaseRecoveryError):
            self.run_mirror(src, dst)
        self.assertEqual(dst.release['name'], marker)
        self.assertEqual(dst.deleted, [])

    def test_digest_change_during_upgrade_blocks_finalization(self):
        src = DigestSource(digest(b'new'))
        dst = DigestDestination(digest(b'new'))
        dst.release['name'] = legacy_marker('v2', src.pages('assets')[0])
        original = dst.request
        def request(method, url, data=None):
            result = original(method, url, data)
            if method == 'PATCH' and url.endswith('/releases/2'):
                src.metadata = digest(b'zzz')
            return result
        dst.request = request
        with self.assertRaises(mirror.ReleaseRecoveryError):
            self.run_mirror(src, dst)
        self.assertTrue(dst.release['name'].startswith('[repo-mirror:pending:v2]'))
        self.assertEqual(dst.deleted, [])
