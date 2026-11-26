"""Manual actions must not use the unmodified scheduled selection."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repo_mirror import forgejo_schedule


class DispatchWiringTests(unittest.TestCase):
    def test_manual_remove_stops_before_mirror_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            manifest = p / 'config-forgejo.json'
            manifest.write_text(json.dumps({'destinations': {'local': {}}, 'repositories': [
                {'source': {'url': 'https://github.com/acme/demo'}, 'mirrors': {'local': {'name': 'demo'}}}
            ]}))
            secret = p / 'secret'
            secret.write_text('s' * 48)
            secret.chmod(0o600)
            token = p / 'token'
            token.touch(mode=0o600)
            args = ['run', '--manifest', str(manifest), '--journal-dir', directory, '--lock', str(p / 'lock'),
                    '--timestamp-secret-file', str(secret), '--token-file', str(token), '--owner', 'user',
                    '--installation', '123456', '--config-repo', 'acme/private-config']
            data = json.dumps(dict(source_urls='https://github.com/acme/demo', remove_from_config=True))
            with (patch.dict(os.environ, {'FORGEJO_MANUAL_INPUTS': data}),
                  patch('repo_mirror.forgejo_schedule.prepare_manual', return_value=(None, None, [])) as manual,
                  patch('repo_mirror.forgejo_schedule.subprocess.Popen') as workers,
                  contextlib.redirect_stdout(io.StringIO()) as out):
                self.assertEqual(forgejo_schedule.main(args), 0)
            manual.assert_called_once()
            workers.assert_not_called()
            self.assertIn('No mirroring requested', out.getvalue())
