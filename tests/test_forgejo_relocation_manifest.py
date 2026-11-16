import unittest
from repo_mirror.forgejo_sync import parse_manifest
from repo_mirror.migration_attestation import forgejo_claim, sign

def signed(item):
    item['signature'] = sign(forgejo_claim(item, 3300, 'dest'), 'fixture-signing-secret-0123456789abcdef')
    return item


class ForgejoRelocationManifestTests(unittest.TestCase):
    def test_scheduler_and_launcher_accept_same_pinned_manifest(self):
        from repo_mirror.forgejo_schedule import validate_sources
        from scheduler.auto_update import valid_manifest
        item = {'url': 'https://git.example/new/repo', 'source_id': 11, 'destination_id': 22,
                'destination_name': 'new-repo', 'legacy_url': 'https://git.example/old/repo',
                'legacy_destination_name': 'old-repo', 'signature': 'a' * 64}
        validate_sources([item])
        self.assertTrue(valid_manifest([item]))
        self.assertFalse(valid_manifest([{**item, 'destination_id': True}]))

    def test_real_destination_accepts_original_provenance_after_relocation(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from repo_mirror.forgejo_git import ForgejoDestination
        from repo_mirror.forgejo_sync import _sync_requested

        identity = 'https://git.example/new/repo'
        old = 'https://git.example/old/repo'
        entry = parse_manifest([signed({'url': identity, 'source_id': 11, 'destination_id': 22,
                                 'destination_name': 'new-repo', 'legacy_url': old,
                                 'legacy_destination_name': 'old-repo', 'signature': 'a' * 64})])[0]
        repo = {'id': 22, 'full_name': 'dest/new-repo', 'private': True, 'has_actions': False,
                'description': 'repo-mirror-source: git.example/old/repo [repo-mirror:initialized:v1]'}
        api = Mock()
        api.request.side_effect = lambda method, path, payload=None: dict(repo) if method == 'GET' else self.fail('unexpected write')
        dest = ForgejoDestination(api, 'dest')
        src = Mock()
        src.request.return_value = {'id': 11, 'full_name': 'new/repo', 'clone_url': identity + '.git'}
        src.pages.return_value = []
        adapter = Mock(target='http://127.0.0.1:3300/api/v1/repos/dest/new-repo')
        with patch('repo_mirror.forgejo_sync.PublicForgejoSourceAdapter', return_value=src), \
             patch('repo_mirror.forgejo_sync.ForgejoDestinationAdapter', return_value=adapter), \
             patch('repo_mirror.forgejo_sync.validate_source_release_listing'), \
             patch('repo_mirror.forgejo_sync.validate_source_latest'), \
             patch('repo_mirror.forgejo_sync.fetch_source_latest'), \
             patch('repo_mirror.forgejo_sync.sync_refs') as sync, \
             patch('repo_mirror.forgejo_sync.snapshot_refs', return_value=(None, {}, {})) as snapshot, \
             patch('repo_mirror.forgejo_sync.mirror_releases'):
            result = _sync_requested(SimpleNamespace(port=3300, owner='dest'), [identity], dest,
                                     None, 'token', 'fixture-signing-secret-0123456789abcdef', '/tmp', mappings={identity: entry})
        self.assertEqual(result, 0)
        self.assertEqual(sync.call_args.args[2], identity)
        self.assertEqual(sync.call_args.kwargs['recovery_identity'], old)
        self.assertTrue(all(c.args[2] == identity and c.kwargs['recovery_identity'] == old
                            for c in snapshot.call_args_list))
        self.assertEqual(repo['description'], 'repo-mirror-source: git.example/old/repo [repo-mirror:initialized:v1]')

    def test_worker_uses_pinned_destination_and_original_recovery_binding(self):
        from unittest.mock import Mock, patch
        from types import SimpleNamespace
        from repo_mirror.forgejo_sync import _sync_requested
        identity = 'https://git.example/new/repo'
        entry = parse_manifest([signed({'url': identity, 'source_id': 11, 'destination_id': 22,
                                 'destination_name': 'new-repo', 'legacy_url': 'https://git.example/old/repo',
                                 'legacy_destination_name': 'old-repo', 'signature': 'a' * 64})])[0]
        src = Mock()
        src.request.return_value = {'id': 11, 'full_name': 'new/repo', 'clone_url': identity + '.git'}
        src.pages.return_value = []
        dest = Mock()
        dest.begin_initialization.return_value = False
        dest.run_pin = (22, 'marker')
        adapter = Mock()
        adapter.target = 'http://127.0.0.1:3300/api/v1/repos/dest/new-repo'
        args = SimpleNamespace(port=3300, owner='dest')
        with patch('repo_mirror.forgejo_sync.PublicForgejoSourceAdapter', return_value=src), \
             patch('repo_mirror.forgejo_sync.ForgejoDestinationAdapter', return_value=adapter), \
             patch('repo_mirror.forgejo_sync.validate_source_release_listing'), \
             patch('repo_mirror.forgejo_sync.validate_source_latest'), \
             patch('repo_mirror.forgejo_sync.fetch_source_latest'), \
             patch('repo_mirror.forgejo_sync.sync_refs'), \
             patch('repo_mirror.forgejo_sync.snapshot_refs', return_value=(None, {}, {})), \
             patch('repo_mirror.forgejo_sync.mirror_releases') as releases:
            result = _sync_requested(args, [identity], dest, None, 'token', 'fixture-signing-secret-0123456789abcdef', '/tmp',
                                     mappings={identity: entry})
        self.assertEqual(result, 0)
        dest.begin_initialization.assert_called_once_with('new-repo', entry['legacy_identity'], expected_id=22)
        call = releases.call_args
        self.assertEqual(call.args[7], '[repo-mirror: forgejo:https://git.example/old/repo]')
        self.assertEqual(call.kwargs['recovery_target'], 'http://127.0.0.1:3300/api/v1/repos/dest/old-repo')
        self.assertEqual(call.kwargs['journal_source']['owner'], 'old')
        self.assertEqual(call.kwargs['journal_name'], 'repo')

    def test_worker_rejects_wrong_source_before_destination_write(self):
        from unittest.mock import Mock, patch
        from types import SimpleNamespace
        from repo_mirror.forgejo_sync import _sync_requested
        identity = 'https://git.example/new/repo'
        entry = parse_manifest([signed({'url': identity, 'source_id': 11, 'destination_id': 22,
                                 'destination_name': 'new-repo', 'legacy_url': 'https://git.example/old/repo',
                                 'legacy_destination_name': 'old-repo', 'signature': 'a' * 64})])[0]
        source = Mock()
        source.request.return_value = {'id': 99, 'full_name': 'new/repo', 'clone_url': identity + '.git'}
        destination = Mock()
        with patch('repo_mirror.forgejo_sync.PublicForgejoSourceAdapter', return_value=source):
            result = _sync_requested(SimpleNamespace(port=3300, owner='dest'), [identity], destination,
                                     None, 'token', 'fixture-signing-secret-0123456789abcdef', '/tmp', mappings={identity: entry})
        self.assertEqual(result, 1)
        destination.begin_initialization.assert_not_called()

    def test_live_metadata_must_match_pin_name_and_clone(self):
        from repo_mirror.forgejo_sync import verify_source_pin
        identity = 'https://git.example/new/repo'
        good = {'id': 11, 'full_name': 'new/repo', 'clone_url': identity + '.git'}
        verify_source_pin(identity, good, 11)
        for key, value in [('id', 12), ('full_name', 'attacker/repo'), ('clone_url', 'https://evil.example/new/repo.git')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                verify_source_pin(identity, {**good, key: value}, 11)

    def test_pinned_mapping_and_legacy_string(self):
        mapping = {'url': 'https://git.example/new/repo', 'source_id': 11, 'destination_id': 22,
                   'destination_name': 'new-repo', 'legacy_url': 'https://git.example/old/repo',
                   'legacy_destination_name': 'old-repo', 'signature': 'a' * 64}
        result = parse_manifest([mapping, 'https://git.example/other/other'])
        self.assertEqual(result[0]['identity'], 'https://git.example/new/repo')
        self.assertEqual(result[0]['destination_name'], 'new-repo')
        self.assertEqual(result[1]['destination_name'], 'other')

    def test_rejects_untrusted_mapping_and_destination_collision(self):
        mapping = {'url': 'https://git.example/new/repo', 'source_id': 11, 'destination_id': 22,
                   'destination_name': 'new-repo', 'legacy_url': 'https://git.example/old/repo',
                   'legacy_destination_name': 'old-repo', 'signature': 'a' * 64}
        for key, val in [('source_id', True), ('destination_id', 0),
                         ('destination_name', '../escape'), ('legacy_url', 'https://elsewhere.example/old/repo')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                parse_manifest([{**mapping, key: val}])
        with self.assertRaises(ValueError):
            parse_manifest([mapping, 'https://git.example/another/new-repo'])
        with self.assertRaises(ValueError):
            parse_manifest([mapping, 'https://git.example/another/old-repo'])
