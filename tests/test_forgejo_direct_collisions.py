import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from repo_mirror import forgejo_sync


class DirectCollisionTests(unittest.TestCase):
    def test_full_config_collision_rejected_before_any_source_or_destination_access(self):
        github = 'https://github.com/mirror-owner/mirror-demo'
        cases = [
            [github, 'https://codeberg.org/alice/MIRROR-DEMO'],
            [github, 'https://codeberg.org/alice/project', 'https://forge.example/bob/PROJECT'],
        ]
        for sources in cases:
            with self.subTest(sources=sources), tempfile.TemporaryDirectory() as directory:
                config = os.path.join(directory, 'sources.json')
                with open(config, 'w') as stream:
                    json.dump(sources, stream)
                argv = ['--config', config, '--source', github, '--token-file', 'missing-token',
                        '--timestamp-secret-file', 'missing-secret', '--journal-dir', directory,
                        '--owner', 'local', '--use-github-token']
                output = io.StringIO()
                with patch('repo_mirror.forgejo_sync.SourceAPI') as source_api, \
                     patch('repo_mirror.forgejo_sync.LocalForgejoAPI') as destination_api, \
                     patch('repo_mirror.forgejo_sync.destination_locks') as locks, \
                     redirect_stderr(output):
                    self.assertEqual(forgejo_sync.main(argv), 2)
                self.assertIn('conflicting destination', output.getvalue())
                source_api.assert_not_called()
                destination_api.assert_not_called()
                locks.assert_not_called()
                self.assertEqual(os.listdir(directory), ['sources.json'])
