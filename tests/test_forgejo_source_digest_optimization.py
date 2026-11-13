import contextlib
import io
import unittest
from unittest.mock import patch

import mirror
from mirror_test_support import patch_everywhere
from repo_mirror.forgejo_source_adapter import ForgejoSourceAdapter
from test_published_digest import DigestDestination, DigestSource, digest
from test_reconcile_assets import SOURCE


class Listing(DigestSource):
    def __init__(self, metadata):
        super().__init__(metadata)
        self.listings = 0

    def pages(self, url):
        result = super().pages(url)
        if not url.endswith('/releases'):
            self.listings += 1
            result[0].update(id=4, browser_download_url='https://github.com/alice/demo/releases/download/v1/changed')
        return result


class Client:
    def __init__(self):
        self.downloads = []

    def _download_once(self, asset, out, **kwargs):
        self.downloads.append(asset['name'])
        out.write(b'new')


class ForgejoSourceDigestTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.listing = Listing(digest(b'new'))
        self.client = Client()
        self.src = ForgejoSourceAdapter('alice/demo', 'token', api=self.listing, client=self.client)
        self.dst = DigestDestination(None)

    def run_mirror(self, src=None):
        log = io.StringIO()
        with patch_everywhere('mirror_git'), contextlib.redirect_stderr(log):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', src or self.src, self.dst, 'token')
        return log.getvalue()

    def test_matching_authenticated_digest_hashes_destination_and_skips_source(self):
        log = self.run_mirror()
        self.assertEqual(self.client.downloads, [])
        self.assertGreaterEqual(len(self.dst.downloads), 1)
        self.assertGreaterEqual(self.listing.listings, 2)
        self.assertIn('skipped=1', log)
        self.assertEqual(self.dst.deleted, [])

    def test_tampering_after_initial_hash_is_not_finalized(self):
        self.dst.metadata = digest(b'new')
        download = self.dst.download
        def tamper(url, out):
            download(url, out)
            if len(self.dst.downloads) == 1:
                self.dst.assets['changed'] = b'old'
        self.dst.download = tamper
        with self.assertRaisesRegex(RuntimeError, 'destination asset digest mismatch'):
            self.run_mirror()
        self.assertEqual(self.dst.deleted, [])
        self.assertIn('[repo-mirror:pending:v3]', self.dst.release['body'])

    def test_same_size_tampering_downloads_and_replaces(self):
        self.dst.assets['changed'] = b'old'
        self.run_mirror()
        self.assertEqual(self.client.downloads, ['changed'])
        self.assertEqual(self.dst.assets['changed'], b'new')
        self.assertEqual(self.dst.deleted, ['changed'])

    def test_matching_destination_metadata_does_not_hide_tampering(self):
        self.dst.metadata = digest(b'new')
        self.dst.assets['changed'] = b'old'
        with self.assertRaisesRegex(RuntimeError, 'destination asset digest mismatch'):
            self.run_mirror()
        self.assertEqual(self.client.downloads, ['changed'])
        self.assertEqual(self.dst.deleted, [])

    def test_untrusted_adapter_cannot_use_new_fast_path(self):
        src = DigestSource(digest(b'new'))
        self.run_mirror(src)
        self.assertEqual(len(src.downloads), 1)

    def test_boolean_source_size_does_not_delete(self):
        pages = self.listing.pages
        def sized(url):
            assets = pages(url)
            if not url.endswith('/releases'):
                assets[0]['size'] = True
            return assets
        self.listing.pages = sized
        self.listing.metadata = digest(b'n')
        self.client._download_once = lambda asset, out, **kwargs: out.write(b'n')
        self.dst.assets['changed'] = b'o'
        with self.assertRaisesRegex(RuntimeError, 'source asset size mismatch'):
            self.run_mirror()
        self.assertEqual(self.dst.deleted, [])

    def test_malformed_digest_falls_back_to_download(self):
        self.listing.metadata = 'sha256:bad'
        self.run_mirror()
        self.assertEqual(self.client.downloads, ['changed'])

    def test_final_source_snapshot_still_checked(self):
        original = self.listing.pages
        def changing(url):
            if not url.endswith('/releases') and self.listing.listings:
                self.listing.metadata = digest(b'old')
            return original(url)
        self.listing.pages = changing
        with self.assertRaises(mirror.ReleaseRecoveryError):
            self.run_mirror()
        self.assertEqual(self.dst.deleted, [])
