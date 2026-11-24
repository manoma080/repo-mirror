"""Forgejo visibility through the real worker, with all API and Git I/O faked."""
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from repo_mirror.forgejo_git import APIError, sync_refs
from repo_mirror.forgejo_migration import MigrationRefused, _ForgejoPolicy
from repo_mirror.forgejo_sync import main, parse_manifest
from repo_mirror.journal import FailureJournal
from test_forgejo_wipe_parity import Scenario, git as local_git


SOURCE = 'https://git.example/team/project'
MARKER = 'repo-mirror-source: git.example/team/project'
INITIALIZING = ' [repo-mirror:initializing:v1]'
INITIALIZED = ' [repo-mirror:initialized:v1]'


def manifest(**options):
    return {'destinations': {'local': {}}, 'repositories': [
        {'source': {'url': SOURCE}, 'mirrors': {'local': {
            'name': 'copy', 'ignore_releases': True, **options}}}]}


class ForgejoVisibilityTests(unittest.TestCase):
    def run_worker(self, config, *, existing=None, force_private=False, drift=False,
                   extra_args=(), journal_files=None, moved_source=False):
        repo = None if existing is None else dict(existing)
        name = config['repositories'][0]['mirrors']['local']['name']
        dest_path = '/api/v1/repos/dest/' + name
        mutations = []
        git_calls = []

        def local_request(api, method, path, payload=None, **kwargs):
            nonlocal repo
            if method == 'GET' and path == '/api/v1/user':
                return {'login': 'dest'}
            if method == 'GET' and path == dest_path:
                if repo is None:
                    raise APIError(404)
                return dict(repo)
            if method == 'POST' and path == '/api/v1/user/repos':
                self.assertIsNone(repo)
                assert payload is not None
                self.assertFalse(payload['has_actions'])
                self.assertFalse(payload['auto_init'])
                mutations.append((method, dict(payload)))
                repo = {**payload, 'full_name': 'dest/' + name, 'id': 22,
                        'private': force_private or payload['private'], 'has_actions': True}
                return dict(repo)
            if method == 'PATCH' and path == dest_path:
                assert repo is not None and payload is not None
                mutations.append((method, dict(payload)))
                repo.update(payload)
                return dict(repo)
            self.fail(f'unexpected destination request: {method} {path}')

        def source_request(api, method, url, body=None, headers=None, **kwargs):
            self.assertTrue(moved_source)
            self.assertEqual(method, 'GET')
            canonical = 'https://git.example/team/moved'
            return json.dumps({'id': 11, 'full_name': 'team/moved', 'html_url': canonical,
                               'clone_url': canonical + '.git'}).encode()

        def git(args, **kwargs):
            assert repo is not None
            self.assertIs(repo['has_actions'], False)
            git_calls.append(args)
            if drift and len(git_calls) == 1:
                repo['private'] = not repo['private']
            return subprocess.CompletedProcess(args, 0, b'', b'')

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / 'sources.json'
            config_path.write_text(json.dumps(config))
            token = root / 'token'
            token.write_text('test-token')
            token.chmod(0o600)
            secret = root / 'secret'
            secret.write_text('test-signing-secret-0123456789abcdef')
            secret.chmod(0o600)
            journal = root / 'journal'
            journal.mkdir(mode=0o700)
            stderr = io.StringIO()
            with patch('repo_mirror.github_api.API._request', new=source_request), \
                 patch('repo_mirror.forgejo_git.LocalForgejoAPI.request', new=local_request), \
                 patch('repo_mirror.forgejo_git.subprocess.run', side_effect=git), \
                 patch('repo_mirror.forgejo_sync.FailureJournal', wraps=FailureJournal) as open_journal, \
                 redirect_stderr(stderr), redirect_stdout(io.StringIO()):
                code = main(['--config', str(config_path), '--source', SOURCE, '--owner', 'dest',
                             '--token-file', str(token), '--timestamp-secret-file', str(secret),
                             '--journal-dir', str(journal), *extra_args])
            if journal_files is not None:
                journal_files.extend(Path(call.args[0]).name for call in open_journal.call_args_list)
            self.assertEqual(json.loads(config_path.read_text()), config)
        return code, stderr.getvalue(), repo, mutations, git_calls

    def pinned(self, private):
        return {'full_name': 'dest/copy', 'id': 22, 'private': private, 'has_actions': False,
                'description': MARKER + INITIALIZED}

    def test_new_mirror_creation_and_initialization_respect_visibility(self):
        for options, private in (({}, True), ({'visibility': 'private'}, True),
                                 ({'visibility': 'public'}, False)):
            with self.subTest(options=options):
                code, stderr, repo, mutations, git = self.run_worker(manifest(**options))
                self.assertEqual(code, 0, stderr)
                self.assertIs(mutations[0][1]['private'], private)
                self.assertEqual(mutations[1:], [('PATCH', {'has_actions': False}),
                                                  ('PATCH', {'description': MARKER + INITIALIZED})])
                self.assertIs(repo['private'], private)
                self.assertTrue(git)

    def test_pinned_public_mirror_runs_again_without_mutation(self):
        code, stderr, _, mutations, git = self.run_worker(
            manifest(id=22, visibility='public'), existing=self.pinned(False))
        self.assertEqual(code, 0, stderr)
        self.assertFalse(mutations)
        self.assertTrue(git)

    def test_existing_mismatched_visibility_fails_without_conversion(self):
        for options, private in (({'visibility': 'public'}, True),
                                 ({'visibility': 'private'}, False), ({}, False)):
            with self.subTest(options=options, private=private):
                code, _, repo, mutations, git = self.run_worker(
                    manifest(id=22, **options), existing=self.pinned(private))
                self.assertEqual(code, 1)
                self.assertIs(repo['private'], private)
                self.assertFalse(mutations)
                self.assertFalse(git)

    def test_forced_private_public_creation_fails_before_git(self):
        code, stderr, repo, mutations, git = self.run_worker(
            manifest(visibility='public'), force_private=True)
        self.assertEqual(code, 1)
        self.assertIn('privacy', stderr)
        self.assertIs(repo['private'], True)
        self.assertEqual([method for method, _ in mutations], ['POST'])
        self.assertFalse(git)

    def test_visibility_drift_stops_before_initialization_completion(self):
        code, stderr, repo, mutations, git = self.run_worker(manifest(visibility='public'), drift=True)
        self.assertEqual(code, 1)
        self.assertIn('privacy', stderr)
        self.assertEqual(repo['description'], MARKER + INITIALIZING)
        self.assertFalse(any('description' in payload for method, payload in mutations if method == 'PATCH'))
        self.assertTrue(git)
        self.assertFalse(any('push' in args or 'http://127.0.0.1:3300/dest/copy.git' in args for args in git))

    def test_only_exact_visibility_values_are_allowed(self):
        for value in (None, True, [], {}, 'Public', 'internal', ''):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'visibility'):
                parse_manifest(manifest(visibility=value))

    def test_transient_target_journal_does_not_share_the_authoritative_filename(self):
        stable = hashlib.sha256(f'3300:dest:{SOURCE.lower()}'.encode()).hexdigest() + '.json'
        journals = []
        isolated = ('--isolated-dispatch-journal',)
        for name, flags in (('stable', ()), ('copy', isolated), ('copy', isolated), ('other', isolated)):
            code, stderr, _, _, _ = self.run_worker(manifest(name=name), extra_args=flags, journal_files=journals)
            self.assertEqual(code, 0, stderr)
        self.assertEqual(journals[0], stable)
        self.assertNotEqual(journals[1], stable)
        self.assertEqual(journals[1], journals[2])
        self.assertNotEqual(journals[1], journals[3])

    def test_transient_snapshot_rejects_source_relocation_before_writes(self):
        config = manifest(id=22, visibility='public')
        config['repositories'][0]['source']['id'] = 11
        baseline, stderr, _, _, _ = self.run_worker(config, existing=self.pinned(False), moved_source=True)
        self.assertEqual(baseline, 0, stderr)
        self.assertIn('FORGEJO_RELOCATION:', stderr)
        code, stderr, _, mutations, git = self.run_worker(config, existing=self.pinned(False),
            extra_args=('--no-dispatch-relocation',), moved_source=True)
        self.assertEqual(code, 1)
        self.assertNotIn('FORGEJO_RELOCATION:', stderr)
        self.assertFalse(mutations)
        self.assertFalse(git)

    def test_empty_named_manifest_is_valid(self):
        self.assertEqual(parse_manifest({'destinations': {'local': {}}, 'repositories': []}), [])

    def test_migration_verifies_visibility_against_authoritative_config(self):
        config = manifest(id=22, visibility='public')
        config['repositories'][0]['source']['id'] = 11
        backend = SimpleNamespace(read=lambda: (json.dumps(config).encode(),))
        entry = {'source': {'id': 11, 'origin': 'https://git.example'},
                 'destination': {'id': 22, 'name': 'copy'},
                 'recovery': {'source_origin': 'https://git.example', 'source_owner': 'team',
                              'source_name': 'project'}}
        hooks = _ForgejoPolicy(None, 3300, 'dest', 'unused')
        hooks.verify_destination(SimpleNamespace(get=lambda name: self.pinned(False)), backend, entry, 'renamed')
        with self.assertRaises(MigrationRefused):
            hooks.verify_destination(SimpleNamespace(get=lambda name: self.pinned(True)), backend, entry, 'renamed')

    def test_public_mirror_wipe_archive_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = Scenario(Path(tmp))
            mirror = scenario.metadata['project']
            mirror['private'] = False
            expected = (mirror['id'], mirror['description'], False)
            # Scenario maps the production Git URLs to real local repositories.
            def sync_public(*args, **kwargs):
                return sync_refs(*args, **kwargs, expected=expected)
            with patch('test_forgejo_wipe_parity.sync_refs', new=sync_public):
                scenario.sync()
            name, archive = next(iter(scenario.archives.items()))
            self.assertIs(scenario.metadata[name]['private'], True)
            self.assertIs(scenario.metadata[name]['has_actions'], False)
            self.assertIs(mirror['private'], False)
            self.assertEqual(local_git('ls-remote', '--refs', archive), scenario.before)
