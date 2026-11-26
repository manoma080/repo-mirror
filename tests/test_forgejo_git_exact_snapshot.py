import subprocess
import unittest
from unittest.mock import patch

from repo_mirror.forgejo_git import ForgejoDestination, snapshot_refs


class ExactSnapshotTests(unittest.TestCase):
    def test_snapshot_rejects_destination_only_refs(self):
        oid = 'a' * 40

        class API:
            base_url = 'http://127.0.0.1:3000'

            def token(self):
                return 'local-secret'

            def request(self, method, path, payload=None):
                return {'id': 1, 'full_name': 'mirror/project', 'private': True,
                        'description': 'repo-mirror-source: github.com/source/project',
                        'has_actions': False, 'default_branch': 'main'}

        def run(args, **kwargs):
            if '--symref' in args:
                output = f'ref: refs/heads/main\tHEAD\n{oid}\tHEAD\n'
            else:
                output = f'{oid}\trefs/heads/main\n'
                if any('127.0.0.1' in arg for arg in args):
                    output += f'{oid}\trefs/tags/extra\n'
            return subprocess.CompletedProcess(args, 0, output.encode(), b'')

        with patch('repo_mirror.forgejo_git.subprocess.run', side_effect=run):
            with self.assertRaisesRegex(RuntimeError, 'Forgejo ref readback'):
                snapshot_refs(ForgejoDestination(API(), 'mirror'), 'project',
                              'source/project', source_token='source-secret')
