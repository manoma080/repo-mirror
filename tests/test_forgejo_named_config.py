import copy
import json
import importlib.util
from pathlib import Path

import unittest

from repo_mirror.forgejo_sync import parse_manifest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('standalone_updater', ROOT / 'scheduler/auto_update.py')
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


def named():
    return {'destinations': {'local': {'account_type': 'personal'}}, 'repositories': [
        {'source': {'url': 'https://github.com/manoma080/mirror-a'},
         'mirrors': {'local': {'name': 'mirror-a'}}}]}


class NamedConfigTests(unittest.TestCase):
    def test_named_manifest_preserves_selection(self):
        config = named()
        entries = parse_manifest(config)
        self.assertEqual(entries[0]['url'], config['repositories'][0]['source']['url'])
        self.assertEqual(entries[0]['destination_name'], 'mirror-a')
        self.assertTrue(updater.valid_manifest(config))

    def test_pins_and_custom_name_are_preserved(self):
        config = named()
        config['repositories'][0]['source']['id'] = 123
        config['repositories'][0]['mirrors']['local'] = {'name': 'copy', 'id': 456}
        entry = parse_manifest(config)[0]
        self.assertEqual((entry['source_id'], entry['destination_id'], entry['destination_name']), (123, 456, 'copy'))
        self.assertTrue(updater.valid_manifest(config))

    def test_named_pins_are_enforced_by_worker(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from repo_mirror.forgejo_sync import _sync_requested
        config = named()
        config['repositories'][0] = {'source': {'url': 'https://git.example/a/b', 'id': 11},
                                     'mirrors': {'local': {'name': 'copy', 'id': 22}}}
        entry = parse_manifest(config)[0]
        identity = entry['identity']
        source = Mock()
        source.request.return_value = {'id': 99, 'full_name': 'a/b', 'clone_url': identity + '.git'}
        destination = Mock()
        import tempfile
        with tempfile.TemporaryDirectory() as directory, patch('repo_mirror.forgejo_sync.PublicForgejoSourceAdapter', return_value=source):
            result = _sync_requested(SimpleNamespace(port=3300, owner='dest'), [identity], destination,
                                     None, 'token', 'x' * 32, directory, mappings={identity: entry})
        self.assertEqual(result, 1)
        source.request.assert_called_once()
        destination.begin_initialization.assert_not_called()

    def test_named_relocation_retains_old_binding(self):
        config = named()
        config['repositories'][0] = {'source': {'url': 'https://git.example/new/repo', 'id': 11},
            'mirrors': {'local': {'name': 'new-repo', 'id': 22,
                'legacy': {'url': 'https://git.example/old/repo', 'name': 'old-repo', 'signature': 'a' * 64}}}}
        entry = parse_manifest(config)[0]
        self.assertEqual((entry['legacy_identity'], entry['legacy_destination_name'], entry['signature']),
                         ('https://git.example/old/repo', 'old-repo', 'a' * 64))
        self.assertTrue(updater.valid_manifest(config))

    def test_named_manifest_scheduler_passes_exact_snapshot_to_worker(self):
        import os
        import subprocess
        import sys
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            config = named()
            config['repositories'][0]['source']['url'] = 'https://forge.example/a/b'
            config['repositories'][0]['mirrors']['local']['name'] = 'copy'
            manifest = tmp / 'manifest'
            manifest.write_text(json.dumps(config))
            secret = tmp / 'secret'; secret.write_text('x' * 32); secret.chmod(0o600)
            journal = tmp / 'journal'; journal.mkdir(mode=0o700)
            worker = tmp / 'worker'
            worker.write_text('#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\na=sys.argv\nPath(os.environ["RESULT"]).write_text(json.dumps([json.loads(Path(a[a.index("--config")+1]).read_text()),a[-1]]))\n')
            worker.chmod(0o700)
            cmd = [sys.executable, '-m', 'repo_mirror.forgejo_schedule', 'run', '--manifest', str(manifest), '--token-file', 'unused', '--timestamp-secret-file', str(secret), '--journal-dir', str(journal), '--owner', 'alice', '--lock', str(tmp / 'lock'), '--sync-python', str(worker)]
            result = subprocess.run(cmd, cwd=ROOT, env={**os.environ, 'RESULT': str(tmp / 'result')}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            snapshot, url = json.loads((tmp / 'result').read_text())
            self.assertEqual(snapshot, config)
            self.assertEqual(url, 'https://forge.example/a/b')

    def test_duplicate_json_keys_are_rejected(self):
        raw = '{"destinations":{},"destinations":{},"repositories":[]}'
        with self.assertRaises(ValueError):
            json.loads(raw, object_pairs_hook=updater.unique_pairs)

    def test_multiple_destinations_rejected(self):
        config = named()
        config['destinations']['other'] = {}
        with self.assertRaises(ValueError):
            parse_manifest(config)
        self.assertFalse(updater.valid_manifest(config))
