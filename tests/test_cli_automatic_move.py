import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from repo_mirror import cli
from repo_mirror.sources import source_marker


class AutomaticMoveTests(unittest.TestCase):
    def test_cli_mirrors_verified_move_to_original_target_and_reports_it(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'old', 'name': 'repo'}
        metadata = {'id': 1, 'full_name': 'new/renamed', 'html_url': 'https://github.com/new/renamed',
                    'clone_url': 'https://github.com/new/renamed.git', 'default_branch': 'main'}
        src, dst = Mock(), Mock()
        src.request.return_value = metadata
        dst.request.return_value = {'id': 2, 'description': source_marker(source, 'repo'), 'default_branch': 'main'}
        env = {'MIRROR_TIMESTAMP_SECRET': 'fixture-signing-secret-0123456789abcdef', 'GH_TOKEN': 'token', 'DESTINATION_OWNER': 'dest'}
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            original = {'destinations': {'main': {'account_type': 'personal'}},
                        'repositories': [{'source': {'url': 'https://github.com/old/repo', 'id': 1},
                                          'mirrors': {'main': {'name': 'copy', 'id': 2}}}]}
            config.write_text(json.dumps(original))
            stderr = Mock()
            with patch.dict('os.environ', env, clear=True), patch.object(cli, 'SourceAPI', return_value=src), \
                 patch.object(cli, 'API', side_effect=[Mock(), dst]), \
                 patch('repo_mirror.repository.mirror_git') as git, \
                 patch('repo_mirror.repository.mirror_releases') as releases, \
                 patch('sys.stderr', stderr):
                cli.main(['--config', str(config)])
            # The copy runs against the canonical source and the configured
            # destination is kept; configuration migration is deferred.
            git.assert_called_once()
            self.assertEqual(git.call_args[0][0], 'https://github.com/new/renamed.git')
            releases.assert_called_once()
            self.assertEqual(json.loads(config.read_text()), original)
        self.assert_no_destination_rename(dst)

    @staticmethod
    def assert_no_destination_rename(dst):
        for call in dst.request.call_args_list:
            if call.args[0] in ('PATCH', 'POST') and isinstance(call.args[2] if len(call.args) > 2 else None, dict):
                assert 'name' not in call.args[2], 'worker must not rename the destination'

    def test_cli_retains_preexisting_signed_legacy_identity_after_another_move(self):
        from repo_mirror.migration_attestation import github_claim, sign
        secret = 'fixture-signing-secret-0123456789abcdef'
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'middle', 'name': 'repo',
                  'mirror': {'name': 'copy', 'source_id': 1, 'destination_id': 2,
                      'legacy': {'source_owner': 'old', 'source_name': 'original', 'destination_name': 'old-copy'}}}
        signature = sign(github_claim(source, 'repo', 'dest'), secret)
        named_config = {
            'destinations': {'main': {'account_type': 'personal'}},
            'repositories': [{
                'source': {'url': 'https://github.com/middle/repo', 'id': 1},
                'mirrors': {'main': {
                    'name': 'copy', 'id': 2,
                    'legacy': {'source_owner': 'old', 'source_name': 'original', 'destination_name': 'old-copy',
                               'signature': signature},
                }},
            }],
        }
        original = {**source, 'owner': 'old'}
        marker = source_marker(original, 'original')
        src, dst = Mock(), Mock()
        src.request.return_value = {'id': 1, 'full_name': 'new/repo', 'html_url': 'https://github.com/new/repo',
                                    'clone_url': 'https://github.com/new/repo.git', 'default_branch': 'main'}
        dst.request.return_value = {'id': 2, 'description': marker, 'default_branch': 'main'}
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps(named_config))
            with patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': secret, 'GH_TOKEN': 'token', 'DESTINATION_OWNER': 'dest'}, clear=True), \
                 patch.object(cli, 'SourceAPI', return_value=src), patch.object(cli, 'API', side_effect=[Mock(), dst]), \
                 patch('repo_mirror.repository.mirror_git'), patch('repo_mirror.repository.mirror_releases') as releases:
                cli.main(['--config', str(config)])
        # The original signed identity survives the second move: the release
        # engine still sees the historical marker and journal names.
        releases.assert_called_once()
        self.assertEqual(releases.call_args[0][7], marker)
        self.assertEqual(releases.call_args[1]['journal_name'], 'original')
        self.assert_no_destination_rename(dst)
