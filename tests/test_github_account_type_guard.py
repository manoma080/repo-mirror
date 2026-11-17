import json
import os
import tempfile
import unittest
from unittest.mock import patch

from repo_mirror import cli


class GithubAccountTypeGuardTests(unittest.TestCase):
    def run_config(self, config, account_type):
        env = {'MIRROR_TIMESTAMP_SECRET': 'test', 'GH_TOKEN': 'token',
               'DESTINATION_OWNER': 'dest', 'DESTINATION_ACCOUNT_TYPE': account_type}
        with tempfile.NamedTemporaryFile(mode='w+') as f:
            json.dump(config, f)
            f.flush()
            with patch.dict(os.environ, env, clear=True), patch.object(cli, 'API') as api:
                cli.main(['--config', f.name])
                return api

    def test_named_profile_rejects_environment_mismatch_before_api(self):
        config = {'destinations': {'main': {'account_type': 'personal'}}, 'repositories': []}
        with self.assertRaisesRegex(ValueError, 'account type mismatch'):
            self.run_config(config, 'organization')

    def test_named_profile_allows_empty_or_matching_workflow_environment(self):
        config = {'destinations': {'main': {'account_type': 'personal'}}, 'repositories': []}
        for value in ('', 'personal'):
            with self.subTest(value=value):
                self.run_config(config, value)

    def test_legacy_environment_override_remains_supported(self):
        self.run_config({'destination_account_type': 'personal', 'sources': []}, 'organization')
