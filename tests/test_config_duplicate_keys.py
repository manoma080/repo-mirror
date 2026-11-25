"""Configuration identities must not depend on JSON duplicate-key semantics."""
import os
import tempfile
import unittest
from unittest.mock import patch

from repo_mirror import adhoc, cli


class DuplicateConfigKeysTest(unittest.TestCase):
    def test_manual_config_update_rejects_duplicate_keys_before_discovery(self):
        with tempfile.NamedTemporaryFile(mode='w+', suffix='.json') as config:
            original = '{"destinations":{"main":{"account_type":"personal"}},"repositories":[],"repositories":[]}'
            config.write(original)
            config.flush()
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                adhoc.main(['--config', config.name, '--urls', 'https://github.com/a/b', '--remove'])
            config.seek(0)
            self.assertEqual(config.read(), original)

    def test_github_cli_rejects_duplicate_keys_before_discovery(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json') as config:
            config.write('{"destinations":{"main":{"account_type":"personal"}},"repositories":[],"repositories":[]}')
            config.flush()
            with patch.dict(os.environ, {'MIRROR_TIMESTAMP_SECRET': 'x' * 32,
                                         'GH_TOKEN': 'test', 'DESTINATION_OWNER': 'test'}, clear=True):
                with self.assertRaisesRegex(ValueError, 'duplicate'):
                    cli.main(['--config', config.name])
