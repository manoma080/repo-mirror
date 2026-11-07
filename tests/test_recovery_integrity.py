import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import mirror
from test_reconcile_assets import SOURCE, Source, Destination


class RecoveryIntegrityTests(unittest.TestCase):
    def setUp(self):
        secret = patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'})
        secret.start()
        self.addCleanup(secret.stop)

    def test_interrupted_create_recovers_provisional_marker(self):
        class EmptySource(Source):
            def pages(self, url):
                return super().pages(url) if url.endswith('/releases') else []

        class CreateDestination(Destination):
            def __init__(self):
                super().__init__()
                self.release = None
                self.assets = {}
                self.fail_binding = True
                self.duplicate = False
            def request(self, method, url, data=None):
                if '/releases/tags/' in url:
                    if self.release is None:
                        raise HTTPError(url, 404, '', {}, io.BytesIO())
                    return self.release.copy()
                if method == 'POST' and url.endswith('/releases'):
                    self.release = dict(data, id=2, upload_url='https://uploads.github.com/x{?name}')
                    return self.release.copy()
                if method == 'PATCH' and url.endswith('/releases/2') and self.fail_binding:
                    self.fail_binding = False
                    raise RuntimeError('binding interrupted')
                return super().request(method, url, data)
            def pages(self, url):
                if url.endswith('/releases'):
                    return ([self.release.copy()] * (2 if self.duplicate else 1)) if self.release else []
                return super().pages(url)

        dst = CreateDestination()
        with patch.object(mirror, 'mirror_git'):
            with self.assertRaisesRegex(RuntimeError, 'binding interrupted'):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', EmptySource(), dst, 'token')
            self.assertIn('WAITING_SOURCE', dst.release['body'])
            original = dst.release['body']
            dst.duplicate = True
            with self.assertRaises(mirror.ReleaseRecoveryError):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', EmptySource(), dst, 'token')
            self.assertEqual(dst.release['body'], original)
            dst.duplicate = False
            dst.assets['changed'] = b'old'
            with self.assertRaises(mirror.ReleaseRecoveryError):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', Source(), dst, 'token')
            self.assertEqual(dst.assets['changed'], b'old')
            self.assertEqual(dst.deleted, [])
            dst.release['body'] = original
            dst.assets = {}
            mirror.mirror_repository(SOURCE, 'demo', 'dest', EmptySource(), dst, 'token')
        self.assertEqual(dst.release['body'], 'notes')
        self.assertEqual(dst.deleted, [])

    def test_same_size_corrupt_new_upload_does_not_finalize(self):
        class OneSource(Source):
            def pages(self, url):
                return super().pages(url) if url.endswith('/releases') else [{'name': 'new', 'size': 1, 'browser_download_url': 'https://github.com/new'}]
        class CorruptDestination(Destination):
            def upload(self, url, data, content_type):
                self.assets['new'] = b'x'
        dst = CorruptDestination()
        dst.assets = {}
        with patch.object(mirror, 'mirror_git'):
            with self.assertRaises(RuntimeError):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', OneSource(), dst, 'token')
        self.assertIn('[repo-mirror:pending:v3]', dst.release['body'])
