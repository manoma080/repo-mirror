import json
import os
import tempfile
import unittest
from unittest.mock import patch

import mirror


class FilterIntegrationTests(unittest.TestCase):
    def setUp(self):
        secret = patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'})
        secret.start()
        self.addCleanup(secret.stop)
    def run_case(self, fail=False):
        sources = [dict(type='github', base_url='https://github.com', owner='alice', repos=['one', 'two']),
                   dict(type='github', base_url='https://github.com', owner='bob', repos=['three'])]
        def discover(source, api):
            if fail and source['owner'] == 'bob':
                raise OSError('failure')
            return source['repos']
        with tempfile.NamedTemporaryFile(mode='w') as f:
            json.dump(dict(destination_account_type='personal', sources=sources), f)
            f.flush()
            env = {'GH_TOKEN': 'token', 'DESTINATION_OWNER': 'dest',
                   'MIRROR_SOURCE_URLS': 'https://github.com/alice/two'}
            with patch.dict(os.environ, env), patch.object(mirror, 'discover', side_effect=discover), \
                 patch.object(mirror, 'get_existing', return_value=None), \
                 patch.object(mirror, 'mirror_repository') as run:
                if fail:
                    with self.assertRaises(RuntimeError):
                        mirror.main(['--config', f.name])
                else:
                    mirror.main(['--config', f.name])
                return [call.args[1] for call in run.call_args_list]

    def test_selected_only(self):
        self.assertEqual(self.run_case(), ['two'])

    def test_discovery_failure_prevents_all_writes(self):
        self.assertEqual(self.run_case(fail=True), [])
