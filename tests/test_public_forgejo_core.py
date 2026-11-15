import unittest
from unittest.mock import patch
from types import SimpleNamespace

from repo_mirror.forgejo_git import _source, _source_marker, _source_url
from repo_mirror.forgejo_sync import _sync_requested


class PublicForgejoCoreTests(unittest.TestCase):
    def test_host_qualified_identity_maps_to_git_and_provenance(self):
        identity = 'https://code.example.org/alice/project'
        self.assertEqual(_source(identity), ('alice', 'project'))
        self.assertEqual(_source_url(identity), identity + '.git')
        self.assertEqual(_source_marker(identity), 'repo-mirror-source: code.example.org/alice/project')
        self.assertEqual(_source_marker('alice/project'), 'repo-mirror-source: github.com/alice/project')

    def test_forgejo_asset_listing_is_single_unpaginated_api_call(self):
        from repo_mirror.forgejo_source_adapter import PublicForgejoSourceAdapter
        from unittest.mock import Mock
        api = Mock()
        api.request.return_value = [{'id': 4, 'name': 'archive', 'size': 1,
            'browser_download_url': 'https://code.example.org/alice/project/releases/download/v1/archive'}]
        source = PublicForgejoSourceAdapter('https://code.example.org/alice/project', api=api)
        items = source.request('GET', source.base + '/releases/5/assets')
        self.assertEqual(items[0]['state'], 'uploaded')
        api.request.assert_called_once_with('GET', source.base + '/releases/5/assets')
        api.pages.assert_not_called()

    def test_empty_forgejo_target_preflight_is_explicit(self):
        from repo_mirror.repository import validate_source_release_listing, validate_source_latest
        release = {'id': 1, 'tag_name': 'v1', 'name': 'v1', 'body': 'body',
                   'draft': False, 'prerelease': False, 'target_commitish': ''}
        with self.assertRaises(ValueError):
            validate_source_release_listing([release])
        validate_source_release_listing([release], allow_empty_target=True)
        validate_source_latest(release, [release], allow_empty_target=True)

    def test_sync_uses_full_forgejo_descriptor_without_github_credentials(self):
        args = SimpleNamespace(port=3300, owner='local')
        dest = SimpleNamespace(run_pin=None, begin_initialization=lambda *a: False)
        identity = 'https://code.example.org/alice/project'
        with patch('repo_mirror.forgejo_sync.FailureJournal'), \
             patch('repo_mirror.forgejo_sync.SourceAPI') as api, \
             patch('repo_mirror.forgejo_sync.sync_refs') as refs, \
             patch('repo_mirror.forgejo_sync.snapshot_refs', return_value=(None, {}, {})), \
             patch('repo_mirror.forgejo_sync.ForgejoDestinationAdapter') as adapter, \
             patch('repo_mirror.forgejo_sync.fetch_source_latest', return_value=None), \
             patch('repo_mirror.forgejo_sync.validate_source_release_listing'), \
             patch('repo_mirror.forgejo_sync.validate_source_latest'), \
             patch('repo_mirror.forgejo_sync.mirror_releases') as releases:
            api.return_value.pages.return_value = []
            adapter.return_value.target = 'target'
            self.assertEqual(_sync_requested(args, [identity], dest, 'github-secret', 'local-secret', 'signing-secret', '/tmp'), 0)
            self.assertEqual(refs.call_args.kwargs['source_token'], None)
            descriptor, name = releases.call_args.args[:2]
            self.assertEqual(descriptor, {'type': 'forgejo', 'base_url': 'https://code.example.org', 'owner': 'alice', 'repos': ['project']})
            self.assertEqual(name, 'project')
            self.assertEqual(releases.call_args.args[5], 'https://code.example.org/api/v1/repos/alice/project')


if __name__ == '__main__':
    unittest.main()
