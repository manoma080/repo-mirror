import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from repo_mirror import cli

from repo_mirror.gitlab_onboarding_github import publish_binding


class Store:
    def __init__(self, data):
        self.data = data
        self.sha = 'a'

    def read(self):
        return self.data, self.sha

    def publish(self, sha, data):
        if sha != self.sha:
            raise ValueError('conflict')
        self.data = data
        self.sha = 'b'


class GitLabGroupPublicationTests(unittest.TestCase):
    def test_binding_is_published_and_read_back(self):
        initial = json.dumps({'sources': [{'type': 'gitlab', 'base_url': 'https://gitlab.com',
                                           'owner': 'group', 'repos': 'all'}]}).encode()
        store = Store(initial)
        publish_binding(store, initial, 0, 'project', 42, 9)
        saved = json.loads(store.data)
        self.assertEqual(saved['sources'][0]['destinations']['project'],
                         {'source_id': 42, 'destination_id': 9})

    def test_cli_publishes_before_mirror_and_reuses_binding(self):
        initial = json.dumps({'destination_account_type': 'personal', 'sources': [
            {'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'group', 'repos': 'all'}]}).encode()
        store = Store(initial)
        mirrored = []
        def prepare(config, *args, **kwargs):
            config['sources'][0]['destinations']['project']['destination_id'] = 9
        def mirror(source, name, *args):
            self.assertEqual(json.loads(store.data)['sources'][0]['destinations']['project']['source_id'], 42)
            mirrored.append(source['destinations']['project']['destination_id'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_bytes(initial)
            with patch.dict('os.environ', {'GH_TOKEN': 'x', 'DESTINATION_OWNER': 'dest',
                                           'MIRROR_TIMESTAMP_SECRET': 'secret'}, clear=True), \
                 patch('repo_mirror.gitlab_onboarding_github.store_from_environment', return_value=store, create=True), \
                 patch('repo_mirror.sources.discover_gitlab_projects', return_value={'project': 42}), \
                 patch('repo_mirror.cli.get_existing', return_value=None), \
                 patch('repo_mirror.cli.add_to_config_pinned', side_effect=prepare) as prepare_mock, \
                 patch('repo_mirror.cli.mirror_repository', side_effect=mirror):
                cli.main(['--config', str(path)])
                path.write_bytes(store.data)
                cli.main(['--config', str(path)])
        self.assertEqual(mirrored, [9, 9])
        self.assertEqual(prepare_mock.call_count, 1)

    def test_interrupted_publication_never_mirrors(self):
        initial = json.dumps({'destination_account_type': 'personal', 'sources': [
            {'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'group', 'repos': 'all'}]}).encode()
        store = Store(initial)
        def prepare(config, *args, **kwargs):
            config['sources'][0]['destinations']['project']['destination_id'] = 9
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_bytes(initial)
            with patch.dict('os.environ', {'GH_TOKEN': 'x', 'DESTINATION_OWNER': 'dest',
                                           'MIRROR_TIMESTAMP_SECRET': 'secret'}, clear=True), \
                 patch('repo_mirror.gitlab_onboarding_github.store_from_environment', return_value=store), \
                 patch.object(store, 'publish', side_effect=OSError('interrupted')), \
                 patch('repo_mirror.sources.discover_gitlab_projects', return_value={'project': 42}), \
                 patch('repo_mirror.cli.get_existing', return_value=None), \
                 patch('repo_mirror.cli.add_to_config_pinned', side_effect=prepare), \
                 patch('repo_mirror.cli.mirror_repository') as mirror:
                with self.assertRaises(RuntimeError):
                    cli.main(['--config', str(path)])
                mirror.assert_not_called()
        self.assertEqual(store.data, initial)

    def test_existing_unpinned_destination_is_not_adopted(self):
        from repo_mirror.sources import source_marker
        source = {'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'group', 'repos': 'all'}
        initial = json.dumps({'destination_account_type': 'personal', 'sources': [source]}).encode()
        store = Store(initial)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_bytes(initial)
            with patch.dict('os.environ', {'GH_TOKEN': 'x', 'DESTINATION_OWNER': 'dest',
                                           'MIRROR_TIMESTAMP_SECRET': 'secret'}, clear=True), \
                 patch('repo_mirror.gitlab_onboarding_github.store_from_environment', return_value=store), \
                 patch('repo_mirror.sources.discover_gitlab_projects', return_value={'project': 42}), \
                 patch('repo_mirror.cli.get_existing', return_value={'id': 9, 'description': source_marker(source, 'project')}), \
                 patch('repo_mirror.cli.add_to_config_pinned') as prepare, \
                 patch('repo_mirror.cli.mirror_repository') as mirror:
                with self.assertRaises(RuntimeError):
                    cli.main(['--config', str(path)])
                prepare.assert_not_called()
                mirror.assert_not_called()
