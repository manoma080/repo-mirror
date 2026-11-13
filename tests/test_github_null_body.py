import unittest
from unittest.mock import patch

import mirror
from mirror_test_support import patch_everywhere
from test_reconcile_assets import SOURCE, Destination, Source


class BodySource(Source):
    def __init__(self, body):
        self.body = body

    def pages(self, url):
        result = super().pages(url)
        if url.endswith('/releases'):
            result[0]['body'] = self.body
            result[0]['draft'] = False
        return result


class GithubDestination(Destination):
    """Model GitHub PATCH: null leaves body unchanged, empty string clears it."""
    def __init__(self):
        super().__init__()
        self.patches = []
        self.release.update(tag_name='v1', draft=False)

    def request(self, method, url, data=None):
        if method == 'GET' and url.endswith('/releases/2'):
            return self.release.copy()
        if method == 'PATCH' and url.endswith('/releases/2'):
            self.patches.append(dict(data))
            if data.get('body', object()) is None:
                data = {k: v for k, v in data.items() if k != 'body'}
        return super().request(method, url, data)


class GithubNullBodyTests(unittest.TestCase):
    def setUp(self):
        p = patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'})
        p.start()
        self.addCleanup(p.stop)

    def sync(self, src, dst):
        with patch_everywhere('mirror_git'):
            return mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')

    def test_null_source_removes_signed_bound_marker_after_interruption(self):
        src, dst = BodySource(None), GithubDestination()
        with self.assertRaises(RuntimeError):
            self.sync(src, dst)
        self.assertIn('"phase":"BOUND"', dst.release['body'])
        self.assertIn('"signature":', dst.release['body'])
        dst.fail = False
        self.sync(src, dst)
        self.assertEqual(dst.release['body'], '')
        self.assertEqual(dst.patches[-1]['body'], '')

    def test_ignored_empty_final_patch_fails_readback(self):
        class IgnoreEmpty(GithubDestination):
            def request(self, method, url, data=None):
                if method == 'PATCH' and data is not None and data.get('body') == '':
                    data = {k: v for k, v in data.items() if k != 'body'}
                return super().request(method, url, data)
        dst = IgnoreEmpty()
        dst.fail = False
        with self.assertRaises(mirror.ReleaseRecoveryError):
            self.sync(BodySource(None), dst)
        self.assertIn('"phase":"BOUND"', dst.release['body'])

    def test_omitted_draft_ignored_clear_fails_readback(self):
        class OmittedDraft(BodySource):
            def pages(self, url):
                result = super().pages(url)
                if url.endswith('/releases'):
                    result[0].pop('draft')
                return result

        class IgnoreEmpty(GithubDestination):
            def request(self, method, url, data=None):
                if method == 'PATCH' and data is not None and data.get('body') == '':
                    data = {k: v for k, v in data.items() if k != 'body'}
                return super().request(method, url, data)

        dst = IgnoreEmpty()
        dst.fail = False
        with self.assertRaises(mirror.ReleaseRecoveryError):
            self.sync(OmittedDraft(None), dst)
        self.assertIn('"phase":"BOUND"', dst.release['body'])
        self.assertIn('"signature":', dst.release['body'])

    def test_null_body_rejects_malformed_source_draft(self):
        class InvalidDraft(BodySource):
            def pages(self, url):
                result = super().pages(url)
                if url.endswith('/releases'):
                    result[0]['draft'] = 'false'
                return result

        dst = GithubDestination()
        dst.fail = False
        with self.assertRaisesRegex(ValueError, 'invalid source release flags'):
            self.sync(InvalidDraft(None), dst)
        self.assertEqual(dst.patches, [])

    def test_readback_rejects_replaced_identity(self):
        for field, value in (('id', 3), ('tag_name', 'other'), ('draft', True)):
            with self.subTest(field=field):
                class Replaced(GithubDestination):
                    def request(self, method, url, data=None):
                        result = super().request(method, url, data)
                        if method == 'GET' and url.endswith('/releases/2'):
                            result[field] = value
                        return result
                dst = Replaced()
                dst.fail = False
                with self.assertRaises(mirror.ReleaseRecoveryError):
                    self.sync(BodySource(None), dst)

    def test_empty_and_single_space_are_preserved(self):
        for body in ('', ' '):
            with self.subTest(body=body):
                src, dst = BodySource(body), GithubDestination()
                dst.fail = False
                self.sync(src, dst)
                self.assertEqual(dst.release['body'], body)
                self.assertEqual(dst.patches[-1]['body'], body)

    def test_raw_null_to_empty_source_drift_aborts_finalization(self):
        src, dst = BodySource(None), GithubDestination()
        dst.fail = False
        upload = dst.upload
        def drift(*args):
            result = upload(*args)
            src.body = ''
            return result
        dst.upload = drift
        with self.assertRaisesRegex(ValueError, 'source release changed'):
            self.sync(src, dst)
        self.assertIn('"phase":"BOUND"', dst.release['body'])

    def test_interrupted_final_patch_is_retryable(self):
        class InterruptedDestination(GithubDestination):
            interrupt = True
            def request(self, method, url, data=None):
                if (method == 'PATCH' and data is not None and data.get('body') == ''
                        and self.interrupt):
                    self.interrupt = False
                    raise RuntimeError('interrupted before final patch')
                return super().request(method, url, data)
        src, dst = BodySource(None), InterruptedDestination()
        dst.fail = False
        with self.assertRaisesRegex(RuntimeError, 'interrupted before final patch'):
            self.sync(src, dst)
        self.assertIn('"phase":"BOUND"', dst.release['body'])
        self.sync(src, dst)
        self.assertEqual(dst.release['body'], '')
