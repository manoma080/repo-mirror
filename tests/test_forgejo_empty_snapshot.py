import subprocess
import unittest
from unittest.mock import patch

from repo_mirror.forgejo_git import ForgejoDestination, snapshot_refs, sync_refs


class API:
    base_url = 'http://127.0.0.1:3000'

    def __init__(self, state='initialized'):
        self.state = state

    def token(self):
        return 'local-token'

    def request(self, method, path, payload=None):
        assert method == 'GET'
        return {'id': 1, 'full_name': 'mirror/project', 'private': True,
                'description': f'repo-mirror-source: github.com/source/project [repo-mirror:{self.state}:v1]',
                'has_actions': False, 'default_branch': 'main'}


class EmptySnapshotTests(unittest.TestCase):
    def run_scenario(self, operation, source_listings, destination_listing=b'', state='initialized'):
        listings = iter(source_listings)
        writes = []
        def run(args, **kwargs):
            if 'init' in args:
                output = b''
            elif '--symref' in args:
                output = b''
            elif '--refs' in args:
                output = destination_listing if any('127.0.0.1' in x for x in args) else next(listings)
            else:
                writes.append(args)
                raise AssertionError('unexpected Git write')
            return subprocess.CompletedProcess(args, 0, output, b'')
        with patch('repo_mirror.forgejo_git.subprocess.run', side_effect=run):
            result = operation(ForgejoDestination(API(state), 'mirror'), 'project', 'source/project', source_token='source')
        self.assertEqual(writes, [])
        return result

    def test_empty_snapshot_is_valid(self):
        for state in ('initializing', 'initialized'):
            with self.subTest(state=state):
                self.assertEqual(self.run_scenario(snapshot_refs, [b'', b''], state=state), (None, {}, {}))

    def test_empty_sync_is_valid(self):
        for state in ('initializing', 'initialized'):
            with self.subTest(state=state):
                self.assertEqual(self.run_scenario(sync_refs, [b'', b''], state=state), 0)

    def test_empty_snapshot_rejects_populated_destination(self):
        extra = b'a' * 40 + b'\trefs/heads/main\n'
        with self.assertRaisesRegex(RuntimeError, 'ref readback'):
            self.run_scenario(snapshot_refs, [b'', b''], extra)

    def test_empty_sync_rejects_populated_destination(self):
        extra = b'a' * 40 + b'\trefs/heads/main\n'
        with self.assertRaisesRegex(RuntimeError, 'ref readback'):
            self.run_scenario(sync_refs, [b'', b''], extra)

    def test_empty_sync_rejects_source_drift(self):
        extra = b'a' * 40 + b'\trefs/tags/new\n'
        with self.assertRaisesRegex(RuntimeError, 'source refs changed'):
            self.run_scenario(sync_refs, [b'', extra])

    def test_empty_snapshot_rejects_source_drift(self):
        extra = b'a' * 40 + b'\trefs/tags/new\n'
        with self.assertRaisesRegex(RuntimeError, 'source refs changed'):
            self.run_scenario(snapshot_refs, [b'', extra])

    def test_empty_snapshot_rejects_refs_without_head(self):
        extra = b'a' * 40 + b'\trefs/tags/new\n'
        with self.assertRaisesRegex(RuntimeError, 'source default branch'):
            self.run_scenario(snapshot_refs, [extra, extra])
