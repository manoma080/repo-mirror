import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repo_mirror import cli
import os
import subprocess
import textwrap


def workflow_step(name):
    text = (Path(__file__).resolve().parents[1] / '.github/workflows/mirror.yml').read_text()
    section = text.split('      - name: ' + name + '\n', 1)[1].split('\n      - name:', 1)[0]
    return textwrap.dedent(section.split('        run: |\n', 1)[1])


def git(directory, *args):
    return subprocess.run(['git', *args], cwd=directory, check=True, text=True, capture_output=True).stdout.strip()


class EntrypointSafetyTests(unittest.TestCase):
    def test_changed_config_blocks_admission_before_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git(root, 'init', '--bare', '--initial-branch=main', 'remote.git')
            git(root, 'clone', 'remote.git', 'runner')
            runner = root / 'runner'
            (runner / 'config.json').write_text('approved')
            git(runner, 'add', '.')
            git(runner, '-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'initial')
            git(runner, 'push', 'origin', 'HEAD:main')
            git(root, 'clone', 'remote.git', 'writer')
            writer = root / 'writer'
            (writer / 'config.json').write_text('revoked')
            git(writer, 'add', 'config.json')
            git(writer, '-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'revoke')
            git(writer, 'push', 'origin', 'HEAD:main')
            env = {**os.environ, 'MANAGEMENT_TOKEN': 'test', 'MANAGEMENT_BRANCH': 'main', 'RUNNER_TEMP': directory}
            result = subprocess.run(['bash', '-e', '-c', workflow_step('Refresh failure journal')], cwd=runner, env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertIn('config', result.stderr.lower())
            tree = git(writer, 'ls-tree', '--name-only', 'origin/main')
            self.assertNotIn('.failure-journal-in-progress', tree)

    def test_missing_journal_clears_fence_without_creating_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git(root, 'init', '--bare', '--initial-branch=main', 'remote.git')
            git(root, 'clone', 'remote.git', 'runner')
            runner = root / 'runner'
            (runner / 'config.json').write_text('config')
            git(runner, 'add', '.')
            git(runner, '-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'initial')
            git(runner, 'push', 'origin', 'HEAD:main')
            env = {**os.environ, 'MANAGEMENT_TOKEN': 'test', 'MANAGEMENT_BRANCH': 'main',
                   'RUNNER_TEMP': directory, 'GITHUB_OUTPUT': str(root / 'output'),
                   'PREPARE_OUTCOME': 'success', 'JOURNAL_PRESENT': '', 'MIRROR_OUTCOME': 'success'}
            def run(step):
                return subprocess.run(['bash', '-e', '-c', workflow_step(step)], cwd=runner, env=env, capture_output=True, text=True)
            self.assertEqual(run('Refresh failure journal').returncode, 0)
            self.assertEqual(run('Prepare failure journal recovery').returncode, 0)
            result = run('Save failure journal')
            self.assertEqual(result.returncode, 0, result.stderr)
            git(runner, 'fetch', 'origin', 'main')
            tree = git(runner, 'ls-tree', '--name-only', 'origin/main')
            self.assertNotIn('.failure-journal-in-progress', tree)
            self.assertNotIn('failures.json', tree)

    def test_missing_journal_does_not_clobber_concurrent_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git(root, 'init', '--bare', '--initial-branch=main', 'remote.git')
            git(root, 'clone', 'remote.git', 'runner')
            runner = root / 'runner'
            (runner / 'config.json').write_text('config')
            git(runner, 'add', '.')
            git(runner, '-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'initial')
            git(runner, 'push', 'origin', 'HEAD:main')
            env = {**os.environ, 'MANAGEMENT_TOKEN': 'test', 'MANAGEMENT_BRANCH': 'main', 'RUNNER_TEMP': directory,
                   'GITHUB_OUTPUT': str(root / 'output'), 'PREPARE_OUTCOME': 'success', 'JOURNAL_PRESENT': '',
                   'MIRROR_OUTCOME': 'success'}
            def run(step):
                return subprocess.run(['bash', '-e', '-c', workflow_step(step)], cwd=runner, env=env, capture_output=True, text=True)
            self.assertEqual(run('Refresh failure journal').returncode, 0)
            self.assertEqual(run('Prepare failure journal recovery').returncode, 0)
            git(root, 'clone', 'remote.git', 'writer')
            writer = root / 'writer'
            (writer / 'failures.json').write_text('concurrent')
            git(writer, 'add', 'failures.json')
            git(writer, '-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'concurrent')
            git(writer, 'push', 'origin', 'HEAD:main')
            self.assertNotEqual(run('Save failure journal').returncode, 0)
            self.assertEqual(git(writer, 'show', 'origin/main:failures.json'), 'concurrent')
            self.assertIn('.failure-journal-in-progress', git(writer, 'ls-tree', '--name-only', 'origin/main'))

    def test_cancelled_or_ambiguous_run_keeps_fence(self):
        text = (Path(__file__).resolve().parents[1] / '.github/workflows/mirror.yml').read_text()
        self.assertIn('MIRROR_OUTCOME: ${{ steps.mirror-run.outcome }}', text)
        # GitHub rejects cancelled() outside `if:` contexts, so the fence
        # decision must rely on the step outcome alone.
        self.assertNotIn('cancelled()', text)
        for outcome in ('cancelled', ''):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                git(root, 'init', '--bare', '--initial-branch=main', 'remote.git')
                git(root, 'clone', 'remote.git', 'runner')
                runner = root / 'runner'
                (runner / 'config.json').write_text('config')
                git(runner, 'add', '.')
                git(runner, '-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'initial')
                git(runner, 'push', 'origin', 'HEAD:main')
                env = {**os.environ, 'MANAGEMENT_TOKEN': 'test', 'MANAGEMENT_BRANCH': 'main', 'RUNNER_TEMP': directory,
                       'GITHUB_OUTPUT': str(root / 'output'), 'PREPARE_OUTCOME': 'success', 'JOURNAL_PRESENT': '',
                       'MIRROR_OUTCOME': outcome}
                def run(step):
                    return subprocess.run(['bash', '-e', '-c', workflow_step(step)], cwd=runner, env=env, capture_output=True, text=True)
                self.assertEqual(run('Refresh failure journal').returncode, 0)
                self.assertEqual(run('Prepare failure journal recovery').returncode, 0)
                run('Save failure journal')
                git(runner, 'fetch', 'origin', 'main')
                self.assertIn('.failure-journal-in-progress', git(runner, 'ls-tree', '--name-only', 'origin/main'))

    def test_unresolved_run_fences_next_run_even_if_artifact_upload_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git(root, 'init', '--bare', '--initial-branch=main', 'remote.git')
            git(root, 'clone', 'remote.git', 'first')
            first = root / 'first'
            (first / 'config.json').write_text('config')
            (first / 'failures.json').write_text('base\n')
            git(first, 'add', '.')
            git(first, '-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'initial')
            git(first, 'push', 'origin', 'HEAD:main')
            git(root, 'clone', 'remote.git', 'second')
            env = {**os.environ, 'MANAGEMENT_TOKEN': 'test', 'MANAGEMENT_BRANCH': 'main',
                   'RUNNER_TEMP': str(root), 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}
            def refresh(where):
                return subprocess.run(['bash', '-e', '-c', workflow_step('Refresh failure journal')], cwd=where, env=env, capture_output=True, text=True)
            self.assertEqual(refresh(first).returncode, 0)
            # Simulate loss of the runner and failure to upload its artifact.
            env['GITHUB_RUN_ID'] = '124'
            blocked = refresh(root / 'second')
            self.assertNotEqual(blocked.returncode, 0, blocked.stderr)
            self.assertIn('unresolved', blocked.stderr.lower())

    def test_workflow_failure_journal_keeps_new_config_and_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git(root, 'init', '--bare', '--initial-branch=main', 'remote.git')
            git(root, 'clone', 'remote.git', 'runner')
            runner = root / 'runner'
            (runner / 'config.json').write_text('old config')
            (runner / 'failures.json').write_text('old failures')
            git(runner, 'add', '.')
            git(runner, '-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'initial')
            git(runner, 'push', 'origin', 'HEAD:main')
            env = {**os.environ, 'MANAGEMENT_TOKEN': 'test', 'MANAGEMENT_BRANCH': 'main', 'RUNNER_TEMP': directory,
                   'PREPARE_OUTCOME': 'success', 'JOURNAL_PRESENT': 'true', 'MIRROR_OUTCOME': 'failure', 'MIRROR_OUTCOME': 'success'}
            refresh = subprocess.run(['bash', '-e', '-c', workflow_step('Refresh failure journal')], cwd=runner, env=env, capture_output=True, text=True)
            self.assertEqual(refresh.returncode, 0, refresh.stderr)
            git(root, 'clone', 'remote.git', 'api')
            api = root / 'api'
            (api / 'config.json').write_text('new pinned config')
            (api / 'migrations.json').write_text('new migration')
            git(api, 'add', '.')
            git(api, '-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'CAS')
            git(api, 'push', 'origin', 'HEAD:main')
            (runner / 'failures.json').write_text('new failures')
            recovery = root / 'failure-journal-recovery'
            recovery.mkdir()
            (recovery / 'failures.json').write_bytes((runner / 'failures.json').read_bytes())
            save = subprocess.run(['bash', '-e', '-c', workflow_step('Save failure journal')], cwd=runner, env=env, capture_output=True, text=True)
            self.assertEqual(save.returncode, 0, save.stderr)
            git(api, 'fetch', 'origin', 'main')
            self.assertEqual(git(api, 'show', 'origin/main:config.json'), 'new pinned config')
            self.assertEqual(git(api, 'show', 'origin/main:migrations.json'), 'new migration')
            self.assertEqual(git(api, 'show', 'origin/main:failures.json'), 'new failures')
            # A competing writer changes the same journal after our refresh.
            git(api, 'reset', '--hard', 'origin/main')
            (api / 'failures.json').write_text('concurrent failures')
            git(api, 'add', 'failures.json')
            git(api, '-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'concurrent')
            git(api, 'push', 'origin', 'HEAD:main')
            (runner / 'failures.json').write_text('another local change')
            (recovery / 'failures.json').write_bytes((runner / 'failures.json').read_bytes())
            save = subprocess.run(['bash', '-e', '-c', workflow_step('Save failure journal')], cwd=runner, env=env, capture_output=True, text=True)
            self.assertNotEqual(save.returncode, 0)
            self.assertIn('changed concurrently', save.stderr)
            git(api, 'fetch', 'origin', 'main')
            self.assertEqual(git(api, 'show', 'origin/main:failures.json'), 'concurrent failures')

    def test_workflow_preserves_conflicting_local_journal_as_artifact(self):
        text = (Path(__file__).resolve().parents[1] / '.github/workflows/mirror.yml').read_text()
        self.assertIn('actions/upload-artifact@', text)
        self.assertIn('steps.save-journal.outcome', text)
        self.assertIn('retention-days:', text)
        self.assertIn('if-no-files-found: error', text)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'failures.json').write_bytes(b'exact local bytes\n')
            env = {**os.environ, 'RUNNER_TEMP': directory, 'GITHUB_OUTPUT': str(root / 'output')}
            result = subprocess.run(['bash', '-e', '-c', workflow_step('Prepare failure journal recovery')], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'failure-journal-recovery' / 'failures.json').read_bytes(), b'exact local bytes\n')

    def test_workflow_rejects_non_main_before_mirror(self):
        text = (Path(__file__).resolve().parents[1] / '.github/workflows/mirror.yml').read_text()
        self.assertLess(text.index('- name: Validate management branch'), text.index('- name: Mirror\n'))
        script = workflow_step('Validate management branch')
        for branch, code in [('main', 0), ('other', 1)]:
            result = subprocess.run(['bash', '-e', '-c', script], env={**os.environ, 'MANAGEMENT_BRANCH': branch}, capture_output=True)
            self.assertEqual(result.returncode, code)

    def test_push_race_retains_snapshot_and_remote(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git(root, 'init', '--bare', '--initial-branch=main', 'remote.git')
            git(root, 'clone', 'remote.git', 'runner')
            runner = root / 'runner'
            (runner / 'config.json').write_text('config')
            (runner / 'failures.json').write_text('original\n')
            git(runner, 'add', '.')
            git(runner, '-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'initial')
            git(runner, 'push', 'origin', 'HEAD:main')
            git(root, 'clone', 'remote.git', 'api')
            api = root / 'api'
            env = {**os.environ, 'MANAGEMENT_TOKEN': 'test', 'MANAGEMENT_BRANCH': 'main',
                   'RUNNER_TEMP': directory, 'GITHUB_OUTPUT': str(root / 'output'),
                   'PREPARE_OUTCOME': 'success', 'JOURNAL_PRESENT': 'true', 'MIRROR_OUTCOME': 'failure', 'MIRROR_OUTCOME': 'success'}
            def run(step):
                return subprocess.run(['bash', '-e', '-c', workflow_step(step)], cwd=runner, env=env, capture_output=True, text=True)
            self.assertEqual(run('Refresh failure journal').returncode, 0)
            (runner / 'failures.json').write_bytes(b'local exact\n')
            self.assertEqual(run('Prepare failure journal recovery').returncode, 0)
            git(api, 'fetch', 'origin', 'main')
            git(api, 'reset', '--hard', 'origin/main')
            (api / 'failures.json').write_text('racing remote\n')
            git(api, 'add', 'failures.json')
            git(api, '-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'race')
            bindir = root / 'bin'
            bindir.mkdir()
            wrapper = bindir / 'git'
            wrapper.write_text('#!/bin/bash\nif [[ " $* " == *" push "* ]]; then\n  /usr/bin/git -C "$RACE_API" push origin HEAD:main >/dev/null 2>&1 || exit 2\nfi\nexec /usr/bin/git "$@"\n')
            wrapper.chmod(0o755)
            env.update(PATH=str(bindir) + os.pathsep + os.environ['PATH'], RACE_API=str(api))
            save = run('Save failure journal')
            self.assertNotEqual(save.returncode, 0, save.stderr)
            self.assertEqual(git(api, 'show', 'origin/main:failures.json'), 'racing remote')
            self.assertEqual((root / 'failure-journal-recovery' / 'failures.json').read_bytes(), b'local exact\n')
            # Even if artifact upload fails, the next workflow shell refuses to run.
            env['PATH'] = os.environ['PATH']
            next_run = run('Refresh failure journal')
            self.assertNotEqual(next_run.returncode, 0)
            self.assertIn('unresolved', next_run.stderr.lower())
            text = (Path(__file__).resolve().parents[1] / '.github/workflows/mirror.yml').read_text()
            upload = text.split('      - name: Upload failure journal recovery\n', 1)[1]
            self.assertIn("steps.save-journal.outcome == 'failure' && steps.prepare-journal.outputs.present == 'true'", upload)
            self.assertIn('path: ${{ runner.temp }}/failure-journal-recovery/failures.json', upload)
            self.assertIn('retention-days: 7', upload)

    def test_recovery_rejects_symlink_and_ignores_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {**os.environ, 'RUNNER_TEMP': directory, 'GITHUB_OUTPUT': str(root / 'output')}
            def prepare():
                return subprocess.run(['bash', '-e', '-c', workflow_step('Prepare failure journal recovery')], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(prepare().returncode, 0)
            (root / 'secret').write_text('secret')
            (root / 'failures.json').symlink_to(root / 'secret')
            self.assertNotEqual(prepare().returncode, 0)
            self.assertFalse((root / 'failure-journal-recovery').exists())

    def test_group_cli_real_preparation_and_contents_api_cas(self):
        import base64
        from urllib.error import HTTPError
        from repo_mirror.github_migration import GitHubConfigFile
        from repo_mirror.migrations import blob_sha
        source = {'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'group', 'repos': 'all'}
        initial = json.dumps({'destination_account_type': 'personal', 'sources': [source]}).encode()
        class Contents:
            data = initial
            puts = 0
            def request(self, method, url, payload=None):
                if method == 'PUT':
                    assert payload['sha'] == blob_sha(self.data)
                    self.data = base64.b64decode(payload['content'])
                    self.puts += 1
                    return {}
                return {'type': 'file', 'encoding': 'base64', 'sha': blob_sha(self.data),
                        'content': base64.b64encode(self.data).decode()}
        class Source:
            def restrict_origin(self, origin): pass
            def request(self, method, url, *args):
                return {'id': 42, 'visibility': 'public', 'path_with_namespace': 'group/project',
                        'http_url_to_repo': 'https://gitlab.com/group/project.git'}
        class Destination:
            repo = None
            def request(self, method, url, payload=None, **kwargs):
                if url.endswith('/actions/permissions'):
                    return {'enabled': False} if method == 'GET' else None
                if url.endswith('/user'): return {'login': 'dest'}
                if method == 'GET':
                    if self.repo is None: raise HTTPError(url, 404, 'missing', {}, None)
                    return self.repo.copy()
                assert method == 'POST'
                self.repo = {'id': 9, 'full_name': 'dest/' + payload['name'], 'description': payload['description']}
                return self.repo.copy()
        management, dst = Contents(), Destination()
        store = GitHubConfigFile(management, 'owner', 'management')
        mirrored = []
        def mirror(source, name, *args):
            binding = json.loads(management.data)['sources'][0]['destinations']['project']
            mirrored.append(binding.copy())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_bytes(initial)
            with patch.dict('os.environ', {'GH_TOKEN': 'x', 'DESTINATION_OWNER': 'dest',
                    'MIRROR_TIMESTAMP_SECRET': 'secret'}, clear=True), \
                 patch('repo_mirror.gitlab_onboarding_github.store_from_environment', return_value=store), \
                 patch('repo_mirror.sources.discover_gitlab_projects', return_value={'project': 42}), \
                 patch('repo_mirror.cli.SourceAPI', side_effect=lambda token: Source()), \
                 patch('repo_mirror.cli.API', side_effect=lambda token=None: dst), \
                 patch('repo_mirror.cli.mirror_repository', side_effect=mirror):
                cli.main(['--config', str(path)])
        self.assertEqual(management.puts, 1)
        self.assertEqual(mirrored, [{'source_id': 42, 'destination_id': 9}])

    def test_group_runtime_custom_settings_rejected_before_destination_calls(self):
        source = {'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'group', 'repos': 'all'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            initial = json.dumps({'destination_account_type': 'personal', 'sources': [source]}).encode()
            path.write_bytes(initial)
            from test_gitlab_group_github import Store
            store = Store(initial)
            with patch.dict('os.environ', {'GH_TOKEN': 'x', 'DESTINATION_OWNER': 'dest',
                    'MIRROR_TIMESTAMP_SECRET': 'secret', 'MIRROR_SOURCE_URLS': 'https://gitlab.com/group/project',
                    'MIRROR_DESTINATION_NAME': 'custom', 'MIRROR_VISIBILITY': 'public'}, clear=True), \
                 patch('repo_mirror.gitlab_onboarding_github.store_from_environment', return_value=store), \
                 patch('repo_mirror.sources.discover_gitlab_projects', return_value={'project': 42}), \
                 patch('repo_mirror.cli.get_existing') as existing, \
                 patch('repo_mirror.cli.mirror_repository') as mirror:
                with self.assertRaises(ValueError):
                    cli.main(['--config', str(path)])
                existing.assert_not_called()
                mirror.assert_not_called()

    def test_explicit_source_only_pin_never_reaches_mirror(self):
        source = {'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'group',
                  'repos': ['project'], 'destinations': {'project': {'source_id': 42}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps({'destination_account_type': 'personal', 'sources': [source]}))
            with patch.dict('os.environ', {'GH_TOKEN': 'x', 'DESTINATION_OWNER': 'dest',
                                           'MIRROR_TIMESTAMP_SECRET': 'secret'}, clear=True), \
                 patch('repo_mirror.cli.get_existing', return_value=None), \
                 patch('repo_mirror.cli.mirror_repository') as mirror:
                with self.assertRaises(RuntimeError):
                    cli.main(['--config', str(path)])
                mirror.assert_not_called()
