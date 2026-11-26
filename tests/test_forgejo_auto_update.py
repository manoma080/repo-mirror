import json
import fcntl
import os
import signal
import time
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


LAUNCHER = Path(__file__).resolve().parents[1] / 'scheduler' / 'auto_update.py'


def git(cwd, *args):
    return subprocess.check_output(['git', '-C', str(cwd), *args], stderr=subprocess.DEVNULL, text=True).strip()


def fixture(tmp_path):
    repo = tmp_path / 'remote'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.email', 'test@example.com')
    git(repo, 'config', 'user.name', 'Test')
    (repo / 'repo_mirror').mkdir()
    (repo / 'repo_mirror' / '__init__.py').write_text('')
    (repo / 'repo_mirror' / 'forgejo_schedule.py').write_text(
        'import json, os, pathlib, sys\n'
        'p = pathlib.Path(sys.argv[sys.argv.index("--manifest") + 1])\n'
        'pathlib.Path(os.environ["RESULT"]).write_text(json.dumps([p.read_text(), pathlib.Path(__file__).read_text()]))\n'
    )
    (repo / 'config-forgejo.json').write_text(json.dumps(['https://github.com/mirror-owner/mirror-one']))
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'initial')
    helper = tmp_path / 'helper.py'
    helper.write_text('import os, sys\na=sys.argv[sys.argv.index("--")+1:]\nos.execvp(a[0], a)\n')
    return repo, helper


def launch(tmp_path, repo, helper, extra=None):
    env = dict(os.environ, RESULT=str(tmp_path / 'result'))
    if extra is None:
        extra = ['--lock', str(tmp_path / 'scheduler.lock')]
    return subprocess.run([sys.executable, str(LAUNCHER), '--remote', str(repo), '--state-dir', str(tmp_path / 'state'), '--job-lock', str(tmp_path / 'job.lock'), '--helper', str(helper), '--installation', '123456', '--', *extra], env=env, capture_output=True, text=True)


class AutoUpdateIntegrationTests(unittest.TestCase):
    def test_named_manifest_passes_through_standalone_launcher(self):
        tmp = self.tmp_path
        repo, helper = fixture(tmp)
        config = {'destinations': {'local': {}}, 'repositories': [
            {'source': {'url': 'https://github.com/mirror-owner/mirror-one', 'id': 11},
             'mirrors': {'local': {'name': 'mirror-one', 'id': 22}}}]}
        (repo / 'config-forgejo.json').write_text(json.dumps(config))
        git(repo, 'add', '.'); git(repo, 'commit', '-m', 'named config')
        result = launch(tmp, repo, helper)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(json.loads((tmp / 'result').read_text())[0]), config)

    def test_rejects_invalid_or_conflicting_sources_before_scheduler(self):
        tmp = self.tmp_path
        repo, helper = fixture(tmp)
        for sources in [['https://forge.example:99999/a/b'],
                        ['https://github.com.evil.example/a/b'],
                        ['https://forge.example/-owner/repo'],
                        ['https://forge.example/owner/-repo'],
                        ['https://forge.example/a/b?query=x'],
                        ['https://forge.example/a/repo', 'https://else.example/b/REPO']]:
            with self.subTest(sources=sources):
                (repo / 'config-forgejo.json').write_text(json.dumps(sources))
                git(repo, 'add', '.'); git(repo, 'commit', '-m', 'bad sources')
                (tmp / 'result').unlink(missing_ok=True)
                self.assertEqual(launch(tmp, repo, helper).returncode, 2)
                self.assertFalse((tmp / 'result').exists())

    def test_github_sources_outside_the_mirror_account_are_accepted(self):
        # Parity with the GitHub runner: any configured GitHub repository is a
        # valid source, not only this account's mirror-* repositories.
        tmp = self.tmp_path
        repo, helper = fixture(tmp)
        sources = ['https://github.com/other/repo', 'https://github.com/octocat/Hello-World']
        (repo / 'config-forgejo.json').write_text(json.dumps(sources))
        git(repo, 'add', '.'); git(repo, 'commit', '-m', 'third-party sources')
        result = launch(tmp, repo, helper)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(json.loads((tmp / 'result').read_text())[0]), sources)

    def test_forgejo_url_passes_unmodified_to_fetched_scheduler(self):
        tmp = self.tmp_path
        repo, helper = fixture(tmp)
        sources = ['https://github.com/mirror-owner/mirror-one', 'https://Forge.Example:443/Owner/Repo']
        (repo / 'config-forgejo.json').write_text(json.dumps(sources))
        git(repo, 'add', '.'); git(repo, 'commit', '-m', 'mixed sources')
        result = launch(tmp, repo, helper)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(json.loads((tmp / 'result').read_text())[0]), sources)

    def test_scheduler_arguments_are_validated_before_argparse_scheduler(self):
        tmp = self.tmp_path
        repo, helper = fixture(tmp)
        (repo / 'repo_mirror' / 'forgejo_schedule.py').write_text(
            'import argparse,json,os,pathlib\n'
            'p=argparse.ArgumentParser()\n'
            'p.add_argument("action")\n'
            'p.add_argument("--manifest")\n'
            'p.add_argument("--lock")\n'
            'p.add_argument("--organization", action="store_true")\n'
            'for opt in ("token-file", "timestamp-secret-file", "journal-dir", "owner", "app-id", "private-key-file"):\n'
            '    p.add_argument("--" + opt)\n'
            'a=p.parse_args()\n'
            'pathlib.Path(os.environ["RESULT"]).write_text(json.dumps(vars(a)))\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'argparse scheduler')
        good = ['--lock', str(tmp / 'scheduler.lock')]
        for bad in (['--manif', '/evil'], ['--manif=/evil'],
                    ['--loc', str(tmp / 'job.lock')], ['--loc=' + str(tmp / 'job.lock')],
                    ['--unknown', 'x'], ['--owner', 'one', '--owner=two'],
                    ['--organization', '--organization'], ['--lock', str(tmp / 'other')],
                    ['--owner', '--manif=/evil'], ['--'], ['--lock='],
                    ['--config-repo', '../evil'], ['--config-repo', 'owner/']):
            with self.subTest(bad=bad):
                (tmp / 'result').unlink(missing_ok=True)
                result = launch(tmp, repo, helper, good + bad)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertFalse((tmp / 'result').exists())
        service = ['--token-file', '/token', '--timestamp-secret-file', '/secret',
                   '--journal-dir', '/journal', '--owner', 'owner', '--app-id', '123',
                   '--private-key-file', '/key', '--organization', '--lock=' + str(tmp / 'scheduler.lock')]
        result = launch(tmp, repo, helper, service)
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = json.loads((tmp / 'result').read_text())
        self.assertEqual(parsed['lock'], str(tmp / 'scheduler.lock'))
        self.assertTrue(parsed['organization'])
        self.assertEqual(parsed['owner'], 'owner')
        self.assertTrue(parsed['manifest'].endswith('/snapshot/config-forgejo.json'))

    def test_separate_configuration_repository_supplies_the_manifest(self):
        tmp = self.tmp_path
        repo, helper = fixture(tmp)
        # The code snapshot carries one manifest; the configuration repository
        # carries another and must win.
        (repo / 'config-forgejo.json').write_text(json.dumps(['https://github.com/mirror-owner/code-copy']))
        git(repo, 'add', '.'); git(repo, 'commit', '-m', 'code copy')
        config = tmp / 'config-remote'
        config.mkdir()
        git(config, 'init', '-b', 'main')
        git(config, 'config', 'user.email', 'test@example.com')
        git(config, 'config', 'user.name', 'Test')
        authoritative = ['https://github.com/mirror-owner/mirror-from-config-repository']
        (config / 'config-forgejo.json').write_text(json.dumps(authoritative))
        git(config, 'add', '.'); git(config, 'commit', '-m', 'authoritative')
        helper.write_text(
            'import os, sys\n'
            'a = sys.argv[sys.argv.index("--") + 1:]\n'
            'if os.environ.get("CONFIG_REMOTE"):\n'
            '    a = [os.environ["CONFIG_REMOTE"] if x.startswith("https://github.com/") else x for x in a]\n'
            'os.execvp(a[0], a)\n')
        env = dict(os.environ, RESULT=str(tmp / 'result'), CONFIG_REMOTE=str(config))
        result = subprocess.run(
            [sys.executable, str(LAUNCHER), '--remote', str(repo), '--state-dir', str(tmp / 'state'),
             '--job-lock', str(tmp / 'job.lock'), '--helper', str(helper), '--installation', '123456', '--',
             '--config-repo', 'mirror-owner/repo-mirror-config', '--lock', str(tmp / 'scheduler.lock')],
            env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(json.loads((tmp / 'result').read_text())[0]), authoritative)

    def test_scheduler_cleanup_not_killed_after_five_seconds(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            scheduler = tmp / 'scheduler.py'
            scheduler.write_text("import os,pathlib,signal,time\ndef stop(*a):\n    time.sleep(5.5)\n    pathlib.Path(os.environ['DONE']).touch()\n    raise SystemExit(130)\nsignal.signal(signal.SIGTERM, stop)\npathlib.Path(os.environ['READY']).touch()\ntime.sleep(60)\n")
            harness = tmp / 'harness.py'
            harness.write_text("import importlib.util,os,sys\ns=importlib.util.spec_from_file_location('updater', sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)\nf=os.open(sys.argv[2],os.O_CREAT|os.O_RDWR,0o600)\nsys.exit(m.checked_process([sys.executable,sys.argv[3]],cwd='.',env=os.environ,lock_fd=f,scheduler=True))\n")
            env = {**os.environ, 'READY': str(tmp / 'ready'), 'DONE': str(tmp / 'done')}
            child = subprocess.Popen([sys.executable, str(harness), str(LAUNCHER), str(tmp / 'lock'), str(scheduler)], env=env)
            try:
                deadline = time.monotonic() + 5
                while not (tmp / 'ready').exists() and time.monotonic() < deadline: time.sleep(.01)
                self.assertTrue((tmp / 'ready').exists())
                child.send_signal(signal.SIGTERM)
                self.assertEqual(child.wait(timeout=9), 130)
                self.assertTrue((tmp / 'done').exists())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()

    def setUp(self):
        self.tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_signal_during_spawn_is_forwarded(self):
        script = '''
import importlib.util, os, signal, subprocess, sys, tempfile
spec = importlib.util.spec_from_file_location("updater", sys.argv[1])
updater = importlib.util.module_from_spec(spec); spec.loader.exec_module(updater)
real = subprocess.Popen
def spawn(*a, **kw):
    child = real(*a, **kw)
    os.kill(os.getpid(), signal.SIGTERM)
    return child
updater.subprocess.Popen = spawn
with tempfile.TemporaryFile() as lock:
    status = updater.checked_process([sys.executable, "-c", "import time; time.sleep(30)"], cwd="/", env=os.environ, lock_fd=lock.fileno())
sys.exit(0 if status == 143 else 1)
'''
        result = subprocess.run([sys.executable, '-c', script, str(LAUNCHER)], timeout=10)
        self.assertEqual(result.returncode, 0)

    def test_uncooperative_fetch_descendant_is_killed(self):
        tmp = self.tmp_path
        repo, helper = fixture(tmp)
        helper.write_text(
            'import os, pathlib, signal, subprocess, sys, time\n'
            'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
            'p = subprocess.Popen([sys.executable, "-c", '
            '"import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"], close_fds=False)\n'
            'pathlib.Path(os.environ["RESULT"]).write_text(str(p.pid))\n'
            'while True: time.sleep(.05)\n')
        env = dict(os.environ, RESULT=str(tmp / 'result'))
        cmd = [sys.executable, str(LAUNCHER), '--remote', str(repo), '--state-dir', str(tmp / 'state'), '--job-lock', str(tmp / 'job.lock'), '--helper', str(helper), '--installation', '123456', '--', '--lock', str(tmp / 'scheduler.lock')]
        child = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        descendant = None
        try:
            deadline = time.monotonic() + 10
            while not (tmp / 'result').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((tmp / 'result').exists())
            descendant = int((tmp / 'result').read_text())
            self.assertEqual(launch(tmp, repo, helper).returncode, 75)
            child.send_signal(signal.SIGTERM)
            self.assertNotEqual(child.wait(timeout=8), 0)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with (tmp / 'job.lock').open('r') as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(.02)
            else:
                self.fail('descendant retained job lock after escalation')
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            if descendant is not None:
                try:
                    os.kill(descendant, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_fresh_remote_snapshot_every_run(self):
        tmp_path = self.tmp_path
        repo, helper = fixture(tmp_path)
        result = launch(tmp_path, repo, helper)
        assert result.returncode == 0, result.stderr
        first = json.loads((tmp_path / 'result').read_text())
        self.assertIn('Updater fetched commit: ' + git(repo, 'rev-parse', 'HEAD'), result.stderr)
        assert 'mirror-one' in first[0]
        (repo / 'config-forgejo.json').write_text(json.dumps(['https://github.com/mirror-owner/mirror-two']))
        (repo / 'repo_mirror' / 'forgejo_schedule.py').write_text((repo / 'repo_mirror' / 'forgejo_schedule.py').read_text() + '\n# second revision\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'second')
        result = launch(tmp_path, repo, helper)
        assert result.returncode == 0, result.stderr
        second = json.loads((tmp_path / 'result').read_text())
        self.assertIn('Updater fetched commit: ' + git(repo, 'rev-parse', 'HEAD'), result.stderr)
        assert 'mirror-two' in second[0] and 'second revision' in second[1]


    def test_failed_fetch_never_runs_stale_snapshot(self):
        tmp_path = self.tmp_path
        repo, helper = fixture(tmp_path)
        assert launch(tmp_path, repo, helper).returncode == 0
        (tmp_path / 'result').unlink()
        repo.rename(tmp_path / 'gone')
        result = launch(tmp_path, repo, helper)
        assert result.returncode != 0
        assert not (tmp_path / 'result').exists()


    def test_fetch_failure_does_not_log_helper_secrets(self):
        tmp_path = self.tmp_path
        repo, helper = fixture(tmp_path)
        secret = 'test-secret-fetch-token-12345'
        helper.write_text(
            'import sys\n'
            f'print({secret!r}, flush=True)\n'
            f'print({secret!r}, file=sys.stderr, flush=True)\n'
            'raise SystemExit(1)\n'
        )
        result = launch(tmp_path, repo, helper)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Updater failed', result.stderr)
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertFalse((tmp_path / 'result').exists())

    def test_failure_diagnostics_identify_stage_without_helper_output(self):
        tmp = self.tmp_path
        repo, helper = fixture(tmp)
        helper.write_text('import sys\nprint("sensitive-token", file=sys.stderr)\nraise SystemExit(1)\n')
        result = launch(tmp, repo, helper)
        self.assertEqual(result.returncode, 2)
        self.assertIn('Updater failed: fetch', result.stderr)
        self.assertNotIn('sensitive-token', result.stderr)
        helper.write_text('import os, sys\na=sys.argv[sys.argv.index("--")+1:]\nos.execvp(a[0], a)\n')
        (repo / 'config-forgejo.json').write_text('["https://evil.invalid/repo"]')
        git(repo, 'add', '.'); git(repo, 'commit', '-m', 'invalid')
        result = launch(tmp, repo, helper)
        self.assertEqual(result.returncode, 2)
        self.assertIn('Updater failed: manifest validation', result.stderr)
        self.assertFalse((tmp / 'result').exists())

    def test_invalid_manifest_fails_before_scheduler(self):
        tmp_path = self.tmp_path
        repo, helper = fixture(tmp_path)
        (repo / 'config-forgejo.json').write_text('["https://evil.invalid/repo"]')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'invalid')
        assert launch(tmp_path, repo, helper).returncode != 0
        assert not (tmp_path / 'result').exists()


    def test_lock_contention_preserves_unrelated_state(self):
        tmp_path = self.tmp_path
        repo, helper = fixture(tmp_path)
        state = tmp_path / 'state'
        state.mkdir(mode=0o700)
        (state / 'important').write_text('untouched')
        with (tmp_path / 'job.lock').open('w') as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            assert launch(tmp_path, repo, helper).returncode == 75
        assert (state / 'important').read_text() == 'untouched'
        assert not (tmp_path / 'result').exists()


    def test_scheduler_status_is_preserved(self):
        tmp_path = self.tmp_path
        repo, helper = fixture(tmp_path)
        (repo / 'repo_mirror' / 'forgejo_schedule.py').write_text('raise SystemExit(37)\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'exit')
        assert launch(tmp_path, repo, helper).returncode == 37


    def test_git_environment_cannot_override_fetched_commit(self):
        tmp_path = self.tmp_path
        repo, helper = fixture(tmp_path)
        with patch.dict(os.environ, {'GIT_OBJECT_DIRECTORY': str(tmp_path / 'does-not-exist')}):
            result = launch(tmp_path, repo, helper)
        assert result.returncode == 0, result.stderr


    def test_lock_held_during_scheduler_and_term_forwarded(self):
        tmp_path = self.tmp_path
        repo, helper = fixture(tmp_path)
        (repo / 'repo_mirror' / 'forgejo_schedule.py').write_text(
            'import os, pathlib, signal, time\n'
            'signal.signal(signal.SIGTERM, lambda *a: exit(42))\n'
            'pathlib.Path(os.environ["RESULT"]).write_text("started")\n'
            'while True: time.sleep(0.05)\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'wait')
        env = dict(os.environ, RESULT=str(tmp_path / 'result'))
        cmd = [sys.executable, str(LAUNCHER), '--remote', str(repo), '--state-dir', str(tmp_path / 'state'), '--job-lock', str(tmp_path / 'job.lock'), '--helper', str(helper), '--installation', '123456', '--', '--lock', str(tmp_path / 'scheduler.lock')]
        child = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not (tmp_path / 'result').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            assert (tmp_path / 'result').exists()
            assert launch(tmp_path, repo, helper).returncode == 75
            child.send_signal(signal.SIGTERM)
            assert child.wait(timeout=10) == 42
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            if child.stdout is not None:
                child.stdout.close()
            if child.stderr is not None:
                child.stderr.close()

    def test_helper_defaults_to_sibling_github_app(self):
        tmp = self.tmp_path
        repo, helper = fixture(tmp)
        installed = tmp / 'installed'
        installed.mkdir(mode=0o700)
        launcher = installed / 'auto_update.py'
        launcher.write_text(LAUNCHER.read_text())
        sibling = installed / 'github_app.py'
        sibling.write_text(
            'import json, os, pathlib, sys\n'
            'idx = sys.argv.index("--")\n'
            'pathlib.Path(os.environ["HELPER_CALL"]).write_text(json.dumps({"script": str(pathlib.Path(__file__).resolve()), "args": sys.argv[1:idx]}))\n'
            'a = sys.argv[idx + 1:]\n'
            'os.execvp(a[0], a)\n'
        )
        env = dict(os.environ, RESULT=str(tmp / 'result'), HELPER_CALL=str(tmp / 'helper_call'))
        result = subprocess.run(
            [sys.executable, str(launcher), '--remote', str(repo), '--state-dir', str(tmp / 'state'),
             '--job-lock', str(tmp / 'job.lock'), '--installation', '123456', '--',
             '--lock', str(tmp / 'scheduler.lock')],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        call = json.loads((tmp / 'helper_call').read_text())
        self.assertEqual(call['script'], str(sibling.resolve()))
        self.assertEqual(call['args'], ['--installation', '123456', 'run'])
        self.assertTrue((tmp / 'result').exists())

    def test_explicit_helper_overrides_sibling_default(self):
        tmp = self.tmp_path
        repo, helper = fixture(tmp)
        installed = tmp / 'installed'
        installed.mkdir(mode=0o700)
        launcher = installed / 'auto_update.py'
        launcher.write_text(LAUNCHER.read_text())
        sibling = installed / 'github_app.py'
        sibling.write_text('raise SystemExit(99)\n')
        helper.write_text(
            'import json, os, pathlib, sys\n'
            'idx = sys.argv.index("--")\n'
            'pathlib.Path(os.environ["HELPER_CALL"]).write_text(json.dumps({"script": str(pathlib.Path(__file__).resolve())}))\n'
            'a = sys.argv[idx + 1:]\n'
            'os.execvp(a[0], a)\n'
        )
        for label, helper_arg in (('absolute', str(helper)), ('relative', 'helper.py')):
            with self.subTest(mode=label):
                (tmp / 'result').unlink(missing_ok=True)
                (tmp / 'helper_call').unlink(missing_ok=True)
                env = dict(os.environ, RESULT=str(tmp / 'result'), HELPER_CALL=str(tmp / 'helper_call'))
                result = subprocess.run(
                    [sys.executable, str(launcher), '--remote', str(repo), '--state-dir', str(tmp / 'state'),
                     '--job-lock', str(tmp / 'job.lock'), '--helper', helper_arg, '--installation', '123456', '--',
                     '--lock', str(tmp / 'scheduler.lock')],
                    cwd=tmp, env=env, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads((tmp / 'helper_call').read_text())['script'], str(helper.resolve()))
                self.assertTrue((tmp / 'result').exists())

    def test_missing_default_helper_fails_closed_without_fetch(self):
        tmp = self.tmp_path
        repo, _helper = fixture(tmp)
        installed = tmp / 'installed'
        installed.mkdir(mode=0o700)
        launcher = installed / 'auto_update.py'
        launcher.write_text(LAUNCHER.read_text())
        env = dict(os.environ, RESULT=str(tmp / 'result'))
        result = subprocess.run(
            [sys.executable, str(launcher), '--remote', str(repo), '--state-dir', str(tmp / 'state'),
             '--job-lock', str(tmp / 'job.lock'), '--installation', '123456', '--',
             '--lock', str(tmp / 'scheduler.lock')],
            env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn('GitHub App helper path is required', result.stderr)
        self.assertFalse((tmp / 'state').exists())
        self.assertFalse((tmp / 'job.lock').exists())
        self.assertFalse((tmp / 'result').exists())

    def test_helper_path_rejects_leading_dash_non_regular_and_wrong_owner(self):
        import importlib.util
        import io
        from contextlib import redirect_stderr
        tmp = self.tmp_path
        repo, helper = fixture(tmp)
        dash_helper = tmp / '-helper.py'
        dash_helper.write_text(helper.read_text())
        symlink_helper = tmp / 'link_helper.py'
        symlink_helper.symlink_to(helper)
        dir_helper = tmp / 'dir_helper'
        dir_helper.mkdir()
        cases = (
            ('leading-dash', '-helper.py'),
            ('symlink', str(symlink_helper)),
            ('directory', str(dir_helper)),
            ('missing', str(tmp / 'missing.py')),
        )
        for label, helper_arg in cases:
            with self.subTest(case=label):
                result = subprocess.run(
                    [sys.executable, str(LAUNCHER), '--remote', str(repo), '--state-dir', str(tmp / 'state'),
                     '--job-lock', str(tmp / 'job.lock'), '--helper', helper_arg, '--installation', '123456', '--',
                     '--lock', str(tmp / 'scheduler.lock')],
                    cwd=tmp, env=dict(os.environ, RESULT=str(tmp / 'result')), capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 2)
                self.assertFalse((tmp / 'state').exists())
                self.assertFalse((tmp / 'result').exists())

        spec = importlib.util.spec_from_file_location('auto_update_owner_check', LAUNCHER)
        updater = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(updater)
        other_uid = os.getuid() + 1
        stderr = io.StringIO()
        with patch.object(updater.os, 'getuid', return_value=other_uid), redirect_stderr(stderr):
            rc = updater.main([
                '--remote', str(repo), '--state-dir', str(tmp / 'state'),
                '--job-lock', str(tmp / 'job.lock'), '--helper', str(helper),
                '--installation', '123456', '--', '--lock', str(tmp / 'scheduler.lock'),
            ])
        self.assertEqual(rc, 2)
        self.assertIn('Updater failed: configuration', stderr.getvalue())
        self.assertFalse((tmp / 'state').exists())
        self.assertFalse((tmp / 'result').exists())
