import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import mirror
from test_reconcile_assets import SOURCE, Source, Destination


class ReleaseBodyV3Tests(unittest.TestCase):
    def setUp(self):
        secret = patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'})
        secret.start()
        self.addCleanup(secret.stop)

    def test_starter_journal_entry_cleared_on_success_only(self):
        class Waiting(Source):
            def pages(self, url):
                if url.endswith('/releases'): return super().pages(url)
                return [{'name': 'waiting', 'size': 0, 'state': 'starter'}]
        dst = Destination()
        with tempfile.TemporaryDirectory() as directory, patch.object(mirror, 'mirror_git'):
            journal = mirror.FailureJournal(Path(directory) / 'journal.json')
            starter = journal.key(SOURCE, 'demo', 'dest', 'v1', 'waiting')
            other = journal.key(SOURCE, 'demo', 'dest', 'v1', 'other')
            journal.set(starter, True)
            journal.set(other, True)
            mirror.mirror_repository(SOURCE, 'demo', 'dest', Waiting(), dst, 'token', journal=journal)
            self.assertNotIn(starter, journal.failures)
            self.assertIn(other, journal.failures)
        self.assertEqual(dst.deleted, [])

    def test_starter_is_skipped_without_leaving_pending(self):
        class WaitingSource(Source):
            waiting = True
            def pages(self, url):
                result = super().pages(url)
                if not url.endswith('/releases') and self.waiting:
                    return [{'name': 'SHA256SUMS', 'size': 0, 'state': 'starter'}]
                return result
        src = WaitingSource()
        dst = Destination()
        dst.release['body'] = 'existing notes'
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
        self.assertEqual(dst.deleted, [])
        self.assertEqual(dst.release['body'], 'notes')

    def test_bound_marker_rejects_changed_source_manifest(self):
        class ChangingSource(Source):
            changed = False
            def pages(self, url):
                result = super().pages(url)
                if self.changed and not url.endswith('/releases'):
                    result[0]['size'] = 4
                return result
        src = ChangingSource()
        dst = Destination()
        dst.release['body'] = 'existing notes'
        with patch.object(mirror, 'mirror_git'):
            with self.assertRaises(RuntimeError):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
            self.assertIn('BOUND', dst.release['body'])
            deleted_before = list(dst.deleted)
            src.changed = True
            with self.assertRaisesRegex(mirror.ReleaseRecoveryError, 'changed'):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')
            self.assertEqual(dst.deleted, deleted_before)

    def test_long_title_survives_interruption_and_restart(self):
        title = 'PS5X360 ' + 'long release title ' * 10
        class LongTitleSource(Source):
            def pages(self, url):
                releases = super().pages(url)
                if url.endswith('/releases'):
                    releases[0]['name'] = title
                return releases
        dst = Destination()
        dst.release['body'] = 'existing notes'
        with patch.object(mirror, 'mirror_git'):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', LongTitleSource(), dst, 'token')
            self.assertEqual(dst.release['name'], 'old')
            self.assertIn('[repo-mirror:pending:v3]', dst.release['body'])
            self.assertIn('existing notes', dst.release['body'])
            dst.fail = False
            mirror.mirror_repository(SOURCE, 'demo', 'dest', LongTitleSource(), dst, 'token')
        self.assertEqual(dst.release['name'], title)
        self.assertEqual(dst.release['body'], 'notes')

    def test_malformed_and_duplicate_markers_fail_without_mutation(self):
        sentinel = '[repo-mirror:pending:v3]'
        for body in (sentinel, 'notes\n' + sentinel + '{}',
                     'notes\n' + sentinel + '{"phase":"WAITING_SOURCE","phase":"BOUND","digest":null,"replace":true}',
                     'notes\n' + sentinel + '{}' + sentinel):
            with self.subTest(body=body):
                dst = Destination()
                dst.release['body'] = body
                with patch.object(mirror, 'mirror_git'):
                    with self.assertRaises(mirror.ReleaseRecoveryError):
                        mirror.mirror_repository(SOURCE, 'demo', 'dest', Source(), dst, 'token')
                self.assertEqual(dst.release['body'], body)
                self.assertEqual(dst.deleted, [])

    def test_source_marker_collision_fails_without_mutation(self):
        class Collision(Source):
            def pages(self, url):
                result = super().pages(url)
                if url.endswith('/releases'):
                    result[0]['body'] = 'user [repo-mirror:pending:v3] text'
                return result
        dst = Destination()
        with patch.object(mirror, 'mirror_git'):
            with self.assertRaises(mirror.ReleaseRecoveryError):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', Collision(), dst, 'token')
        self.assertEqual(dst.release['name'], 'old')
        self.assertEqual(dst.deleted, [])

    def test_starter_transition_before_upload_is_skipped(self):
        class Changing(Source):
            calls = 0
            def pages(self, url):
                if url.endswith('/releases'):
                    return super().pages(url)
                self.calls += 1
                return [{'name': 'new', 'size': 1, 'browser_download_url': 'https://github.com/new'},
                        {'name': 'later', 'size': 0, 'state': 'starter' if self.calls == 1 else 'uploaded'}]
        dst = Destination()
        dst.assets = {}
        dst.fail = False
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', Changing(), dst, 'token')
        self.assertEqual(dst.assets, {'new': b'n'})

    def test_starter_transition_survives_interruption_and_clears_journal(self):
        class Waiting(Source):
            uploaded = False
            def pages(self, url):
                result = super().pages(url)
                if not url.endswith('/releases'):
                    result.append({'name': 'waiting', 'id': 99, 'size': 0,
                                   'state': 'uploaded' if self.uploaded else 'starter'})
                return result
        src, dst = Waiting(), Destination()
        dst.assets['waiting'] = b'keep'
        with tempfile.TemporaryDirectory() as directory, patch.object(mirror, 'mirror_git'):
            journal = mirror.FailureJournal(Path(directory) / 'journal.json')
            key = journal.key(SOURCE, 'demo', 'dest', 'v1', 'waiting')
            journal.set(key, True)
            with self.assertRaises(RuntimeError):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token', journal=journal)
            src.uploaded = True
            dst.fail = False
            mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token', journal=journal)
            self.assertNotIn(key, journal.failures)
        self.assertEqual(dst.assets['waiting'], b'keep')
        self.assertEqual(dst.release['body'], 'notes')

    def test_signed_starter_snapshot_rejects_tampering(self):
        class Waiting(Source):
            def pages(self, url):
                result = super().pages(url)
                if not url.endswith('/releases'):
                    result.append({'name': 'waiting', 'id': 99, 'state': 'starter'})
                return result
        dst = Destination()
        with patch.object(mirror, 'mirror_git'):
            with self.assertRaises(RuntimeError):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', Waiting(), dst, 'token')
            dst.release['body'] = dst.release['body'].replace('"id":99', '"id":98')
            before = list(dst.deleted)
            with self.assertRaisesRegex(mirror.ReleaseRecoveryError, 'marker'):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', Waiting(), dst, 'token')
            self.assertEqual(dst.deleted, before)

    def test_new_starter_is_snapshotted_before_it_transitions(self):
        class Changing(Source):
            calls = 0
            def pages(self, url):
                if url.endswith('/releases'):
                    return super().pages(url)
                self.calls += 1
                assets = [{'name': 'new', 'size': 1, 'browser_download_url': 'https://github.com/new'}]
                if self.calls >= 2:
                    assets.append({'name': 'later', 'id': 42, 'size': 0,
                                   'state': 'starter' if self.calls == 2 else 'uploaded'})
                return assets
        dst = Destination()
        dst.assets = {'later': b'keep'}
        dst.fail = False
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', Changing(), dst, 'token')
        self.assertEqual(dst.assets, {'new': b'n', 'later': b'keep'})

    def test_starter_transition_does_not_hide_other_asset_change(self):
        class Changing(Source):
            calls = 0
            def pages(self, url):
                if url.endswith('/releases'):
                    return super().pages(url)
                self.calls += 1
                return [{'name': 'new', 'size': 1 if self.calls == 1 else 2,
                         'browser_download_url': 'https://github.com/new'},
                        {'name': 'later', 'id': 42, 'size': 0,
                         'state': 'starter' if self.calls == 1 else 'uploaded'}]
        dst = Destination()
        dst.assets = {}
        dst.fail = False
        with patch.object(mirror, 'mirror_git'):
            with self.assertRaisesRegex(mirror.ReleaseRecoveryError, 'changed'):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', Changing(), dst, 'token')
        self.assertEqual(dst.assets, {})

    def test_starter_name_collision_with_uploaded_asset_fails_safely(self):
        class Duplicate(Source):
            def pages(self, url):
                if url.endswith('/releases'):
                    return super().pages(url)
                return [{'name': 'changed', 'size': 3, 'browser_download_url': 'https://github.com/changed'},
                        {'name': 'changed', 'size': 0, 'state': 'starter'}]
        dst = Destination()
        with patch.object(mirror, 'mirror_git'):
            with self.assertRaises(mirror.ReleaseRecoveryError):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', Duplicate(), dst, 'token')
        self.assertEqual(dst.deleted, [])
