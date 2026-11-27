"""Tests for operator-supplied GitHub App owner and installation id options."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from repo_mirror import forgejo_migration, forgejo_schedule, forgejo_sync
from repo_mirror.logging_utils import SAFE_ERROR_MESSAGES, error_detail

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / 'scheduler' / 'auto_update.py'
URL = 'https://github.com/mirror-owner/mirror-demo'
OWNER_REQUIRED = 'source GitHub App owner is required'
INSTALLATION_REQUIRED = 'GitHub App installation id is required'
HELPER_REQUIRED = 'GitHub App helper path is required'
GITHUB_TOKEN_HELPER_REQUIRED = 'GITHUB_TOKEN required; run under the GitHub App helper'


def _load_updater():
    spec = importlib.util.spec_from_file_location('auto_update_tested', LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in ('MIRROR_APP_OWNER', 'MIRROR_APP_INSTALLATION')}
    env.update(extra)
    return env


def _git(cwd, *args):
    subprocess.check_call(
        ['git', '-C', str(cwd), *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class SyncOwnerOptionTests(unittest.TestCase):
    def _setup_files(self, tmp):
        config = tmp / 'config.json'
        config.write_text(json.dumps([URL]))
        token = tmp / 'token'
        token.write_text('local-token')
        token.chmod(0o600)
        secret = tmp / 'secret'
        secret.write_text('x' * 32)
        secret.chmod(0o600)
        key = tmp / 'app.pem'
        key.write_text('fake-key')
        key.chmod(0o600)
        journals = tmp / 'journals'
        journals.mkdir(mode=0o700)
        return [
            '--config', str(config),
            '--source', URL,
            '--token-file', str(token),
            '--timestamp-secret-file', str(secret),
            '--journal-dir', str(journals),
            '--owner', 'dest-owner',
            '--app-id', '123',
            '--private-key-file', str(key),
        ]

    def test_env_var_honoured_when_flag_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self._setup_files(Path(directory))
            with (
                patch.dict(os.environ, _clean_env(MIRROR_APP_OWNER='env-owner'), clear=True),
                patch('repo_mirror.forgejo_sync.AppTokenProvider', return_value=lambda: 'tok') as provider,
                patch('repo_mirror.forgejo_sync.LocalForgejoAPI'),
                patch('repo_mirror.forgejo_sync.destination_locks', return_value=nullcontext()),
                patch('repo_mirror.forgejo_sync.journal_lock', return_value=nullcontext(directory)),
                patch('repo_mirror.forgejo_sync._sync_requested', return_value=0),
            ):
                self.assertEqual(forgejo_sync.main(args), 0)
            provider.assert_called_once_with('123', 'fake-key', 'env-owner', role='source')

    def test_flag_wins_over_env_var(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self._setup_files(Path(directory)) + ['--source-app-owner', 'flag-owner']
            with (
                patch.dict(os.environ, _clean_env(MIRROR_APP_OWNER='env-owner'), clear=True),
                patch('repo_mirror.forgejo_sync.AppTokenProvider', return_value=lambda: 'tok') as provider,
                patch('repo_mirror.forgejo_sync.LocalForgejoAPI'),
                patch('repo_mirror.forgejo_sync.destination_locks', return_value=nullcontext()),
                patch('repo_mirror.forgejo_sync.journal_lock', return_value=nullcontext(directory)),
                patch('repo_mirror.forgejo_sync._sync_requested', return_value=0),
            ):
                self.assertEqual(forgejo_sync.main(args), 0)
            provider.assert_called_once_with('123', 'fake-key', 'flag-owner', role='source')

    def test_missing_both_fails_closed_without_defaulting_to_destination_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self._setup_files(Path(directory))
            stderr = io.StringIO()
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch('repo_mirror.forgejo_sync.AppTokenProvider') as provider,
                patch('repo_mirror.forgejo_sync.LocalForgejoAPI') as api,
                redirect_stderr(stderr),
            ):
                self.assertEqual(forgejo_sync.main(args), 2)
            provider.assert_not_called()
            api.assert_not_called()
            self.assertIn(OWNER_REQUIRED, stderr.getvalue())

    def test_invalid_owner_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base_args = self._setup_files(Path(directory))
            for bad in ('.', '..', 'bad/owner', 'bad owner', ''):
                with (
                    self.subTest(bad=bad),
                    patch.dict(os.environ, _clean_env(), clear=True),
                    patch('repo_mirror.forgejo_sync.AppTokenProvider') as provider,
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(forgejo_sync.main(base_args + ['--source-app-owner', bad]), 2)
                    provider.assert_not_called()


class SchedulerIdentityOptionsTests(unittest.TestCase):
    def _setup_files(self, tmp, manifest_data=None):
        manifest = tmp / 'manifest.json'
        manifest.write_text(json.dumps(manifest_data if manifest_data is not None else [URL]))
        secret = tmp / 'secret'
        secret.write_text('x' * 32)
        secret.chmod(0o600)
        token = tmp / 'token'
        token.write_text('local-token')
        token.chmod(0o600)
        key = tmp / 'app.pem'
        key.write_text('fake-key')
        key.chmod(0o600)
        journals = tmp / 'journals'
        journals.mkdir(mode=0o700)
        return {
            'manifest': str(manifest),
            'secret': str(secret),
            'token': str(token),
            'key': str(key),
            'journals': str(journals),
            'lock': str(tmp / 'lock'),
        }

    def test_app_mode_env_var_honoured_and_worker_argv_omits_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            files = self._setup_files(Path(directory))
            args = [
                'run', '--manifest', files['manifest'], '--token-file', files['token'],
                '--timestamp-secret-file', files['secret'], '--journal-dir', files['journals'],
                '--owner', 'dest-owner', '--lock', files['lock'],
                '--app-id', '123', '--private-key-file', files['key'],
            ]
            with (
                patch.dict(os.environ, _clean_env(MIRROR_APP_OWNER='env-owner'), clear=True),
                patch('repo_mirror.forgejo_schedule.subprocess.Popen',
                      side_effect=lambda *a, **kw: Mock(poll=lambda: 0, pid=999999999)) as popen,
            ):
                self.assertEqual(forgejo_schedule.main(args), 0)
            command = popen.call_args.args[0]
            self.assertNotIn('--source-app-owner', command)
            self.assertIn('--app-id', command)

    def test_app_mode_flag_wins_over_env_and_threads_to_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            files = self._setup_files(Path(directory))
            args = [
                'run', '--manifest', files['manifest'], '--token-file', files['token'],
                '--timestamp-secret-file', files['secret'], '--journal-dir', files['journals'],
                '--owner', 'dest-owner', '--lock', files['lock'],
                '--app-id', '123', '--private-key-file', files['key'],
                '--source-app-owner', 'flag-owner',
            ]
            with (
                patch.dict(os.environ, _clean_env(MIRROR_APP_OWNER='env-owner'), clear=True),
                patch('repo_mirror.forgejo_schedule.subprocess.Popen',
                      side_effect=lambda *a, **kw: Mock(poll=lambda: 0, pid=999999999)) as popen,
            ):
                self.assertEqual(forgejo_schedule.main(args), 0)
            command = popen.call_args.args[0]
            self.assertEqual(command[command.index('--source-app-owner') + 1], 'flag-owner')

    def test_app_mode_missing_owner_fails_closed_before_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            files = self._setup_files(Path(directory))
            args = [
                'run', '--manifest', files['manifest'], '--token-file', files['token'],
                '--timestamp-secret-file', files['secret'], '--journal-dir', files['journals'],
                '--owner', 'dest-owner', '--lock', files['lock'],
                '--app-id', '123', '--private-key-file', files['key'],
            ]
            stderr = io.StringIO()
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch('repo_mirror.forgejo_schedule.subprocess.Popen') as popen,
                redirect_stderr(stderr),
            ):
                self.assertEqual(forgejo_schedule.main(args), 2)
            popen.assert_not_called()
            self.assertFalse(Path(files['lock']).exists())
            self.assertIn(OWNER_REQUIRED, stderr.getvalue())

    def test_helper_mode_installation_env_and_flag_precedence_and_missing_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            files = self._setup_files(Path(directory))
            base = [
                'run', '--manifest', files['manifest'], '--token-file', files['token'],
                '--timestamp-secret-file', files['secret'], '--journal-dir', files['journals'],
                '--owner', 'dest-owner', '--lock', files['lock'],
                '--helper', str(Path(directory) / 'helper.py'),
            ]

            with (
                patch.dict(os.environ, _clean_env(MIRROR_APP_INSTALLATION='654321'), clear=True),
                patch('repo_mirror.forgejo_schedule.subprocess.Popen',
                      side_effect=lambda *a, **kw: Mock(poll=lambda: 0, pid=999999999)) as popen,
            ):
                self.assertEqual(forgejo_schedule.main(base), 0)
            command = popen.call_args.args[0]
            self.assertEqual(command[command.index('--installation') + 1], '654321')

            with (
                patch.dict(os.environ, _clean_env(MIRROR_APP_INSTALLATION='654321'), clear=True),
                patch('repo_mirror.forgejo_schedule.subprocess.Popen',
                      side_effect=lambda *a, **kw: Mock(poll=lambda: 0, pid=999999999)) as popen,
            ):
                self.assertEqual(forgejo_schedule.main(base + ['--installation', '123456']), 0)
            command = popen.call_args.args[0]
            self.assertEqual(command[command.index('--installation') + 1], '123456')

            (Path(files['lock'])).unlink(missing_ok=True)
            stderr = io.StringIO()
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch('repo_mirror.forgejo_schedule.subprocess.Popen') as popen,
                redirect_stderr(stderr),
            ):
                self.assertEqual(forgejo_schedule.main(base), 2)
            popen.assert_not_called()
            self.assertFalse(Path(files['lock']).exists())
            self.assertIn(INSTALLATION_REQUIRED, stderr.getvalue())

    def test_auto_discovery_source_client_owner_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'app.pem'
            key.write_text('fake-key')
            key.chmod(0o600)
            rule = {'type': 'github', 'base_url': 'https://github.com'}

            args_env = SimpleNamespace(app_id='123', private_key_file=str(key), source_app_owner=None)
            with (
                patch.dict(os.environ, _clean_env(MIRROR_APP_OWNER='env-owner'), clear=True),
                patch('repo_mirror.github_api.AppTokenProvider') as provider,
            ):
                forgejo_schedule._build_auto_discovery_source_client(args_env)(rule)
            provider.assert_called_once_with('123', 'fake-key', 'env-owner', role='source')

            args_flag = SimpleNamespace(app_id='123', private_key_file=str(key), source_app_owner='flag-owner')
            with (
                patch.dict(os.environ, _clean_env(MIRROR_APP_OWNER='env-owner'), clear=True),
                patch('repo_mirror.github_api.AppTokenProvider') as provider,
            ):
                forgejo_schedule._build_auto_discovery_source_client(args_flag)(rule)
            provider.assert_called_once_with('123', 'fake-key', 'flag-owner', role='source')

            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                self.assertRaisesRegex(ValueError, OWNER_REQUIRED),
            ):
                forgejo_schedule._build_auto_discovery_source_client(args_env)(rule)

            for bad in ('.', '..', 'bad/owner'):
                args_bad = SimpleNamespace(app_id='123', private_key_file=str(key), source_app_owner=bad)
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    forgejo_schedule._build_auto_discovery_source_client(args_bad)(rule)


class MigrationBuilderOwnerTests(unittest.TestCase):
    def test_build_migration_resolves_source_app_owner(self):
        secret = 's' * 32
        base_args = {
            'forgejo_config_repo': 'dest-owner/control',
            'forgejo_config_id': 42,
            'config_repo': None,
            'owner': 'dest-owner',
            'port': 3000,
            'token_file': 'unused',
            'journal_dir': 'unused',
            'app_id': '123',
            'private_key_file': 'github.pem',
        }

        with (
            patch.dict(os.environ, _clean_env(MIRROR_APP_OWNER='env-owner'), clear=True),
            patch('repo_mirror.forgejo_sync.read_secure_file', return_value=('fixture-val', None)),
            patch('repo_mirror.github_api.AppTokenProvider') as provider,
        ):
            tx = forgejo_migration.build_migration(SimpleNamespace(**base_args), secret)
        self.assertIsNotNone(tx)
        provider.assert_called_once_with('123', 'fixture-val', 'env-owner', 'personal', role='source')

        with (
            patch.dict(os.environ, _clean_env(MIRROR_APP_OWNER='env-owner'), clear=True),
            patch('repo_mirror.forgejo_sync.read_secure_file', return_value=('fixture-val', None)),
            patch('repo_mirror.github_api.AppTokenProvider') as provider,
        ):
            forgejo_migration.build_migration(SimpleNamespace(**base_args, source_app_owner='flag-owner'), secret)
        provider.assert_called_once_with('123', 'fixture-val', 'flag-owner', 'personal', role='source')

        with (
            patch.dict(os.environ, _clean_env(), clear=True),
            patch('repo_mirror.forgejo_sync.read_secure_file', return_value=('fixture-val', None)),
            patch('repo_mirror.github_api.AppTokenProvider') as provider,
            self.assertRaisesRegex(ValueError, OWNER_REQUIRED),
        ):
            forgejo_migration.build_migration(SimpleNamespace(**base_args), secret)
        provider.assert_not_called()

        for bad in ('.', '..', 'bad/owner'):
            with (
                self.subTest(bad=bad),
                patch.dict(os.environ, _clean_env(), clear=True),
                patch('repo_mirror.forgejo_sync.read_secure_file', return_value=('fixture-val', None)),
                self.assertRaises(ValueError),
            ):
                forgejo_migration.build_migration(SimpleNamespace(**base_args, source_app_owner=bad), secret)


class AutoUpdateIdentityTests(unittest.TestCase):
    def _fixture(self, tmp):
        repo = tmp / 'remote'
        repo.mkdir()
        _git(repo, 'init', '-b', 'main')
        _git(repo, 'config', 'user.email', 'test@example.com')
        _git(repo, 'config', 'user.name', 'Test')
        (repo / 'repo_mirror').mkdir()
        (repo / 'repo_mirror' / '__init__.py').write_text('')
        (repo / 'repo_mirror' / 'forgejo_schedule.py').write_text(
            'import json, os, pathlib, sys\n'
            'pathlib.Path(os.environ["RESULT"]).write_text('
            'json.dumps({"argv": sys.argv[1:], "app_owner": os.environ.get("MIRROR_APP_OWNER")}))\n'
        )
        (repo / 'config-forgejo.json').write_text(json.dumps([URL]))
        _git(repo, 'add', '.')
        _git(repo, 'commit', '-m', 'init')
        helper = tmp / 'helper.py'
        helper.write_text(
            'import json, os, pathlib, sys\n'
            'idx = sys.argv.index("--")\n'
            'with pathlib.Path(os.environ["HELPER_CALLS"]).open("a") as f:\n'
            '    f.write(json.dumps(sys.argv[1:idx]) + "\\n")\n'
            'a = sys.argv[idx + 1:]\n'
            'os.execvp(a[0], a)\n'
        )
        return repo, helper

    def test_installation_precedence_missing_failure_and_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            repo, helper = self._fixture(tmp)
            common = [
                sys.executable, str(LAUNCHER),
                '--remote', str(repo),
                '--state-dir', str(tmp / 'state'),
                '--job-lock', str(tmp / 'job.lock'),
                '--helper', str(helper),
            ]
            tail = ['--', '--lock', str(tmp / 'sched.lock')]

            # 1. Missing both fails closed with exact fixed message.
            res = subprocess.run(
                common + tail,
                env=_clean_env(RESULT=str(tmp / 'result'), HELPER_CALLS=str(tmp / 'helper.log')),
                capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 2)
            self.assertIn(INSTALLATION_REQUIRED, res.stderr)
            self.assertFalse((tmp / 'result').exists())

            # 2. Non-digit env installation fails closed.
            res = subprocess.run(
                common + tail,
                env=_clean_env(MIRROR_APP_INSTALLATION='abc', RESULT=str(tmp / 'result'), HELPER_CALLS=str(tmp / 'helper.log')),
                capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 2)
            self.assertFalse((tmp / 'result').exists())

            # 3. Env var honoured; MIRROR_APP_OWNER forwarded in environment.
            res = subprocess.run(
                common + tail,
                env=_clean_env(
                    MIRROR_APP_INSTALLATION='654321',
                    MIRROR_APP_OWNER='env-owner',
                    RESULT=str(tmp / 'result'),
                    HELPER_CALLS=str(tmp / 'helper.log'),
                ),
                capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            calls = [json.loads(line) for line in (tmp / 'helper.log').read_text().splitlines()]
            self.assertEqual(calls[-1][calls[-1].index('--installation') + 1], '654321')
            sched = json.loads((tmp / 'result').read_text())
            self.assertEqual(sched['app_owner'], 'env-owner')

            # 4. Flag wins over env var.
            (tmp / 'result').unlink()
            res = subprocess.run(
                common + ['--installation', '123456'] + tail,
                env=_clean_env(
                    MIRROR_APP_INSTALLATION='654321',
                    RESULT=str(tmp / 'result'),
                    HELPER_CALLS=str(tmp / 'helper.log'),
                ),
                capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            calls = [json.loads(line) for line in (tmp / 'helper.log').read_text().splitlines()]
            self.assertEqual(calls[-1][calls[-1].index('--installation') + 1], '123456')

            # 5. --source-app-owner after -- remains disallowed by auto_update.py allowlist.
            (tmp / 'result').unlink()
            res = subprocess.run(
                common + ['--installation', '123456'] + tail + ['--source-app-owner', 'flag-owner'],
                env=_clean_env(RESULT=str(tmp / 'result'), HELPER_CALLS=str(tmp / 'helper.log')),
                capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 2)
            self.assertFalse((tmp / 'result').exists())

    def test_updater_module_imported_rejects_missing_installation(self):
        updater = _load_updater()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, _clean_env(), clear=True):
            tmp = Path(directory)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                rc = updater.main([
                    '--remote', str(tmp), '--state-dir', str(tmp / 'state'),
                    '--job-lock', str(tmp / 'job.lock'), '--helper', str(tmp / 'helper.py'),
                    '--', '--lock', str(tmp / 'sched.lock'),
                ])
            self.assertEqual(rc, 2)
            self.assertIn(INSTALLATION_REQUIRED, stderr.getvalue())

    def test_updater_requires_helper_flag(self):
        updater = _load_updater()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, _clean_env(MIRROR_APP_INSTALLATION='123456'), clear=True):
            tmp = Path(directory)
            stderr = io.StringIO()
            with patch.object(updater, '__file__', str(tmp / 'auto_update.py')), redirect_stderr(stderr):
                rc = updater.main([
                    '--remote', str(tmp), '--state-dir', str(tmp / 'state'),
                    '--job-lock', str(tmp / 'job.lock'), '--', '--lock', str(tmp / 'sched.lock'),
                ])
            self.assertEqual(rc, 2)
            self.assertIn(HELPER_REQUIRED, stderr.getvalue())

    def test_scheduler_helper_mode_requires_helper_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            manifest = tmp / 'manifest.json'
            manifest.write_text(json.dumps([URL]))
            secret = tmp / 'secret'
            secret.write_text('x' * 32)
            secret.chmod(0o600)
            token = tmp / 'token'
            token.write_text('local-token')
            token.chmod(0o600)
            journals = tmp / 'journals'
            journals.mkdir(mode=0o700)
            lock = tmp / 'lock'
            stderr = io.StringIO()
            with (
                patch.dict(os.environ, _clean_env(MIRROR_APP_INSTALLATION='123456'), clear=True),
                patch('repo_mirror.forgejo_schedule.subprocess.Popen') as popen,
                redirect_stderr(stderr),
            ):
                rc = forgejo_schedule.main([
                    'run', '--manifest', str(manifest), '--token-file', str(token),
                    '--timestamp-secret-file', str(secret), '--journal-dir', str(journals),
                    '--owner', 'dest-owner', '--lock', str(lock),
                ])
            self.assertEqual(rc, 2)
            self.assertIn(HELPER_REQUIRED, stderr.getvalue())
            popen.assert_not_called()
            self.assertFalse(lock.exists())

    def test_sync_missing_github_token_reports_generic_helper_message(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            config = tmp / 'config.json'
            config.write_text(json.dumps([URL]))
            token = tmp / 'token'
            token.write_text('local-token')
            token.chmod(0o600)
            secret = tmp / 'secret'
            secret.write_text('x' * 32)
            secret.chmod(0o600)
            journals = tmp / 'journals'
            journals.mkdir(mode=0o700)
            stderr = io.StringIO()
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                redirect_stderr(stderr),
            ):
                rc = forgejo_sync.main([
                    '--config', str(config), '--source', URL, '--token-file', str(token),
                    '--timestamp-secret-file', str(secret), '--journal-dir', str(journals),
                    '--owner', 'dest-owner', '--use-github-token',
                ])
            self.assertEqual(rc, 2)
            self.assertIn(GITHUB_TOKEN_HELPER_REQUIRED, stderr.getvalue())


class ErrorDetailTests(unittest.TestCase):
    def test_new_fixed_messages_are_safe_and_surfaced(self):
        for message in (OWNER_REQUIRED, INSTALLATION_REQUIRED, HELPER_REQUIRED, GITHUB_TOKEN_HELPER_REQUIRED):
            with self.subTest(message=message):
                self.assertIn(message, SAFE_ERROR_MESSAGES)
                self.assertEqual(error_detail(ValueError(message)), message)
        for message in (OWNER_REQUIRED, HELPER_REQUIRED, GITHUB_TOKEN_HELPER_REQUIRED):
            self.assertEqual(forgejo_sync.safe_reason(ValueError(message), configuration=True), message)
        self.assertEqual(forgejo_sync.safe_reason(ValueError(OWNER_REQUIRED)), 'unexpected sync failure')


if __name__ == '__main__':
    unittest.main()
