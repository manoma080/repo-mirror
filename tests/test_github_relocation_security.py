import unittest
from unittest.mock import Mock, patch

from repo_mirror.repository import mirror_repository
from repo_mirror.sources import source_marker


def setup_mirror():
    source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'new', 'name': 'repo',
              'mirror': {'name': 'mirror-new', 'source_id': 1, 'destination_id': 2,
                  'legacy': {'source_owner': 'old', 'source_name': 'repo', 'destination_name': 'mirror-old'}}}
    from repo_mirror.migration_attestation import github_claim, sign
    source['mirror']['legacy']['signature'] = sign(github_claim(source, 'repo', 'dest'), 'fixture-signing-secret-0123456789abcdef')
    src, dst = Mock(), Mock()
    src.request.return_value = {'id': 1, 'clone_url': 'https://github.com/new/repo.git', 'default_branch': 'main'}
    old_marker = source_marker({**source, 'owner': 'old'}, 'repo')
    dst.request.return_value = {'id': 2, 'description': old_marker, 'default_branch': 'main'}
    return source, src, dst, old_marker


@patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'fixture-signing-secret-0123456789abcdef'})
class RelocationSecurityTests(unittest.TestCase):
    def test_identity_changes_are_blocked_at_each_write_boundary(self):
        for side in ('source', 'destination'):
            for change_on in (2, 3, 4):
                with self.subTest(side=side, change_on=change_on):
                    source, src, dst, _ = setup_mirror()
                    source_metadata, destination_metadata = src.request.return_value, dst.request.return_value
                    calls = {'source': 0, 'destination': 0}
                    def source_request(method, url, *args):
                        calls['source'] += 1
                        return {**source_metadata, 'id': 99 if side == 'source' and calls['source'] >= change_on else 1}
                    def destination_request(method, url, *args):
                        if method == 'GET':
                            calls['destination'] += 1
                        return {**destination_metadata, 'id': 99 if side == 'destination' and calls['destination'] >= change_on else 2}
                    src.request.side_effect = source_request
                    dst.request.side_effect = destination_request
                    with patch('repo_mirror.repository.mirror_git') as git, patch('repo_mirror.repository.mirror_releases') as releases:
                        with self.assertRaisesRegex(ValueError, 'repository id mismatch'):
                            mirror_repository(source, 'repo', 'dest', src, dst, 'token')
                    releases.assert_not_called()
                    if change_on <= 3:
                        git.assert_not_called()
                    if change_on == 2:
                        self.assertTrue(all(c.args[0] == 'GET' for c in dst.request.call_args_list))

    def test_legacy_repository_retains_historical_provenance(self):
        source, src, dst, old_marker = setup_mirror()
        with patch('repo_mirror.repository.mirror_git'), patch('repo_mirror.repository.mirror_releases') as releases:
            mirror_repository(source, 'repo', 'dest', src, dst, 'token')
        self.assertEqual(releases.call_args.args[7], old_marker)
        self.assertFalse(any(c.args[0] == 'PATCH' and c.args[2].get('description') != old_marker
                             for c in dst.request.call_args_list))
