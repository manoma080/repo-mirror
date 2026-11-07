import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
import mirror
from test_reconcile_assets import SOURCE, Source, Destination


class ReplayBindingTests(unittest.TestCase):
    def test_starter_rename_with_stable_id_stays_skipped_and_clears_journal(self):
        class Changing(Source):
            calls = 0
            def pages(self, url):
                if url.endswith('/releases'):
                    return super().pages(url)
                self.calls += 1
                return [{'name': 'new', 'size': 1, 'browser_download_url': 'https://github.com/new'},
                        {'name': 'old' if self.calls == 1 else 'renamed', 'id': 42,
                         'state': 'starter' if self.calls == 1 else 'uploaded', 'size': 0}]
        dst = Destination()
        dst.assets = {'renamed': b'keep'}
        dst.fail = False
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'}), patch.object(mirror, 'mirror_git'):
            journal = mirror.FailureJournal(Path(directory) / 'journal.json')
            keys = [journal.key(SOURCE, 'demo', 'dest', 'v1', name) for name in ('old', 'renamed')]
            for key in keys:
                journal.set(key, True)
            mirror.mirror_repository(SOURCE, 'demo', 'dest', Changing(), dst, 'token', journal=journal)
            self.assertTrue(all(key not in journal.failures for key in keys))
        self.assertEqual(dst.assets, {'renamed': b'keep', 'new': b'n'})

    def test_signed_marker_cannot_be_replayed_to_other_destination_or_release(self):
        with patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'}), patch.object(mirror, 'mirror_git'):
            original = Destination()
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', Source(), original, 'token')
            for owner, release_id in [('other', 2), ('dest', 99)]:
                class Target(Destination):
                    def request(self, method, url, data=None):
                        if '/repos/' in url and '/releases' not in url:
                            return {'description': mirror.source_marker(SOURCE, 'demo')}
                        return super().request(method, url, data)
                dst = Target()
                dst.release['id'] = release_id
                dst.release['body'] = original.release['body']
                with self.subTest(owner=owner, release_id=release_id):
                    with self.assertRaises(mirror.ReleaseRecoveryError):
                        mirror.mirror_repository(SOURCE, 'demo', owner, Source(), dst, 'token')
                    self.assertEqual(dst.deleted, [])
                    self.assertEqual(dst.assets['changed'], b'old')
