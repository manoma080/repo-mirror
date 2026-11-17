import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

from repo_mirror import cli


class GitHubMigrationEntrypointTests(unittest.TestCase):
    def test_batch_reports_relocation_without_publishing_configuration(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = Path(tmp_dir) / 'config.json'
            original = {'destination_account_type': 'personal', 'sources': [
                {'type': 'github', 'base_url': 'https://github.com', 'owner': 'old',
                 'repos': ['repo'], 'destinations': {'repo': {'name': 'copy', 'source_id': 1, 'destination_id': 2}}}]}
            config.write_text(json.dumps(original), encoding='utf-8')
            dst = Mock()
            env = {'MIRROR_TIMESTAMP_SECRET': 's' * 32, 'GH_TOKEN': 'token', 'DESTINATION_OWNER': 'dest'}
            stderr = io.StringIO()
            relocation = {'source_origin': 'https://github.com', 'source_id': 1,
                          'original_owner': 'old', 'original_name': 'repo',
                          'owner': 'new', 'name': 'renamed',
                          'destination': {'profile': None, 'owner': 'dest',
                                          'name': 'copy', 'id': 2}}
            with patch.dict('os.environ', env, clear=True), patch.object(cli, 'SourceAPI'), \
                 patch.object(cli, 'API', side_effect=[Mock(), dst]), \
                 patch.object(cli, 'get_existing', return_value=None), \
                 patch.object(cli, 'mirror_repository', return_value=relocation) as mirror, \
                 redirect_stderr(stderr):
                result = cli.main(['--config', str(config)])
            mirror.assert_called_once()
            self.assertIn('source relocation detected; migration deferred', stderr.getvalue())
            # The copy counts as a success; the rename and configuration update
            # are a separate transaction that has not run.
            self.assertEqual(json.loads(config.read_text(encoding='utf-8')), original)
            self.assertEqual(result[0][0], 1)
            self.assertEqual(result[0][1]['name'], 'renamed')
            dst.request.assert_not_called()
