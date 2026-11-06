import hashlib
import unittest
from unittest.mock import patch
import mirror
from test_published_digest import DigestSource, DigestDestination, digest
from test_reconcile_assets import SOURCE


class ChecksumFallbackTests(unittest.TestCase):
    def test_checksum_manifest_skips_matching_asset(self):
        content = (hashlib.sha256(b'new').hexdigest() + '  changed\n').encode()
        class ChecksumSource(DigestSource):
            def pages(self, url):
                assets = super().pages(url)
                if not url.endswith('/releases'):
                    assets.append({'name': 'SHA256SUMS.txt', 'size': len(content),
                                   'browser_download_url': 'https://github.com/sums'})
                return assets
            def download(self, url, out):
                if url.endswith('/sums'):
                    self.downloads.append(url)
                    out.write(content)
                else:
                    super().download(url, out)
        src = ChecksumSource(None)
        class Destination(DigestDestination):
            def pages(self, url):
                assets = super().pages(url)
                for asset in assets:
                    asset['digest'] = digest(self.assets[asset['name']])
                return assets
        dst = Destination(digest(b'new'))
        dst.assets['SHA256SUMS.txt'] = content
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
        self.assertNotIn('https://github.com/changed', src.downloads)
        self.assertNotIn('https://api.github.com/assets/changed', dst.downloads)

    def test_malformed_or_ambiguous_manifest_falls_back_to_bytes(self):
        h = hashlib.sha256(b'new').hexdigest()
        cases = [([('SHA256SUMS', (h + '  changed\ninvalid\n').encode())]),
                 ([('SHA256SUMS', (h + '  changed\n' + h + '  changed\n').encode())]),
                 ([('SHA256SUMS', (h + '  ../changed\n').encode())]),
                 ([('SHA256SUMS', b'\xff')]),
                 ([('SHA256SUMS', (h + '  changed\n').encode()),
                   ('SHA256SUMS.txt', (h + '  changed\n').encode())])]
        for files in cases:
            with self.subTest(files=files):
                class Source(DigestSource):
                    def pages(self, url):
                        assets = super().pages(url)
                        if not url.endswith('/releases'):
                            assets.extend({'name': n, 'size': len(c), 'browser_download_url': 'https://github.com/' + n} for n, c in files)
                        return assets
                    def download(self, url, out):
                        for n, c in files:
                            if url == 'https://github.com/' + n:
                                self.downloads.append(url)
                                out.write(c)
                                return
                        super().download(url, out)
                class Destination(DigestDestination):
                    def pages(self, url):
                        assets = super().pages(url)
                        for asset in assets:
                            asset['digest'] = digest(self.assets[asset['name']])
                        return assets
                src, dst = Source(None), Destination(digest(b'new'))
                dst.assets.update(dict(files))
                with patch.object(mirror, 'mirror_git'):
                    mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
                self.assertIn('https://github.com/changed', src.downloads)
                self.assertEqual(dst.deleted, [])

    def test_checksum_content_change_preserves_pending_marker(self):
        h = hashlib.sha256(b'new').hexdigest()
        contents = [(h + '  changed\n').encode(), ('0' * 64 + '  changed\n').encode()]
        class Source(DigestSource):
            def __init__(self):
                super().__init__(None)
                self.count = 0
            def pages(self, url):
                assets = super().pages(url)
                if not url.endswith('/releases'):
                    assets.append({'name': 'SHA256SUMS', 'size': len(contents[0]), 'browser_download_url': 'https://github.com/sums'})
                return assets
            def download(self, url, out):
                if url.endswith('/sums'):
                    out.write(contents[min(self.count, 1)])
                    self.count += 1
                else:
                    super().download(url, out)
        class Destination(DigestDestination):
            def pages(self, url):
                assets = super().pages(url)
                for asset in assets:
                    asset['digest'] = digest(self.assets[asset['name']])
                return assets
        src, dst = Source(), Destination(digest(b'new'))
        dst.assets['SHA256SUMS'] = contents[0]
        with patch.object(mirror, 'mirror_git'):
            with self.assertRaises(mirror.ReleaseRecoveryError):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
        self.assertTrue(dst.release['name'].startswith('[repo-mirror:pending:'))
        self.assertEqual(dst.deleted, [])
