import copy
import unittest

from repo_mirror.adhoc import add_to_config, remove_from_config
from repo_mirror import cli


def normalize_config(config):
    return cli.normalize_config(config)


class NamedGitHubConfigTest(unittest.TestCase):
    def setUp(self):
        self.config = {"destinations": {"main": {"account_type": "personal"}}, "repositories": [
            {"source": {"url": "https://github.com/Owner/Repo", "id": 12},
             "mirrors": {"main": {"name": "copy", "id": 34, "visibility": "public"}}}]}

    def test_normalizes_named_repository_and_preserves_pins(self):
        config = normalize_config(self.config)
        self.assertEqual(config["account_type"], "personal")
        self.assertEqual(config["repositories"], [{"type": "github", "base_url": "https://github.com", "owner": "Owner", "name": "Repo", "mirror": {"name": "copy", "source_id": 12, "destination_id": 34, "visibility": "public"}}])

    def test_rejects_non_string_source_url(self):
        self.config['repositories'][0]['source']['url'] = None
        with self.assertRaises(ValueError):
            normalize_config(self.config)

    def test_rejects_multiple_destinations_and_mirrors(self):
        config = copy.deepcopy(self.config)
        config['destinations']['other'] = {'account_type': 'personal'}
        with self.assertRaises(ValueError):
            normalize_config(config)
        config = copy.deepcopy(self.config)
        config['repositories'][0]['mirrors']['other'] = {'name': 'other'}
        with self.assertRaises(ValueError):
            normalize_config(config)

    def test_cli_manual_add_persists_named_config(self):
        import json
        import tempfile
        from pathlib import Path
        from repo_mirror import adhoc
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(self.config))
            adhoc.main(['--config', str(path), '--urls', 'https://github.com/a/b', '--destination', 'copy-b'])
            saved = json.loads(path.read_text())
            self.assertEqual(saved['repositories'][-1]['mirrors']['main']['name'], 'copy-b')
            adhoc.main(['--config', str(path), '--urls', 'https://github.com/a/b', '--remove'])
            self.assertEqual(json.loads(path.read_text()), self.config)

    def test_manual_selection_nonfirst_same_owner_preserves_pins(self):
        import json
        import os
        import tempfile
        from unittest.mock import patch

        self.config['repositories'].insert(0, {
            'source': {'url': 'https://github.com/Owner/First'},
            'mirrors': {'main': {'name': 'first-copy'}}})
        env = {'MIRROR_TIMESTAMP_SECRET': 'test', 'GH_TOKEN': 'token',
               'DESTINATION_OWNER': 'dest', 'MIRROR_SOURCE_URLS': 'https://github.com/owner/repo',
               'MIRROR_DESTINATION_NAME': 'new-target', 'MIRROR_VISIBILITY': 'private'}
        with tempfile.NamedTemporaryFile(mode='w+') as f:
            json.dump(self.config, f)
            f.flush()
            with patch.dict(os.environ, env, clear=True), patch.object(cli, 'get_existing', return_value=None), patch.object(cli, 'mirror_repository') as run:
                cli.main(['--config', f.name])
            self.assertEqual(run.call_count, 1)
            source, name = run.call_args.args[:2]
            self.assertEqual(name, 'Repo')
            self.assertEqual(source['mirror'], {
                'name': 'new-target', 'source_id': 12, 'destination_id': 34, 'visibility': 'private'})
            f.seek(0)
            self.assertEqual(json.load(f), self.config)

    def test_manual_custom_target_missing_pinned_destination_is_not_created(self):
        import json
        import os
        import tempfile
        from unittest.mock import Mock, patch
        from urllib.error import HTTPError
        from email.message import Message

        self.config['repositories'].insert(0, {
            'source': {'url': 'https://github.com/Owner/First'},
            'mirrors': {'main': {'name': 'first-copy'}}})
        env = {'MIRROR_TIMESTAMP_SECRET': 'test', 'GH_TOKEN': 'token',
               'DESTINATION_OWNER': 'dest', 'MIRROR_SOURCE_URLS': 'https://github.com/owner/repo',
               'MIRROR_DESTINATION_NAME': 'new-target', 'MIRROR_VISIBILITY': 'public'}
        src, dst = Mock(), Mock()
        src.request.return_value = {'id': 12, 'clone_url': 'https://github.com/Owner/Repo.git'}
        dst.request.side_effect = HTTPError('url', 404, 'missing', Message(), None)
        with tempfile.NamedTemporaryFile(mode='w+') as f:
            json.dump(self.config, f)
            f.flush()
            with patch.dict(os.environ, env, clear=True), patch.object(cli, 'SourceAPI', return_value=src), \
                 patch.object(cli, 'API', side_effect=[Mock(), dst]):
                with self.assertRaisesRegex(RuntimeError, '1 repositories failed'):
                    cli.main(['--config', f.name])
        self.assertTrue(dst.request.called)
        self.assertTrue(all(c.args[0] == 'GET' for c in dst.request.call_args_list))

    def test_named_writer_updates_and_removes_exact_same_owner_entry(self):
        import json
        import tempfile
        from pathlib import Path
        from repo_mirror import adhoc

        first = {'source': {'url': 'https://github.com/Owner/First'},
                 'mirrors': {'main': {'name': 'first-copy'}}}
        self.config['repositories'].insert(0, first)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(self.config))
            original = path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'pinned destination'):
                adhoc.main(['--config', str(path), '--urls', 'https://github.com/owner/repo',
                            '--destination', 'new-target', '--visibility', 'private'])
            self.assertEqual(path.read_bytes(), original)
            adhoc.main(['--config', str(path), '--urls', 'https://github.com/owner/repo', '--remove'])
            self.assertEqual(json.loads(path.read_text())['repositories'], [first])

    def test_writer_same_name_and_unpinned_rename_preserve_visibility(self):
        import json
        import tempfile
        from pathlib import Path
        from repo_mirror import adhoc

        for pinned in (True, False):
            with self.subTest(pinned=pinned), tempfile.TemporaryDirectory() as directory:
                config = copy.deepcopy(self.config)
                if not pinned:
                    del config['repositories'][0]['mirrors']['main']['id']
                path = Path(directory) / 'config.json'
                path.write_text(json.dumps(config))
                target = 'copy' if pinned else 'new-target'
                adhoc.main(['--config', str(path), '--urls', 'https://github.com/owner/repo',
                            '--destination', target])
                saved = normalize_config(json.loads(path.read_text()))
                settings = saved['repositories'][0]['mirror']
                self.assertEqual(settings['name'], target)
                self.assertEqual(settings['visibility'], 'public')
                if pinned:
                    self.assertEqual(settings['destination_id'], 34)

    def test_manual_add_and_remove_keep_named_format(self):
        self.assertTrue(add_to_config(self.config, 'https://github.com/Other/New', 'custom', 'private'))
        self.assertEqual(self.config['repositories'][-1]['mirrors']['main'], {'name': 'custom', 'visibility': 'private'})
        self.assertTrue(remove_from_config(self.config, 'https://github.com/Other/New'))
        self.assertEqual(len(self.config['repositories']), 1)
        self.assertEqual(normalize_config(self.config)['repositories'][0]['mirror']['destination_id'], 34)
