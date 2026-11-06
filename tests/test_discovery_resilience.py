import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import mirror


class DiscoveryResilienceTests(unittest.TestCase):
    def test_failed_source_reported_and_other_source_mirrored(self):
        sources = [
            {'type': 'github', 'base_url': 'https://github.com', 'owner': 'bad', 'repos': 'all'},
            {'type': 'github', 'base_url': 'https://github.com', 'owner': 'good', 'repos': ['one']},
        ]
        def discover(source, api):
            if source['owner'] == 'bad':
                raise OSError('secret details')
            return source['repos']
        stderr = io.StringIO()
        with tempfile.NamedTemporaryFile(mode='w') as f:
            json.dump({'destination_account_type': 'personal', 'sources': sources}, f); f.flush()
            with patch.dict(os.environ, {'GH_TOKEN': 'token', 'DESTINATION_OWNER': 'dest'}), patch.object(mirror, 'discover', side_effect=discover), patch.object(mirror, 'get_existing', return_value=None), patch.object(mirror, 'mirror_repository') as run, redirect_stderr(stderr):
                with self.assertRaises(RuntimeError):
                    mirror.main(['--config', f.name])
        self.assertEqual([call.args[1] for call in run.call_args_list], ['one'])
        self.assertIn('source discovery failed for source 1', stderr.getvalue())
        self.assertIn('source 1/2', stderr.getvalue())
        self.assertIn('OSError', stderr.getvalue())
        self.assertNotIn('secret details', stderr.getvalue())

    def test_failed_discovery_blocks_potential_collision_but_not_disjoint_prefix(self):
        sources = [
            {'type': 'github', 'base_url': 'https://github.com', 'owner': 'bad', 'prefix': 'x-', 'repos': 'all'},
            {'type': 'github', 'base_url': 'https://github.com', 'owner': 'good', 'prefix': 'X-', 'repos': ['repo']},
            {'type': 'github', 'base_url': 'https://github.com', 'owner': 'safe', 'prefix': 'safe-', 'repos': ['repo']},
        ]
        def discover(source, api):
            if source['owner'] == 'bad': raise OSError('secret')
            return source['repos']
        stderr = io.StringIO()
        with tempfile.NamedTemporaryFile(mode='w') as f:
            json.dump({'destination_account_type': 'personal', 'sources': sources}, f); f.flush()
            with patch.dict(os.environ, {'GH_TOKEN': 'token', 'DESTINATION_OWNER': 'dest'}), patch.object(mirror, 'discover', side_effect=discover), patch.object(mirror, 'get_existing', return_value=None), patch.object(mirror, 'mirror_repository') as run, redirect_stderr(stderr):
                with self.assertRaises(RuntimeError): mirror.main(['--config', f.name])
        self.assertEqual([c.args[0]['owner'] for c in run.call_args_list], ['safe'])
        self.assertIn('collision', stderr.getvalue().lower())

    def test_collision_between_successful_sources_still_aborts_before_writes(self):
        sources = [
            {'type': 'github', 'base_url': 'https://github.com', 'owner': 'bad', 'repos': 'all'},
            {'type': 'github', 'base_url': 'https://github.com', 'owner': 'one', 'prefix': 'x-', 'repos': ['repo']},
            {'type': 'github', 'base_url': 'https://github.com', 'owner': 'two', 'prefix': 'X-', 'repos': ['REPO']},
        ]
        def discover(source, api):
            if source['owner'] == 'bad': raise OSError('broken')
            return source['repos']
        with tempfile.NamedTemporaryFile(mode='w') as f:
            json.dump({'destination_account_type': 'personal', 'sources': sources}, f); f.flush()
            with patch.dict(os.environ, {'GH_TOKEN': 'token', 'DESTINATION_OWNER': 'dest'}), patch.object(mirror, 'discover', side_effect=discover), patch.object(mirror, 'mirror_repository') as run:
                with self.assertRaisesRegex(ValueError, 'duplicate destination'):
                    mirror.main(['--config', f.name])
                run.assert_not_called()
