"""ignore_releases: per-repository release opt-out for both runners."""

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from repo_mirror import cli, forgejo_sync, repository
from repo_mirror.adhoc import add_to_config
from repo_mirror.config_named import normalize_config
from repo_mirror.sources import source_marker, validate_source

GITHUB_URL = 'https://github.com/manoma080/mirror-test'


def named_config(ignore_releases=None):
    mirror = {'name': 'mirror-alice-demo'}
    if ignore_releases is not None:
        mirror['ignore_releases'] = ignore_releases
    return {'destinations': {'github': {'account_type': 'personal'}},
            'repositories': [{'source': {'url': 'https://github.com/alice/demo'}, 'mirrors': {'github': mirror}}]}


class ConfigSchemaTests(unittest.TestCase):
    def test_named_config_accepts_boolean_and_rejects_other_values(self):
        runtime = normalize_config(named_config(True))
        self.assertEqual(runtime['repositories'][0]['mirror']['ignore_releases'], True)
        self.assertNotIn('ignore_releases', normalize_config(named_config())['repositories'][0]['mirror'])
        for value in ('true', 1, None, [], {}):
            with self.subTest(value=value):
                config = named_config()
                config['repositories'][0]['mirrors']['github']['ignore_releases'] = value
                with self.assertRaises(ValueError):
                    normalize_config(config)

    def test_validated_runtime_source_accepts_the_flag(self):
        source = normalize_config(named_config(True))['repositories'][0]
        validate_source(source)
        source['mirror']['ignore_releases'] = 'yes'
        with self.assertRaises(ValueError):
            validate_source(source)


class AdhocPersistenceTests(unittest.TestCase):
    def test_add_to_config_persists_and_clears_the_flag(self):
        config = named_config()
        self.assertTrue(add_to_config(config, 'https://github.com/alice/demo', ignore_releases=True))
        mirror = config['repositories'][0]['mirrors']['github']
        self.assertIs(mirror['ignore_releases'], True)
        self.assertFalse(add_to_config(config, 'https://github.com/alice/demo', ignore_releases=True))
        self.assertTrue(add_to_config(config, 'https://github.com/alice/demo', ignore_releases=False))
        self.assertNotIn('ignore_releases', config['repositories'][0]['mirrors']['github'])
        self.assertFalse(add_to_config(config, 'https://github.com/alice/demo', ignore_releases=None))

    def test_keep_leaves_a_saved_true_in_place(self):
        config = named_config(True)
        self.assertFalse(add_to_config(config, 'https://github.com/alice/demo', ignore_releases=None))
        self.assertIs(config['repositories'][0]['mirrors']['github']['ignore_releases'], True)


class GitHubRunnerTests(unittest.TestCase):
    def source(self, ignore):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice', 'name': 'demo',
                  'mirror': {'source_id': 11, 'destination_id': 22}}
        if ignore is not None:
            source['mirror']['ignore_releases'] = ignore
        return source

    def run_repository(self, ignore):
        source = self.source(ignore)
        src, dst = Mock(), Mock()
        src.request.return_value = {'id': 11, 'clone_url': 'https://github.com/alice/demo.git', 'default_branch': 'main'}
        dst.request.return_value = {'id': 22, 'description': source_marker(source, 'demo'), 'default_branch': 'main'}
        with patch('repo_mirror.repository.mirror_git') as git, \
                patch('repo_mirror.repository.mirror_releases') as releases, \
                redirect_stderr(io.StringIO()):
            repository.mirror_repository(source, 'demo', 'dest', src, dst, 'token')
        return git, releases

    def test_releases_are_skipped_but_git_still_mirrors(self):
        for value in (True, False, None):
            with self.subTest(value=value):
                git, releases = self.run_repository(value)
                git.assert_called_once()
                if value is True:
                    releases.assert_not_called()
                else:
                    releases.assert_called_once()


class CliOverrideTests(unittest.TestCase):
    def run_cli(self, override):
        with TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps({
                'destinations': {'main': {'account_type': 'personal'}},
                'repositories': [{'source': {'url': 'https://github.com/alice/demo', 'id': 11},
                                  'mirrors': {'main': {'id': 22}}}],
            }))
            env = {'MIRROR_TIMESTAMP_SECRET': 'fixture-signing-secret-0123456789abcdef',
                   'GH_TOKEN': 'token', 'DESTINATION_OWNER': 'dest',
                   'MIRROR_SOURCE_URLS': 'https://github.com/alice/demo'}
            if override is not None:
                env['MIRROR_IGNORE_RELEASES'] = override
            src, dst = Mock(), Mock()
            src.request.return_value = {'id': 11, 'clone_url': 'https://github.com/alice/demo.git'}
            dst.request.return_value = {'id': 22, 'description': source_marker({
                'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice'}, 'demo')}
            with patch.dict('os.environ', env, clear=True), \
                    patch.object(cli, 'SourceAPI', return_value=src), \
                    patch.object(cli, 'API', side_effect=[Mock(), dst]), \
                    patch.object(cli, 'mirror_repository') as mirror, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                cli.main(['--config', str(config)])
            return mirror.call_args.args[0]['mirror']

    def test_one_run_override_is_in_memory_only(self):
        self.assertIs(self.run_cli('true')['ignore_releases'], True)
        self.assertNotIn('ignore_releases', self.run_cli('keep'))
        self.assertNotIn('ignore_releases', self.run_cli(None))

    def test_invalid_override_is_rejected(self):
        with self.assertRaises(ValueError):
            self.run_cli('yes')


class ForgejoRunnerTests(unittest.TestCase):
    def run_worker(self, flags, entry_flags=None, fake_source=False):
        with TemporaryDirectory() as directory:
            mirror = {}
            if entry_flags is not None:
                mirror['ignore_releases'] = entry_flags
            config = {'destinations': {'local': {}},
                      'repositories': [{'source': {'url': GITHUB_URL}, 'mirrors': {'local': mirror}}]}
            files = {'config': json.dumps(config), 'token': 'token',
                     'secret': 'synthetic-recovery-fixture-value-32-bytes'}
            for filename, data in files.items():
                path = Path(directory) / filename
                path.write_text(data)
                path.chmod(0o600)
            api = Mock()
            api.request.return_value = {'id': 5, 'full_name': 'local/mirror-test', 'description': ''}
            destination = Mock()
            destination.begin_initialization.return_value = False
            destination.run_pin = None
            argv = ['--config', str(Path(directory) / 'config'), '--source', GITHUB_URL,
                    '--token-file', str(Path(directory) / 'token'),
                    '--timestamp-secret-file', str(Path(directory) / 'secret'),
                    '--journal-dir', directory, '--owner', 'local', '--use-github-token'] + flags
            with patch.dict(os.environ, {'GITHUB_TOKEN': 'source'}), \
                    patch.object(forgejo_sync, 'LocalForgejoAPI', return_value=api), \
                    patch.object(forgejo_sync, 'ForgejoDestination', return_value=destination), \
                    patch.object(forgejo_sync, 'sync_refs', return_value=0), \
                    patch.object(forgejo_sync, 'snapshot_refs', return_value=(None, {}, {})), \
                    patch.object(forgejo_sync, 'ForgejoDestinationAdapter'), \
                    patch.object(forgejo_sync, 'mirror_releases') as releases, \
                    self.source_patches(fake_source), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = forgejo_sync.main(argv)
            return code, releases

    def source_patches(self, fake_source):
        from contextlib import ExitStack, nullcontext
        if not fake_source:
            return nullcontext()
        downstream = Mock()
        downstream.pages.return_value = []
        downstream.request.return_value = {'id': 5, 'full_name': 'manoma080/mirror-test'}
        stack = [
            patch.object(forgejo_sync, 'source_pipeline', return_value=(
                {'type': 'github', 'base_url': 'https://github.com', 'owner': 'manoma080'},
                'token', downstream, 'mirror-test')),
            patch.object(forgejo_sync, 'validate_source_release_listing'),
            patch.object(forgejo_sync, 'validate_source_latest'),
            patch.object(forgejo_sync, 'fetch_source_latest', return_value={}),
        ]
        from contextlib import ExitStack
        exit_stack = ExitStack()
        for item in stack:
            exit_stack.enter_context(item)
        return exit_stack

    def test_releases_run_when_not_ignored(self):
        code, releases = self.run_worker([], fake_source=True)
        self.assertEqual(code, 0)
        releases.assert_called_once()

    def test_worker_flag_skips_releases(self):
        code, releases = self.run_worker(['--ignore-releases'])
        self.assertEqual(code, 0)
        releases.assert_not_called()

    def test_saved_entry_flag_skips_releases(self):
        code, releases = self.run_worker([], entry_flags=True)
        self.assertEqual(code, 0)
        releases.assert_not_called()


if __name__ == '__main__':
    unittest.main()
