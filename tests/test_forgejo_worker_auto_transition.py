import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

from repo_mirror.forgejo_sync import main

OLD = 'https://git.example/old/repo'
NEW = 'https://git.example/new/renamed'
SECRET = 'fixture-signing-secret-0123456789abcdef'


def manifest(source, mirror):
    return {'destinations': {'local': {}}, 'repositories': [
        {'source': source, 'mirrors': {'local': mirror}}]}


class WorkerAutomaticTransitionTests(unittest.TestCase):
    def run_worker(self, config, metadata):
        source = Mock()
        source.request.return_value = metadata
        source.pages.return_value = []
        dest = Mock(run_pin=(22, 'marker'))
        dest.begin_initialization.return_value = False
        adapter = Mock(target='http://127.0.0.1:3000/api/v1/repos/dest/copy')
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'manifest.json'
            path.write_text(json.dumps(config))
            token = root / 'token'
            token.write_text('token')
            token.chmod(0o600)
            key = root / 'secret'
            key.write_text(SECRET)
            key.chmod(0o600)
            journal = root / 'journal'
            journal.mkdir(mode=0o700)
            argv = ['--config', str(path), '--source', config['repositories'][0]['source']['url'],
                    '--token-file', str(token), '--timestamp-secret-file', str(key),
                    '--journal-dir', str(journal), '--owner', 'dest', '--port', '3000']
            with patch('repo_mirror.forgejo_sync.PublicForgejoSourceAdapter', return_value=source), \
                 patch('repo_mirror.forgejo_sync.ForgejoDestination', return_value=dest), \
                 patch('repo_mirror.forgejo_sync.ForgejoDestinationAdapter', return_value=adapter), \
                 patch('repo_mirror.forgejo_sync.validate_source_release_listing'), \
                 patch('repo_mirror.forgejo_sync.validate_source_latest'), \
                 patch('repo_mirror.forgejo_sync.fetch_source_latest'), \
                 patch('repo_mirror.forgejo_sync.sync_refs') as sync, \
                 patch('repo_mirror.forgejo_sync.snapshot_refs', return_value=(None, {}, {})) as snapshot, \
                 patch('repo_mirror.forgejo_sync.mirror_releases') as releases, \
                 redirect_stderr(stderr):
                result = main(argv)
            self.assertEqual(json.loads(path.read_text()), config)
        sync.snapshots = snapshot
        return result, dest, sync, releases, stderr.getvalue()

    def relocation(self, stderr):
        lines = [line for line in stderr.splitlines() if line.startswith('FORGEJO_RELOCATION: ')]
        self.assertEqual(len(lines), 1, stderr)
        return json.loads(lines[0][len('FORGEJO_RELOCATION: '):])

    def test_initial_forgejo_worker_uses_origin_for_git(self):
        config = manifest({'url': OLD, 'id': 11}, {'name': 'copy', 'id': 22})
        metadata = {'id': 11, 'full_name': 'old/repo', 'clone_url': OLD + '.git'}
        result, dest, sync, releases, stderr = self.run_worker(config, metadata)
        self.assertEqual(result, 0, stderr)
        self.assertEqual(sync.call_args.args[2], OLD)
        self.assertIsNone(sync.call_args.kwargs['source_token'])
        self.assertEqual(len(sync.snapshots.call_args_list), 2)
        self.assertTrue(all(call.args[2] == OLD for call in sync.snapshots.call_args_list))
        dest.begin_initialization.assert_called_once_with('copy', OLD, expected_id=22)
        self.assertEqual(releases.call_args.kwargs['journal_identity'],
                         ('https://git.example', 11, 'http://127.0.0.1:3000', 22))

    def test_verified_move_mirrors_into_existing_destination_and_reports_it(self):
        config = manifest({'url': OLD, 'id': 11}, {'name': 'copy', 'id': 22})
        metadata = {'id': 11, 'full_name': 'new/renamed', 'html_url': NEW, 'clone_url': NEW + '.git'}
        result, dest, sync, releases, stderr = self.run_worker(config, metadata)
        self.assertEqual(result, 0, stderr)
        # The worker keeps the configured destination and the original binding.
        dest.begin_initialization.assert_called_once_with('copy', OLD, expected_id=22)
        self.assertEqual(sync.call_args.args[2], NEW)
        self.assertEqual(len(sync.snapshots.call_args_list), 2)
        self.assertTrue(all(c.args[2] == NEW and c.kwargs['recovery_identity'] == OLD
                            for c in sync.snapshots.call_args_list))
        self.assertEqual(sync.call_args.kwargs['recovery_identity'], OLD)
        call = releases.call_args
        self.assertEqual(call.args[7], '[repo-mirror: forgejo:https://git.example/old/repo]')
        self.assertEqual(call.kwargs['recovery_target'], 'http://127.0.0.1:3000/api/v1/repos/dest/copy')
        self.assertEqual(call.kwargs['journal_source']['owner'], 'old')
        self.assertEqual(call.kwargs['journal_name'], 'repo')
        self.assertEqual(call.kwargs['journal_identity'],
                         ('https://git.example', 11, 'http://127.0.0.1:3000', 22))
        self.assertEqual(self.relocation(stderr),
                         {'url': OLD, 'canonical': NEW, 'source_id': 11, 'destination_id': 22})

    def test_move_without_destination_pin_fails_before_destination_writes(self):
        config = manifest({'url': OLD, 'id': 11}, {'name': 'copy'})
        metadata = {'id': 11, 'full_name': 'new/renamed', 'html_url': NEW, 'clone_url': NEW + '.git'}
        result, dest, sync, releases, stderr = self.run_worker(config, metadata)
        self.assertEqual(result, 1)
        dest.begin_initialization.assert_not_called()
        dest.complete_initialization.assert_not_called()
        sync.assert_not_called()
        releases.assert_not_called()
        self.assertNotIn('FORGEJO_RELOCATION: ', stderr)

    def test_nondefault_port_move_rejects_untrusted_metadata_before_writes(self):
        old = 'https://git.example:8443/old/repo'
        new = 'https://git.example:8443/new/renamed'
        config = manifest({'url': old, 'id': 11}, {'name': 'copy', 'id': 22})
        good = {'id': 11, 'full_name': 'new/renamed', 'html_url': new, 'clone_url': new + '.git'}
        variants = [
            {'id': 99},
            {'html_url': 'https://git.example:9443/new/renamed'},
            {'html_url': 'https://git.example/new/renamed'},
            {'html_url': 'https://evil.example:8443/new/renamed'},
            {'html_url': 'https://user@git.example:8443/new/renamed'},
            {'html_url': new + '?redirect=1'},
            {'clone_url': 'https://git.example:9443/new/renamed.git'},
            {'full_name': 'other/renamed'},
        ]
        for changes in variants:
            with self.subTest(changes=changes):
                result, dest, sync, releases, stderr = self.run_worker(config, {**good, **changes})
                self.assertEqual(result, 1)
                dest.begin_initialization.assert_not_called()
                sync.assert_not_called()
                releases.assert_not_called()
                self.assertNotIn('FORGEJO_RELOCATION: ', stderr)

    def test_changed_source_id_fails_before_destination_writes(self):
        config = manifest({'url': OLD, 'id': 11}, {'name': 'copy', 'id': 22})
        metadata = {'id': 99, 'full_name': 'new/renamed', 'html_url': NEW, 'clone_url': NEW + '.git'}
        result, dest, sync, releases, stderr = self.run_worker(config, metadata)
        self.assertEqual(result, 1)
        dest.begin_initialization.assert_not_called()
        sync.assert_not_called()
        releases.assert_not_called()
        self.assertNotIn('FORGEJO_RELOCATION: ', stderr)
