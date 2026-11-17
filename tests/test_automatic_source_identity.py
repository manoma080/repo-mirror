import unittest

from repo_mirror import sources


class AutomaticSourceIdentityTests(unittest.TestCase):
    def test_verified_rename_uses_canonical_identity(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'old', 'repos': ['oldrepo']}
        metadata = {'id': 42, 'full_name': 'new/newrepo', 'html_url': 'https://github.com/new/newrepo'}
        self.assertEqual(sources.verified_source_identity(source, 'oldrepo', 42, metadata),
                         'https://github.com/new/newrepo')

    def test_pinned_rename_mirrors_to_original_target_and_reports_move(self):
        from unittest.mock import Mock, patch
        from repo_mirror import repository
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'old', 'repos': ['oldrepo'],
                  'destinations': {'oldrepo': {'source_id': 42, 'destination_id': 9}}}
        src, dst = Mock(), Mock()
        src.request.return_value = {'id': 42, 'full_name': 'new/newrepo',
                                    'html_url': 'https://github.com/new/newrepo',
                                    'clone_url': 'https://github.com/new/newrepo.git'}
        marker = sources.source_marker(source, 'oldrepo')
        dst.request.return_value = {'id': 9, 'description': marker}
        with patch('repo_mirror.repository.mirror_git') as mirror, \
             patch('repo_mirror.repository.mirror_releases') as releases:
            relocation = repository.mirror_repository(
                source, 'oldrepo', 'dest', src, dst, 'token')
        # The copy continues from the canonical source into the existing
        # destination; the destination is not renamed by the worker.
        self.assertEqual(mirror.call_args[0][0], 'https://github.com/new/newrepo.git')
        self.assertEqual(releases.call_args[0][7], marker)
        self.assertEqual(relocation['original_name'], 'oldrepo')
        self.assertEqual(relocation['name'], 'newrepo')
        self.assertEqual(relocation['source_id'], 42)
        self.assertEqual(relocation['destination']['id'], 9)
        for call in dst.request.call_args_list:
            if call.args[0] in ('PATCH', 'POST') and len(call.args) > 2 and isinstance(call.args[2], dict):
                self.assertNotIn('name', call.args[2])

    def test_unverified_rename_never_writes_or_mirrors(self):
        from unittest.mock import Mock, patch
        from repo_mirror import repository
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'old', 'repos': ['oldrepo'],
                  'destinations': {'oldrepo': {'source_id': 42, 'destination_id': 9}}}
        for metadata in (
            {'id': 43, 'full_name': 'new/newrepo', 'html_url': 'https://github.com/new/newrepo',
             'clone_url': 'https://github.com/new/newrepo.git'},
            {'id': 42, 'full_name': 'new/newrepo', 'html_url': 'https://evil.example/new/newrepo',
             'clone_url': 'https://github.com/new/newrepo.git'},
            {'id': 42, 'full_name': 'new/newrepo', 'html_url': 'https://github.com/new/newrepo',
             'clone_url': 'https://evil.example/new/newrepo.git'},
        ):
            with self.subTest(metadata=metadata):
                src, dst = Mock(), Mock()
                src.request.return_value = metadata
                dst.request.return_value = {'id': 9, 'description': sources.source_marker(source, 'oldrepo')}
                with patch('repo_mirror.repository.mirror_git') as mirror, \
                     patch('repo_mirror.repository.mirror_releases') as releases, \
                     self.assertRaises(ValueError):
                    repository.mirror_repository(source, 'oldrepo', 'dest', src, dst, 'token')
                self.assertTrue(all(call.args[0] == 'GET' for call in dst.request.call_args_list))
                mirror.assert_not_called()
                releases.assert_not_called()

    def test_mismatch_missing_id_and_cross_origin_are_rejected(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'old', 'repos': ['oldrepo']}
        for metadata in ({'id': 43, 'full_name': 'new/repo', 'html_url': 'https://github.com/new/repo'},
                         {'full_name': 'new/repo', 'html_url': 'https://github.com/new/repo'},
                         {'id': 42, 'full_name': 'new/repo', 'html_url': 'https://evil.example/new/repo'}):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                sources.verified_source_identity(source, 'oldrepo', 42, metadata)
