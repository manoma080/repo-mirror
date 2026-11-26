import unittest
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from repo_mirror.config_named import normalize_config
from repo_mirror.forgejo_sync import normalize_named
from scheduler.auto_update import valid_manifest

GITHUB = {'type': 'github', 'url': 'https://github.com', 'owner': 'mirror-owner', 'auth': 'mirrorbot'}
FORGEJO = {'type': 'forgejo', 'url': 'http://127.0.0.1:3000', 'owner': 'forgejo-owner', 'auth': 'forgejo-home'}


def manifest(profile, settings):
    return {'destinations': {profile: settings}, 'repositories': [{'source': {'url': 'https://github.com/mirror-owner/mirror-demo', 'id': 123}, 'mirrors': {profile: {'name': 'mirror-demo', 'id': 456}}}]}


class ProfileTests(unittest.TestCase):
    def test_github_profile_preserves_pins(self):
        result = normalize_config(manifest('github-mirror-owner', GITHUB))
        self.assertEqual(result['account_type'], 'personal')
        self.assertEqual(result['repositories'][0]['mirror']['destination_id'], 456)

    def test_runtime_profile_pins_fail_closed(self):
        from repo_mirror.destination_profile import validate_runtime
        github = {**GITHUB, 'auth': 'mirrorbot'}
        validate_runtime(github, 'github', 'https://github.com', 'mirror-owner', 'mirrorbot')
        for changes in ({'owner': 'other'}, {'url': 'https://evil.example'}, {'auth': 'other-app'}, {'type': 'forgejo'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_runtime({**github, **changes}, 'github', 'https://github.com', 'mirror-owner', 'mirrorbot')
        validate_runtime(FORGEJO, 'forgejo', 'http://127.0.0.1:3000', 'forgejo-owner', 'forgejo-home')
        with self.assertRaises(ValueError):
            validate_runtime(FORGEJO, 'forgejo', 'http://127.0.0.1:3001', 'forgejo-owner', 'forgejo-home')

    def test_entrypoints_reject_mismatch_before_io(self):
        from repo_mirror import cli, forgejo_schedule, forgejo_sync
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps(manifest('github', {**GITHUB, 'auth': 'mirrorbot'})))
            env = {'MIRROR_TIMESTAMP_SECRET': 'secret', 'DESTINATION_OWNER': 'wrong',
                   'MIRROR_APP_ID': '123', 'MIRROR_APP_PRIVATE_KEY': 'private'}
            with patch.dict(os.environ, env, clear=True), patch('repo_mirror.cli.SourceAPI') as api:
                with self.assertRaisesRegex(ValueError, 'runtime mismatch'):
                    cli.main(['--config', str(config)])
                api.assert_not_called()
            config.write_text(json.dumps(manifest('local', FORGEJO)))
            common = ['--token-file', 'missing', '--timestamp-secret-file', 'missing', '--journal-dir', 'missing', '--owner', 'wrong', '--port', '3000']
            with patch('repo_mirror.forgejo_sync.LocalForgejoAPI') as api:
                self.assertEqual(forgejo_sync.main(['--config', str(config), '--source', 'https://github.com/mirror-owner/mirror-demo', *common]), 2)
                api.assert_not_called()
            with patch('repo_mirror.forgejo_schedule.subprocess.Popen') as spawn:
                self.assertEqual(forgejo_schedule.main(['run', '--manifest', str(config), '--lock', 'missing', *common]), 2)
                spawn.assert_not_called()

    def test_forgejo_profile_parses_in_worker_and_launcher(self):
        value = manifest('forgejo-home', FORGEJO)
        self.assertEqual(normalize_named(value)[0]['destination_id'], 456)
        self.assertTrue(valid_manifest(value))
