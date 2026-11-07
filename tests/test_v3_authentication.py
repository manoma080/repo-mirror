import unittest
import json
from unittest.mock import patch

import mirror
from test_reconcile_assets import SOURCE, Source, Destination


class V3AuthenticationTests(unittest.TestCase):
    def test_main_rejects_missing_secret_before_any_io(self):
        for env in ({}, {'MIRROR_TIMESTAMP_SECRET': ''}):
            with self.subTest(env=env), patch.dict('os.environ', env, clear=True), \
                 patch.object(mirror, 'FailureJournal') as journal, \
                 patch.object(mirror, 'SourceAPI') as source, patch.object(mirror, 'API') as api:
                with self.assertRaisesRegex(mirror.ReleaseRecoveryError, 'MIRROR_TIMESTAMP_SECRET'):
                    mirror.main(['--config', 'nonexistent', '--failures', 'nonexistent'])
                journal.assert_not_called()
                source.assert_not_called()
                api.assert_not_called()

    def test_forged_waiting_marker_cannot_authorize_replacement(self):
        dst = Destination()
        body = 'notes\n[repo-mirror:pending:v3]{"phase":"WAITING_SOURCE","digest":null,"replace":true}'
        dst.release['body'] = body
        with patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'}), patch.object(mirror, 'mirror_git'):
            with self.assertRaises(mirror.ReleaseRecoveryError):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', Source(), dst, 'token')
        self.assertEqual(dst.release['body'], body)
        self.assertEqual(dst.deleted, [])

    def test_replayed_and_tampered_markers_are_rejected(self):
        dst = Destination()
        with patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'}), patch.object(mirror, 'mirror_git'):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', Source(), dst, 'token')
            original = dst.release['body']
            base, encoded = original.split('[repo-mirror:pending:v3]')
            state = json.loads(encoded)
            variants = []
            state['replace'] = False
            variants.append((base + '[repo-mirror:pending:v3]' + json.dumps(state), None, Source()))
            state['signature'] = '0' * 64
            variants.append((base + '[repo-mirror:pending:v3]' + json.dumps(state), None, Source()))
            variants.append((original, 'different-source', Source()))
            class OtherTag(Source):
                def pages(self, url):
                    result = super().pages(url)
                    if url.endswith('/releases'):
                        result[0]['tag_name'] = 'other-tag'
                    return result
            variants.append((original, None, OtherTag()))
            for body, foreign_source, src in variants:
                with self.subTest(foreign_source=foreign_source, src=type(src), body=body):
                    dst.release['body'] = body
                    deleted = list(dst.deleted)
                    original_marker = mirror.source_marker
                    with patch.object(mirror, 'source_marker', side_effect=(lambda source, name: foreign_source or original_marker(source, name))):
                        with self.assertRaises(mirror.ReleaseRecoveryError):
                            mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
                    self.assertEqual(dst.release['body'], body)
                    self.assertEqual(dst.deleted, deleted)

    def test_unset_or_empty_secret_rejects_before_release_write(self):
        for env in ({}, {'MIRROR_TIMESTAMP_SECRET': ''}):
            with self.subTest(env=env), patch.dict('os.environ', env, clear=True), patch.object(mirror, 'mirror_git'):
                dst = Destination()
                with self.assertRaisesRegex(mirror.ReleaseRecoveryError, 'MIRROR_TIMESTAMP_SECRET'):
                    mirror.mirror_repository(SOURCE, 'demo', 'dest', Source(), dst, 'token')
                self.assertNotIn('body', dst.release)
                self.assertEqual(dst.deleted, [])
