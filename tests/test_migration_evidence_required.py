"""Regression: matching current pins alone cannot authorize historical aliases."""
import unittest
from unittest.mock import Mock, patch

from repo_mirror.repository import mirror_repository
from repo_mirror.sources import source_marker


class MigrationEvidenceRequiredTests(unittest.TestCase):
    def test_matching_current_ids_do_not_authenticate_legacy_alias(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'new', 'name': 'repo',
                  'mirror': {'name': 'mirror-new', 'source_id': 1, 'destination_id': 2,
                      'legacy': {'source_owner': 'old', 'source_name': 'repo', 'destination_name': 'mirror-old'}}}
        src, dst = Mock(), Mock()
        src.request.return_value = {'id': 1, 'clone_url': 'https://github.com/new/repo.git'}
        dst.request.return_value = {'id': 2, 'description': source_marker({**source, 'owner': 'old'}, 'repo')}
        with patch('repo_mirror.repository.mirror_git') as git, \
             patch('repo_mirror.repository.mirror_releases') as releases:
            with self.assertRaises(ValueError):
                mirror_repository(source, 'repo', 'dest', src, dst, 'token')
            git.assert_not_called()
            releases.assert_not_called()
            self.assertTrue(all(call.args[0] == 'GET' for call in dst.request.call_args_list))
