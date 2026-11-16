import unittest
from unittest.mock import patch

from repo_mirror.sources import validate_source
from repo_mirror.repository import find_tag_releases, draft_identity_signature, IDENTITY_SENTINEL


class LegacyBindingTests(unittest.TestCase):
    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'fixture-signing-secret-0123456789abcdef'})
    def test_forgejo_release_interruption_resumes_after_destination_move(self):
        from repo_mirror.forgejo_destination_adapter import ForgejoDestinationAdapter
        from repo_mirror.repository import mirror_releases
        from test_forgejo_space_body_integration import Source, ForgejoAPI, SOURCE, UP
        api = ForgejoAPI()
        source = Source(body='notes')
        old_adapter = ForgejoDestinationAdapter('team/project', 'token', client=api)
        api.fail_upload = True
        with self.assertRaisesRegex(RuntimeError, 'simulated interruption'):
            mirror_releases(SOURCE, 'repo', 'team', source, old_adapter, UP, old_adapter.target,
                            'old-marker', initializing=False)
        old_target = old_adapter.target
        api.base = '/api/v1/repos/team/renamed'
        new_adapter = ForgejoDestinationAdapter('team/renamed', 'token', client=api)
        mirror_releases(SOURCE, 'repo', 'team', source, new_adapter, UP, new_adapter.target,
                        'old-marker', initializing=False, recovery_target=old_target)
        self.assertNotIn('[repo-mirror:pending:v3]', api.release['body'])
        self.assertEqual(list(api.assets.values()), [('package', source.data)])

    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'fixture-signing-secret-0123456789abcdef'})
    def test_interrupted_release_resumes_with_original_journal_and_signature(self):
        import tempfile
        from pathlib import Path
        from repo_mirror.repository import mirror_releases
        from repo_mirror.sources import source_marker
        from repo_mirror.journal import FailureJournal
        from test_reconcile_assets import Source, Destination, SOURCE
        old_source = dict(SOURCE)
        new_source = {**old_source, 'owner': 'bob'}
        old_target = 'https://api.github.com/repos/dest/mirror-alice-demo'
        new_target = 'https://api.github.com/repos/dest/mirror-bob-demo'
        upstream_old = 'https://api.github.com/repos/alice/demo'
        upstream_new = 'https://api.github.com/repos/bob/demo'
        marker = source_marker(old_source, 'demo')
        dest = Destination()
        with tempfile.TemporaryDirectory() as directory:
            journal = FailureJournal(Path(directory) / 'journal')
            with self.assertRaisesRegex(RuntimeError, 'release asset uploads failed'):
                mirror_releases(old_source, 'demo', 'dest', Source(), dest, upstream_old, old_target,
                                marker, initializing=False, journal=journal)
            self.assertTrue(journal.failures)
            dest.fail = False
            mirror_releases(new_source, 'demo', 'dest', Source(), dest, upstream_new, new_target,
                            marker, initializing=False, journal=journal, recovery_target=old_target,
                            journal_source=old_source, journal_name='demo')
            self.assertFalse(journal.failures)
            self.assertEqual(dest.assets['changed'], b'new')

    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'fixture-signing-secret-0123456789abcdef'})
    def test_legacy_binding_never_used_when_either_pin_mismatches(self):
        from unittest.mock import Mock
        from repo_mirror.repository import mirror_repository
        from repo_mirror.sources import source_marker
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'new', 'repos': ['repo'],
                  'destinations': {'repo': {'name': 'mirror-new', 'source_id': 1, 'destination_id': 2,
                     'legacy': {'source_owner': 'old', 'source_name': 'repo', 'destination_name': 'mirror-old'}}}}
        from repo_mirror.migration_attestation import github_claim, sign
        source['destinations']['repo']['legacy']['signature'] = sign(github_claim(source, 'repo', 'dest'), 'fixture-signing-secret-0123456789abcdef')
        for source_id, destination_id in ((3, 2), (1, 3)):
            src, dst = Mock(), Mock()
            src.request.return_value = {'id': source_id, 'clone_url': 'https://github.com/new/repo.git'}
            dst.request.return_value = {'id': destination_id, 'description': source_marker({**source, 'owner': 'old'}, 'repo')}
            with self.subTest(source_id=source_id, destination_id=destination_id), \
                 patch('repo_mirror.repository.mirror_git') as git, \
                 patch('repo_mirror.repository.mirror_releases') as releases, self.assertRaises(ValueError):
                mirror_repository(source, 'repo', 'dest', src, dst, 'token')
            git.assert_not_called()
            releases.assert_not_called()
            self.assertTrue(all(c.args[0] == 'GET' for c in dst.request.call_args_list))

    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'fixture-signing-secret-0123456789abcdef'})
    def test_runner_passes_legacy_binding_only_after_both_pins_match(self):
        from unittest.mock import Mock
        from repo_mirror.repository import mirror_repository
        from repo_mirror.sources import source_marker
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'new', 'repos': ['repo'],
                  'destinations': {'repo': {'name': 'mirror-new', 'source_id': 1, 'destination_id': 2,
                     'legacy': {'source_owner': 'old', 'source_name': 'repo', 'destination_name': 'mirror-old'}}}}
        from repo_mirror.migration_attestation import github_claim, sign
        source['destinations']['repo']['legacy']['signature'] = sign(github_claim(source, 'repo', 'dest'), 'fixture-signing-secret-0123456789abcdef')
        src, dst = Mock(), Mock()
        src.request.return_value = {'id': 1, 'clone_url': 'https://github.com/new/repo.git'}
        dst.request.return_value = {'id': 2, 'description': source_marker({**source, 'owner': 'old'}, 'repo')}
        with patch('repo_mirror.repository.mirror_git'), patch('repo_mirror.repository.mirror_releases') as releases:
            mirror_repository(source, 'repo', 'dest', src, dst, 'token')
        args, kwargs = releases.call_args
        self.assertEqual(args[7], source_marker({**source, 'owner': 'old'}, 'repo'))
        self.assertEqual(kwargs['recovery_target'], 'https://api.github.com/repos/dest/mirror-old')
        self.assertEqual(kwargs['journal_source']['owner'], 'old')
        self.assertEqual(kwargs['journal_name'], 'repo')

    def test_relocation_mapping_with_discovery_all_is_rejected(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'new', 'repos': 'all',
                  'destinations': {'repo': {'source_id': 1, 'destination_id': 2,
                      'legacy': {'source_owner': 'old', 'source_name': 'repo', 'destination_name': 'mirror-old'}}}}
        with self.assertRaises(ValueError):
            validate_source(source)

    def test_legacy_binding_requires_both_repository_pins(self):
        base = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'new', 'repos': ['repo'],
                'destinations': {'repo': {'source_id': 1, 'destination_id': 2,
                    'legacy': {'source_owner': 'old', 'source_name': 'repo', 'destination_name': 'mirror-old'}}}}
        base['destinations']['repo']['legacy']['signature'] = 'a' * 64
        validate_source(base)
        for key in ('source_id', 'destination_id'):
            source = {**base, 'destinations': {'repo': dict(base['destinations']['repo'])}}
            source['destinations']['repo'].pop(key)
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_source(source)

    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'fixture-signing-secret-0123456789abcdef'})
    def test_legacy_draft_identity_binds_original_target_and_release(self):
        old_marker = '[repo-mirror: github:https://github.com/old/repo]'
        old_target = 'https://api.github.com/repos/dest/mirror-old'
        current_target = 'https://api.github.com/repos/dest/mirror-new'
        signature = draft_identity_signature('fixture-signing-secret-0123456789abcdef', old_marker, 'v1', old_target, 42)
        release = {'id': 42, 'draft': True, 'tag_name': 'untagged-42',
                   'body': 'notes\n' + IDENTITY_SENTINEL + signature}
        self.assertEqual(find_tag_releases(None, current_target, 'v1', releases=[release], marker=old_marker,
                                           recovery_target=old_target), [release])
        with self.assertRaises(ValueError):
            find_tag_releases(None, current_target, 'v1', releases=[release], marker=old_marker,
                              recovery_target='https://api.github.com/repos/dest/wrong')
        release['id'] = 43
        with self.assertRaises(ValueError):
            find_tag_releases(None, current_target, 'v1', releases=[release], marker=old_marker,
                              recovery_target=old_target)
