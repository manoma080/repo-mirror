import hashlib
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
import mirror
from test_reconcile_assets import SOURCE, Source, Destination


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


class DigestSource(Source):
    def __init__(self, metadata):
        self.metadata = metadata
        self.downloads = []
    def pages(self, url):
        result = super().pages(url)
        if not url.endswith('/releases'):
            result = [result[0]]
            result[0]['digest'] = self.metadata
        return result
    def download(self, url, out):
        self.downloads.append(url)
        super().download(url, out)


class DigestDestination(Destination):
    def __init__(self, metadata, content=b'new'):
        super().__init__()
        self.assets = {'changed': content}
        self.metadata = metadata
        self.downloads = []
        self.fail = False
    def pages(self, url):
        result = super().pages(url)
        for asset in result:
            asset['digest'] = self.metadata
        return result
    def download(self, url, out):
        self.downloads.append(url)
        super().download(url, out)


class PublishedDigestTests(unittest.TestCase):
    def run_mirror(self, src, dst):
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')

    def test_different_valid_digests_replace_after_verification(self):
        src = DigestSource(digest(b'new'))
        dst = DigestDestination(digest(b'old'), b'old')
        self.run_mirror(src, dst)
        self.assertEqual(dst.assets['changed'], b'new')
        self.assertEqual(dst.deleted, ['changed'])
        self.assertEqual(len(src.downloads), 1)
        self.assertEqual(len(dst.downloads), 1)

    def test_missing_or_malformed_digests_fall_back_to_bytes(self):
        for source_hash, target_hash in [(None, digest(b'new')), (digest(b'new'), None),
                                         ('sha256:bad', 'sha256:bad'), ('md5:' + 'a'*32, None)]:
            with self.subTest(source=source_hash, target=target_hash):
                src = DigestSource(source_hash)
                dst = DigestDestination(target_hash)
                self.run_mirror(src, dst)
                self.assertEqual(len(src.downloads), 1)
                self.assertEqual(len(dst.downloads), 1)
                self.assertEqual(dst.deleted, [])

    def test_same_size_without_digests_does_not_imply_equal(self):
        src = DigestSource(None)
        dst = DigestDestination(None, b'old')
        self.run_mirror(src, dst)
        self.assertEqual(dst.assets['changed'], b'new')
        self.assertEqual(dst.deleted, ['changed'])

    def test_incorrect_destination_digest_never_deletes(self):
        src = DigestSource(digest(b'new'))
        dst = DigestDestination(digest(b'bad'), b'old')
        with self.assertRaisesRegex(RuntimeError, 'destination asset digest mismatch'):
            self.run_mirror(src, dst)
        self.assertEqual(dst.deleted, [])
        self.assertEqual(dst.assets['changed'], b'old')

    def test_incorrect_source_digest_never_deletes_or_uploads(self):
        src = DigestSource(digest(b'bad'))
        dst = DigestDestination(digest(b'old'), b'old')
        with self.assertRaisesRegex(RuntimeError, 'source asset digest mismatch'):
            self.run_mirror(src, dst)
        self.assertEqual(dst.deleted, [])
        self.assertEqual(dst.assets['changed'], b'old')

    def test_matching_published_digests_skip_downloads(self):
        src = DigestSource(digest(b'new'))
        dst = DigestDestination(digest(b'new'))
        self.run_mirror(src, dst)
        self.assertEqual(src.downloads, [])
        self.assertEqual(dst.downloads, [])
        self.assertEqual(dst.deleted, [])


    def test_matching_digests_require_valid_equal_sizes(self):
        for source_size, target_size in [(3, 4), (3, None), (3, True), (3, -1),
                                         (True, True), (-1, -1), (None, None)]:
            with self.subTest(source_size=source_size, target_size=target_size):
                class SizedSource(DigestSource):
                    def pages(self, url):
                        assets = super().pages(url)
                        if not url.endswith('/releases'):
                            assets[0]['size'] = source_size
                        return assets
                class SizedDestination(DigestDestination):
                    def pages(self, url):
                        assets = super().pages(url)
                        assets[0]['size'] = target_size
                        return assets
                src, dst = SizedSource(digest(b'new')), SizedDestination(digest(b'new'))
                try:
                    self.run_mirror(src, dst)
                except (RuntimeError, ValueError):
                    pass
                self.assertEqual(len(src.downloads), 1)
                self.assertEqual(dst.deleted, [])


    def test_digest_only_change_stops_before_delete_or_finalize(self):
        for target in (digest(b'old'), digest(b'new')):
            with self.subTest(target=target):
                class ChangingSource(DigestSource):
                    def __init__(self):
                        super().__init__(digest(b'new'))
                        self.count = 0
                    def pages(self, url):
                        if not url.endswith('/releases'):
                            self.count += 1
                            if self.count > 1:
                                self.metadata = digest(b'zzz')
                        return super().pages(url)
                src = ChangingSource()
                dst = DigestDestination(target, b'old' if target == digest(b'old') else b'new')
                with self.assertRaises(mirror.ReleaseRecoveryError):
                    self.run_mirror(src, dst)
                self.assertEqual(dst.deleted, [])
                self.assertTrue(dst.release['name'].startswith('[repo-mirror:pending:'))


    def test_journal_recovery_clears_after_fast_path_finalization(self):
        src, dst = DigestSource(digest(b'new')), DigestDestination(digest(b'new'))
        with tempfile.TemporaryDirectory() as directory:
            journal = mirror.FailureJournal(Path(directory) / 'journal.json')
            key = journal.key(SOURCE, 'demo', 'dest', 'v1', 'changed')
            journal.set(key, True)
            with patch.object(mirror, 'mirror_git'):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token', journal=journal)
            self.assertNotIn(key, mirror.FailureJournal(journal.path).failures)
            self.assertEqual(dst.deleted, [])
            self.assertEqual(src.downloads, [])

    def test_initial_pending_recovery_rejects_digest_only_mutation(self):
        src, dst = DigestSource(digest(b'new')), DigestDestination(None, b'new')
        dst.assets = {}
        dst.release = None
        class InitialDestination(DigestDestination):
            def request(self, method, url, data=None):
                if '/releases/tags/' in url:
                    from urllib.error import HTTPError
                    import io
                    if self.release is None:
                        raise HTTPError(url, 404, 'not found', {}, io.BytesIO())
                if method == 'POST' and url.endswith('/releases'):
                    self.release = {'id': 2, 'name': data['name'], 'upload_url': 'https://uploads.github.com/x{?name}'}
                    return self.release.copy()
                return super().request(method, url, data)
            def pages(self, url):
                if url.endswith('/releases'):
                    return []
                return super().pages(url)
        dst = InitialDestination(None)
        dst.assets = {}
        dst.release = None
        dst.fail = True
        with self.assertRaises(RuntimeError):
            self.run_mirror(src, dst)
        self.assertTrue(dst.release['name'].startswith('[repo-mirror:pending:v1]'))
        src.metadata = digest(b'zzz')
        dst.fail = False
        with self.assertRaises(mirror.ReleaseRecoveryError):
            self.run_mirror(src, dst)
        self.assertEqual(dst.deleted, [])
        self.assertEqual(dst.assets, {})
