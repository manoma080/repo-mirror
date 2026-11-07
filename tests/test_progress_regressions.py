import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import mirror
from test_reconcile_assets import Source, Destination, SOURCE


class ProgressRegressionTests(unittest.TestCase):
    def setUp(self):
        secret = patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'})
        secret.start()
        self.addCleanup(secret.stop)

    def test_error_details_only_allow_exact_known_messages(self):
        message = 'source default branch missing; refusing to prune'
        self.assertEqual(mirror.error_detail(RuntimeError(message)), message)
        self.assertEqual(mirror.error_detail(ValueError('GH_TOKEN and valid DESTINATION_OWNER are required')), 'GH_TOKEN and valid DESTINATION_OWNER are required')
        self.assertNotIn('secret', mirror.error_detail(RuntimeError(message + ' secret')))
        err = mirror.ReleaseRecoveryError('marker')
        err.args = ('secret\n::warning::injected',)
        self.assertNotIn('secret', mirror.error_detail(err))

    def test_source_and_repo_ordinals_appear_in_progress(self):
        config = {'destination_account_type': 'personal', 'sources': [dict(SOURCE, repos=['demo'])]}
        out = io.StringIO()
        with tempfile.NamedTemporaryFile(mode='w') as f:
            json.dump(config, f); f.flush()
            with patch.dict(os.environ, {'GH_TOKEN': 'secret-token', 'DESTINATION_OWNER': 'private-owner'}), patch.object(mirror, 'get_existing', return_value=None), patch.object(mirror, 'mirror_repository'), redirect_stderr(out):
                mirror.main(['--config', f.name])
        self.assertIn('source 1/1', out.getvalue())
        self.assertIn('repository 1', out.getvalue())
        self.assertNotIn('private-owner', out.getvalue())

    def test_release_and_asset_names_are_omitted(self):
        class HostileSource(Source):
            def pages(self, url):
                data = super().pages(url)
                if url.endswith('/releases'):
                    data[0]['tag_name'] = 'v1'
                else:
                    data[1]['name'] = 'https://example.test/file?token=secret\n::warning::oops'
                return data
        class DecodingDestination(Destination):
            def download(self, url, out):
                name = url.partition('/assets/')[2]
                out.write(self.assets[name])
            def upload(self, url, data, content_type):
                from urllib.parse import parse_qs, urlparse
                self.assets[parse_qs(urlparse(url).query)['name'][0]] = data.read()
        dst = DecodingDestination()
        dst.fail = False
        out = io.StringIO()
        with patch.object(mirror, 'mirror_git'), redirect_stderr(out):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', HostileSource(), dst, 'secret-token')
        text = out.getvalue()
        self.assertIn('release 1/1', text)
        self.assertIn('asset 1/2', text)
        self.assertNotIn('secret', text)
        self.assertNotIn('::warning::', text)
        self.assertNotIn('https://', text)

    def test_journal_upload_failure_is_reported_as_upload_failure(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, patch.object(mirror, 'mirror_git'), redirect_stderr(out):
            journal = mirror.FailureJournal(os.path.join(directory, 'journal'))
            with self.assertRaises(RuntimeError):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', Source(), Destination(), 'token', journal=journal)
        self.assertIn('asset upload failed', out.getvalue())
        self.assertIn('upload_failed=2', out.getvalue())

    def test_retried_counts_only_journaled_assets_with_upload_attempts(self):
        dst = Destination()
        dst.assets = {'changed': b'new'}
        dst.fail = False
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, patch.object(mirror, 'mirror_git'), redirect_stderr(output):
            journal = mirror.FailureJournal(os.path.join(directory, 'journal'))
            for asset_name in ('changed', 'new'):
                journal.set(journal.key(SOURCE, 'demo', 'dest', 'v1', asset_name), True)
            mirror.mirror_repository(SOURCE, 'demo', 'dest', Source(), dst, 'token', journal=journal)
        self.assertEqual(dst.assets, {'changed': b'new', 'new': b'n'})
        self.assertIn('assets uploaded=1 updated=0 skipped=1 retried=1 upload_failed=0', output.getvalue())
        self.assertFalse(journal.failures)

    def test_preflight_failure_identifies_repository_ordinal(self):
        config = {'destination_account_type': 'personal', 'sources': [dict(SOURCE, repos=['demo'])]}
        out = io.StringIO()
        with tempfile.NamedTemporaryFile(mode='w') as f:
            json.dump(config, f); f.flush()
            with patch.dict(os.environ, {'GH_TOKEN': 'token', 'DESTINATION_OWNER': 'dest'}), patch.object(mirror, 'get_existing', side_effect=RuntimeError('destination collision: repository has no matching source marker')), redirect_stderr(out):
                with self.assertRaises(RuntimeError): mirror.main(['--config', f.name])
        self.assertIn('repository 1', out.getvalue())
        self.assertIn('destination collision: repository has no matching source marker', out.getvalue())

    def test_discovery_failure_is_flushed(self):
        class Output(io.StringIO):
            def __init__(self):
                super().__init__(); self.flush_values = []
            def flush(self):
                self.flush_values.append(self.getvalue())
                super().flush()
        config = {'destination_account_type': 'personal', 'sources': [dict(SOURCE, repos='all')]}
        out = Output()
        with tempfile.NamedTemporaryFile(mode='w') as f:
            json.dump(config, f); f.flush()
            with patch.dict(os.environ, {'GH_TOKEN': 'token', 'DESTINATION_OWNER': 'dest'}), patch.object(mirror, 'discover', side_effect=RuntimeError('secret')), redirect_stderr(out):
                with self.assertRaises(RuntimeError): mirror.main(['--config', f.name])
        self.assertTrue(any(value.endswith('source discovery failed for source 1: RuntimeError\n') for value in out.flush_values))
        self.assertNotIn('secret', out.getvalue())
