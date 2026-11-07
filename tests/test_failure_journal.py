import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

import mirror


class FailureJournalTest(unittest.TestCase):
    def setUp(self):
        secret = patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'})
        secret.start()
        self.addCleanup(secret.stop)

    def test_recorded_assets_are_prioritized_for_retry(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice'}
        with tempfile.TemporaryDirectory() as directory:
            journal = mirror.FailureJournal(Path(directory) / 'failures.json')
            journal.set(journal.key(source, 'demo', 'bob', 'v1', 'second'), True)
            assets = [{'name': 'first'}, {'name': 'second'}]
            self.assertEqual([x['name'] for x in journal.retry_order(source, 'demo', 'bob', 'v1', assets)], ['second', 'first'])

    def test_main_supplies_journal_to_repository_sync(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice', 'repos': ['demo']}
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            path = Path(directory) / 'failures.json'
            config.write_text(json.dumps({'destination_account_type': 'personal', 'sources': [source]}))
            with patch.dict('os.environ', {'GH_TOKEN': 'example', 'DESTINATION_OWNER': 'bob'}), \
                 patch.object(mirror, 'get_existing', return_value=None), \
                 patch.object(mirror, 'mirror_repository') as sync:
                mirror.main(['--config', str(config), '--failures', str(path)])
            self.assertIsInstance(sync.call_args.kwargs['journal'], mirror.FailureJournal)

    def test_failed_upload_is_recorded_then_cleared_after_retry(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice', 'repos': ['demo']}
        class Source:
            def request(self, method, url, data=None):
                if url.endswith('/releases/latest'):
                    raise __import__('urllib.error', fromlist=['HTTPError']).HTTPError(url, 404, '', {}, None)
                return {'clone_url': 'https://github.com/alice/demo.git'}
            def pages(self, url):
                if url.endswith('/releases'): return [{'id': 1, 'tag_name': 'v1', 'name': 'one'}]
                return [{'name': 'file', 'size': 3, 'browser_download_url': 'https://github.com/a'}]
            def download(self, url, output): output.write(b'abc')
        class Destination:
            def __init__(self): self.release = None; self.fail = True; self.assets = []
            def request(self, method, url, data=None):
                if url.endswith('/repos/bob/mirror-alice-demo'): return {'description': mirror.source_marker(source, 'demo')}
                if '/releases/tags/' in url:
                    if self.release is None: raise HTTPError(url, 404, '', {}, None)
                    return self.release.copy()
                if method == 'POST' and url.endswith('/releases'):
                    self.release = dict(data, id=2, upload_url='https://uploads.github.com/a{?name}'); return self.release.copy()
                if method == 'PATCH' and '/releases/' in url: self.release.update(data)
            def pages(self, url): return self.assets if url.endswith('/assets') else []
            def upload(self, *args):
                if self.fail: raise RuntimeError('secret token must not be persisted')
                self.assets = [{'name': 'file', 'size': 3, 'state': 'uploaded', 'url': 'https://api.github.com/file'}]
            def download(self, url, out): out.write(b'abc')
        with tempfile.TemporaryDirectory() as directory, patch.object(mirror, 'mirror_git'):
            path = Path(directory) / 'failures.json'
            journal = mirror.FailureJournal(path)
            dst = Destination()
            with self.assertRaises(RuntimeError):
                mirror.mirror_repository(source, 'demo', 'bob', Source(), dst, 'token', journal=journal)
            text = path.read_text()
            self.assertNotIn('secret', text)
            self.assertEqual(len(json.loads(text)['failures']), 1)
            dst.fail = False
            mirror.mirror_repository(source, 'demo', 'bob', Source(), dst, 'token', journal=mirror.FailureJournal(path))
            self.assertEqual(json.loads(path.read_text())['failures'], [])
            self.assertEqual(dst.release['name'], 'one')
