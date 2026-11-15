import unittest
from unittest.mock import patch

from repo_mirror.github_api import SourceAPI
from repo_mirror.repository import mirror_releases


class GitHubSourceAssetBindingTests(unittest.TestCase):
    def test_github_release_path_uses_repository_scoped_adapter(self):
        source = {'type': 'github', 'owner': 'acme'}
        api = SourceAPI('test-token')
        with patch.object(api, 'pages', side_effect=RuntimeError('unwrapped source')), patch('repo_mirror.forgejo_source_adapter.ForgejoSourceAdapter.pages', side_effect=RuntimeError('reached adapter')) as pages:
            with self.assertRaisesRegex(RuntimeError, 'reached adapter'):
                mirror_releases(source, 'project', 'dest', api, object(),
                                'https://api.github.com/repos/acme/project', 'target', 'marker', initializing=True)
        pages.assert_called_once()
