import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('forgejo_local_config_launcher', Path(__file__).resolve().parents[1]/'scheduler/auto_update.py')
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class LocalConfigLauncherTests(unittest.TestCase):
    def test_local_snapshot_pin_and_blob(self):
        raw = b'{"test":true}\n'
        repo = dict(id=31, private=True, has_actions=False, default_branch='main', full_name='labadmin/control')
        file = dict(type='file', encoding='base64', content=base64.b64encode(raw).decode(), sha=hashlib.sha1(f'blob {len(raw)}\0'.encode()+raw).hexdigest())
        requests = []
        def get(url, token):
            requests.append((url,token))
            return file if '/contents/' in url else repo
        with patch.object(updater, 'local_json', side_effect=get):
            self.assertEqual(updater.local_manifest('labadmin/control', 31, 3300, 'local-token'), raw)
            self.assertEqual(len(requests), 3)
            self.assertTrue(all(u.startswith('/api/v1/repos/labadmin/control') for u, _ in requests))
            with self.assertRaises(ValueError):
                updater.local_manifest('labadmin/control', 32, 3300, 'local-token')
            file['sha'] = '0' * 40
            with self.assertRaises(ValueError):
                updater.local_manifest('labadmin/control', 31, 3300, 'local-token')
