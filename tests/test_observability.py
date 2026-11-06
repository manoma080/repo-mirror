import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch
from urllib.error import URLError

import mirror
from test_reconcile_assets import Source, Destination, SOURCE


class ObservabilityTests(unittest.TestCase):
    def test_sections_mark_discovery_repository_release_and_summary(self):
        output = io.StringIO()
        dst = Destination(); dst.fail = False
        config = {'destination_account_type': 'organization',
                  'sources': [{**SOURCE, 'repos': ['demo']}]}
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8') as f:
            json.dump(config, f); f.flush()
            with patch.dict(os.environ, {'GH_TOKEN': 'secret-token', 'DESTINATION_OWNER': 'dest'}), \
                 patch.object(mirror, 'SourceAPI', return_value=Source()), \
                 patch.object(mirror, 'API', return_value=dst), \
                 patch.object(mirror, 'mirror_git'), redirect_stderr(output):
                mirror.main(['--config', f.name])
        text = output.getvalue()
        headings = [line for line in text.splitlines() if line.startswith('===== ')]
        self.assertEqual(headings, ['===== Source discovery 1/1: alice =====',
                                    '===== Repository 1/1: alice/demo =====',
                                    '===== Release 1/1: v1 =====',
                                    '===== Summary ====='])
        self.assertIn('elapsed=', text)
        self.assertNotIn('secret-token', text)

    def test_release_heading_redacts_hostile_tags(self):
        output = io.StringIO()
        class Hostile(Source):
            def pages(self, url):
                values = super().pages(url)
                if url.endswith('/releases'):
                    values[0]['tag_name'] = 'v1\n::warning::injected'
                return values
        dst = Destination(); dst.fail = False
        with patch.object(mirror, 'mirror_git'), redirect_stderr(output):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', Hostile(), dst, 'secret-token')
        text = output.getvalue()
        self.assertIn('===== Release 1/1: [redacted] =====', text)
        self.assertNotIn('::warning::', text)
        self.assertFalse(any(line.startswith('::') for line in text.splitlines()))

    def test_public_names_and_elapsed_are_logged(self):
        output = io.StringIO()
        dst = Destination(); dst.fail = False
        with patch.object(mirror, 'mirror_git'), redirect_stderr(output):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', Source(), dst, 'secret-token')
        text = output.getvalue()
        self.assertIn('source_repo=alice/demo', text)
        self.assertIn('tag=', text)
        self.assertIn('asset_name=', text)
        self.assertIn('elapsed=', text)
        self.assertIn('reason=', text)
        self.assertNotIn('secret-token', text)
        self.assertNotIn('https://', text)

    def test_long_hyphenated_repository_and_asset_names_are_visible(self):
        for value in ('Ghostcontrol-PS5-USB-Controller-Patcher',
                      'Ghostcontrol-PS5-USB-Controller-Patcher.zip'):
            with self.subTest(value=value):
                self.assertEqual(mirror.safe_label(value), value)

    def test_long_opaque_token_is_suppressed(self):
        self.assertEqual(mirror.safe_label('aB7cD9eF2gH4iJ6kL8mN0pQ3rS5tU7vW'), '[redacted]')
        self.assertEqual(mirror.safe_label('ghp_' + 'aB7cD9eF2gH4iJ6kL8mN0pQ3rS5tU7vW'), '[redacted]')

    def test_opaque_hyphenated_credential_like_labels_are_suppressed(self):
        for value in ('aB7cD9eF-2gH4iJ6k-L8mN0pQ3-rS5tU7vW',
                      'aB7cD9eF-2gH4iJ6k-L8mN0pQ3-rS5tU7vW.zip'):
            with self.subTest(value=value):
                self.assertEqual(mirror.safe_label(value), '[redacted]')

    def test_low_entropy_alphanumeric_hyphenated_groups_are_suppressed(self):
        for value in ('ABCD1234-ABCD1234-ABCD1234-ABCD1234',
                      'release-ABCD1234-ABCD1234-ABCD1234-ABCD1234.zip'):
            with self.subTest(value=value):
                self.assertEqual(mirror.safe_label(value), '[redacted]')

    def test_uuid_and_long_hex_identifiers_are_suppressed(self):
        for value in ('12345678-1234-1234-1234-123456789abc',
                      'release-12345678-1234-1234-1234-123456789ABC.zip',
                      'build-abcdef0123456789abcdef01.zip',
                      'build-AAAAAAAAAAAAAAAAAAAAAAAA.tar.gz'):
            with self.subTest(value=value):
                self.assertEqual(mirror.safe_label(value), '[redacted]')

    def test_exact_configured_secret_overrides_meaningful_name(self):
        name = 'Ghostcontrol-PS5-USB-Controller-Patcher'
        context = mirror.LOG_SECRETS.set((name,))
        try:
            self.assertEqual(mirror.safe_label(name + '.zip'), '[redacted]')
        finally:
            mirror.LOG_SECRETS.reset(context)

    def test_hostile_labels_are_suppressed(self):
        for value in ('v1\n::warning::evil', 'ghp_abcdefghijklmnopqrstuvwxyz1234567890',
                      'https://host/file?signature=secret', '\x1b[31m', 'secret-token'):
            self.assertEqual(mirror.safe_label(value), '[redacted]')
        self.assertEqual(mirror.safe_label('v1.2.3'), 'v1.2.3')

    def test_repository_failure_still_logs_name_and_elapsed(self):
        output = io.StringIO()
        class Broken(Source):
            def request(self, *args):
                raise RuntimeError('sensitive details')
        with patch.object(mirror, 'mirror_git'), redirect_stderr(output):
            with self.assertRaises(RuntimeError):
                mirror.mirror_repository(SOURCE, 'demo', 'dest', Broken(), Destination(), 'opaquevalue123456789')
        self.assertIn('source_repo=alice/demo', output.getvalue())
        self.assertIn('elapsed=', output.getvalue())
        self.assertNotIn('sensitive details', output.getvalue())

    def test_explicit_credentials_are_not_exposed_as_labels(self):
        output = io.StringIO()
        class Hostile(Source):
            def pages(self, url):
                values = super().pages(url)
                if url.endswith('/releases'):
                    values[0]['tag_name'] = 'opaquevalue123456789'
                return values
        dst = Destination(); dst.fail = False
        with patch.object(mirror, 'mirror_git'), redirect_stderr(output):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', Hostile(), dst, 'opaquevalue123456789')
        self.assertNotIn('opaquevalue123456789', output.getvalue())

    def test_failed_request_is_timed_without_url_or_exception(self):
        api = mirror.API()
        api.opener.open = unittest.mock.Mock(side_effect=URLError('private'))
        output = io.StringIO()
        with patch.object(mirror.time, 'sleep'), redirect_stderr(output):
            with self.assertRaises(URLError):
                api._request('GET', 'https://example.com/?token=private')
        self.assertEqual(output.getvalue().count('network target=source'), 4)
        self.assertIn('elapsed=', output.getvalue())
        self.assertNotIn('private', output.getvalue())
        self.assertNotIn('https://', output.getvalue())
