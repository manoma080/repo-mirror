import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from repo_mirror import cli
from repo_mirror.sources import source_marker


SOURCE = {
    'type': 'github', 'base_url': 'https://github.com', 'owner': 'new', 'repos': ['repo'],
    'destinations': {'repo': {'name': 'mirror-new', 'source_id': 1, 'destination_id': 2,
        'legacy': {'source_owner': 'old', 'source_name': 'old-repo', 'destination_name': 'mirror-old'}}},
}
OLD_MARKER = source_marker({**SOURCE, 'owner': 'old'}, 'old-repo')


def run_cli(tmp_path, marker):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'destination_account_type': 'personal', 'sources': [SOURCE]}))
    src = Mock()
    src.request.return_value = {'id': 1, 'clone_url': 'https://github.com/new/repo.git'}
    dst = Mock()
    dst.request.return_value = {'id': 2, 'description': marker, 'default_branch': 'main'}
    env = {'MIRROR_TIMESTAMP_SECRET': 'test-secret', 'GH_TOKEN': 'token', 'DESTINATION_OWNER': 'dest'}
    with patch.dict('os.environ', env, clear=True), \
         patch.object(cli, 'SourceAPI', return_value=src), \
         patch.object(cli, 'API', side_effect=[Mock(), dst]), \
         patch('repo_mirror.repository.mirror_git') as git, \
         patch('repo_mirror.repository.mirror_releases') as releases:
        try:
            cli.main(['--config', str(config)])
            error = None
        except RuntimeError as exc:
            error = exc
    return error, src, dst, git, releases


class CliRelocationTests(unittest.TestCase):
    def test_cli_accepts_legacy_marker_and_runs_pinned_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            error, src, dst, git, releases = run_cli(Path(directory), OLD_MARKER)
        self.assertIsNone(error)
        git.assert_called_once()
        releases.assert_called_once()
        self.assertEqual(releases.call_args.args[7], OLD_MARKER)
        self.assertEqual(releases.call_args.kwargs['recovery_target'], 'https://api.github.com/repos/dest/mirror-old')
        self.assertTrue(src.request.called)
        self.assertGreaterEqual(dst.request.call_count, 2)

    def test_cli_rejects_incompatible_marker_before_repository_work(self):
        with tempfile.TemporaryDirectory() as directory:
            error, src, dst, git, releases = run_cli(Path(directory), '[repo-mirror: github:https://github.com/wrong/repo]')
        self.assertIsNotNone(error)
        git.assert_not_called()
        releases.assert_not_called()
        src.request.assert_not_called()
        self.assertTrue(all(call.args[0] == 'GET' for call in dst.request.call_args_list))
