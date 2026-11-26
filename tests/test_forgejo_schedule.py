import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'repo_mirror' / 'forgejo_schedule.py'


class ScheduleTest(unittest.TestCase):
    def test_streams_only_sanitized_progress_while_worker_is_running(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            secret = tmp / 'secret'; secret.write_text('x' * 32); secret.chmod(0o600)
            journals = tmp / 'journals'; journals.mkdir(mode=0o700)
            manifest = tmp / 'sources'; manifest.write_text(json.dumps(['https://localhost/a/b']))
            worker = tmp / 'worker'
            worker.write_text('''#!/bin/sh
printf 'step git source-snapshot elapsed=0.100s\n' >&2
printf '===== Release 1/2: v1.2.3 =====\n' >&2
printf 'release 1/2 started tag=v1.2.3\n' >&2
printf 'asset 1/2 started asset_name=build.tar.gz\n' >&2
printf 'asset 2/2 started asset_name=SECRET_ASSET\n' >&2
printf 'release 2/2 started tag=https://private.example/signed\n' >&2
printf 'step git snapshot-push elapsed=0.200s SECRET_SUFFIX\n' >&2
printf 'SECRET_RAW https://private.example/signed\n' >&2
printf 'assets uploaded=1 updated=0 skipped=1 retried=0 upload_failed=0\n' >&2
for n in 1 2 3 4 5; do
  if [ -e "$ACK" ]; then exit 0; fi
  sleep 0.2
done
exit 1
''')
            worker.chmod(0o700)
            cmd = [sys.executable, str(SCRIPT), 'run', '--manifest', str(manifest), '--token-file', 'unused',
                   '--timestamp-secret-file', str(secret), '--journal-dir', str(journals), '--owner', 'alice',
                   '--lock', str(tmp / 'lock'), '--sync-python', str(worker)]
            ack = tmp / 'ack'
            with subprocess.Popen(cmd, env={**os.environ, 'ACK': str(ack)}, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True) as child:
                log = []
                assert child.stdout is not None
                for line in child.stdout:
                    log.append(line)
                    if 'step git source-snapshot elapsed=0.100s' in line:
                        ack.touch()
                self.assertEqual(child.wait(timeout=5), 0, ''.join(log))
            output = ''.join(log)
            self.assertIn('===== Repository 1/1: a/b =====', output)
            self.assertIn('repository 1/1 started', output)
            self.assertIn('===== Release 1/2: v1.2.3 =====', output)
            self.assertIn('repository 1/1 release 1/2 started tag=v1.2.3', output)
            self.assertIn('repository 1/1 asset 1/2 started asset_name=build.tar.gz', output)
            self.assertIn('repository 1/1 asset 2/2 started asset_name=[redacted]', output)
            self.assertIn('assets uploaded=1 updated=0 skipped=1 retried=0 upload_failed=0', output)
            self.assertIn('repository 1/1 completed', output)
            self.assertIn('===== Summary =====', output)
            self.assertNotIn('SECRET', output)
            self.assertNotIn('private.example', output)

    def test_parser_rejects_unsafe_names_and_lookalike_host(self):
        from repo_mirror.forgejo_schedule import validate_sources
        for source in ('https://github.com.evil.example/a/b',
                       'https://forge.example/-owner/repo', 'https://forge.example/owner/-repo'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                validate_sources(['https://localhost/ok/first', source])

    def test_scheduler_reports_relocation_failure_without_losing_safe_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            secret = tmp / 'secret'; secret.write_text('x' * 32); secret.chmod(0o600)
            journals = tmp / 'journals'; journals.mkdir(mode=0o700)
            manifest = tmp / 'sources'; manifest.write_text(json.dumps(['https://localhost/a/b']))
            worker = tmp / 'worker'
            worker.write_text('#!/bin/sh\nprintf "%s\\n" "FORGEJO_FAILURE: source relocation requires durable migration state and authoritative config publication" >&2\nexit 1\n')
            worker.chmod(0o700)
            cmd = [sys.executable, str(SCRIPT), 'run', '--manifest', str(manifest), '--token-file', 'unused', '--timestamp-secret-file', str(secret), '--journal-dir', str(journals), '--owner', 'alice', '--lock', str(tmp / 'lock'), '--sync-python', str(worker)]
            result = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('source relocation requires durable migration state and authoritative config publication', result.stdout)

    def test_scheduler_preserves_manifest_bytes_for_remote_cas(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            secret = tmp / 'secret'; secret.write_text('x' * 32); secret.chmod(0o600)
            journals = tmp / 'journals'; journals.mkdir(mode=0o700)
            original = b'[\n  "https://localhost/a/b"\n]\n'
            manifest = tmp / 'sources'; manifest.write_bytes(original)
            worker = tmp / 'worker'
            worker.write_text('#!/bin/sh\nwhile [ "$1" != "--config" ]; do shift; done\ncp "$2" "$CAPTURE"\n')
            worker.chmod(0o700)
            cmd = [sys.executable, str(SCRIPT), 'run', '--manifest', str(manifest), '--token-file', 'unused', '--timestamp-secret-file', str(secret), '--journal-dir', str(journals), '--owner', 'alice', '--lock', str(tmp / 'lock'), '--sync-python', str(worker)]
            result = subprocess.run(cmd, env={**os.environ, 'CAPTURE': str(tmp / 'capture')}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((tmp / 'capture').read_bytes(), original)

    def test_public_forgejo_worker_does_not_require_github_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            secret = tmp / 'secret'; secret.write_text('x' * 32); secret.chmod(0o600)
            journals = tmp / 'journals'; journals.mkdir(mode=0o700)
            manifest = tmp / 'sources'; manifest.write_text(json.dumps(['https://localhost/a/b']))
            worker = tmp / 'worker'; worker.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGS"\n'); worker.chmod(0o700)
            cmd = [sys.executable, str(SCRIPT), 'run', '--manifest', str(manifest), '--token-file', 'unused', '--timestamp-secret-file', str(secret), '--journal-dir', str(journals), '--owner', 'alice', '--lock', str(tmp / 'lock'), '--helper', str(tmp / 'missing-helper'), '--sync-python', str(worker)]
            result = subprocess.run(cmd, env={**os.environ, 'ARGS': str(tmp / 'args')}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn('--use-github-token', (tmp / 'args').read_text())

    def test_invalid_sources_rejected_before_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            manifest = tmp / 'manifest'
            cmd = [sys.executable, str(SCRIPT), 'run', '--manifest', str(manifest), '--token-file', 'unused', '--timestamp-secret-file', 'unused', '--journal-dir', 'unused', '--owner', 'alice', '--lock', str(tmp / 'lock')]
            for source in ['http://forge.example/a/b', 'https://forge.example/a/b.git',
                           'https://user@forge.example/a/b', 'https://forge.example/a/b?q=1',
                           'https://github.com/other/repo', 'https://forge.example:99999/a/b',
                           'https://github.com.evil.example/a/b', 'https://forge.example/-owner/repo',
                           'https://forge.example/owner/-repo']:
                with self.subTest(source=source):
                    manifest.write_text(json.dumps([source]))
                    self.assertEqual(subprocess.run(cmd, capture_output=True).returncode, 2)
                    self.assertFalse((tmp / 'lock').exists())

    def test_mixed_sources_pass_original_urls_and_reject_collisions(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            secret = tmp / 'secret'; secret.write_text('x' * 32); secret.chmod(0o600)
            journals = tmp / 'journals'; journals.mkdir(mode=0o700)
            manifest = tmp / 'sources.json'
            sources = ['https://github.com/manoma080/mirror-a', 'https://Forge.Example:443/Owner/Repo']
            manifest.write_text(json.dumps(sources))
            helper = tmp / 'helper.py'
            helper.write_text('import os,sys\na=sys.argv[sys.argv.index("--")+1:]\nos.execvp(a[0], a)\n')
            worker = tmp / 'worker'
            worker.write_text('#!/bin/sh\nfor last; do :; done\nprintf "%s\\n" "$last" >> "$CALLS"\n'); worker.chmod(0o700)
            cmd = [sys.executable, str(SCRIPT), 'run', '--manifest', str(manifest), '--token-file', 'unused', '--timestamp-secret-file', str(secret), '--journal-dir', str(journals), '--owner', 'alice', '--lock', str(tmp / 'lock'), '--helper', str(helper), '--sync-python', str(worker)]
            env = {**os.environ, 'CALLS': str(tmp / 'calls')}
            result = subprocess.run(cmd, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertCountEqual((tmp / 'calls').read_text().splitlines(), sources)
            (tmp / 'calls').unlink(); (tmp / 'lock').unlink()
            manifest.write_text(json.dumps([sources[0], 'https://other.example/other/MIRROR-a']))
            result = subprocess.run(cmd, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse((tmp / 'calls').exists())
            self.assertFalse((tmp / 'lock').exists())

    def test_four_children_overlap_and_pool_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            secret = tmp / 'key'
            secret.write_text('x' * 32)
            secret.chmod(0o600)
            journals = tmp / 'journals'
            journals.mkdir(mode=0o700)
            manifest = tmp / 'sources.json'
            manifest.write_text(json.dumps([f'https://github.com/manoma080/mirror-{i}' for i in range(8)]))
            helper = tmp / 'helper.py'
            helper.write_text('''import fcntl, os, pathlib, sys, time
p = pathlib.Path(os.environ["STATE"])
with open(p / "guard", "w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    name = sys.argv[-1].split("-")[-1]
    (p / (name + ".started")).touch()
    (p / (name + ".active")).touch()
    if len(list(p.glob("*.active"))) > 4: (p / "overlimit").touch()
deadline = time.monotonic() + 5
while len(list(p.glob("*.started"))) < 4 and time.monotonic() < deadline:
    time.sleep(.01)
if len(list(p.glob("*.started"))) < 4: sys.exit(3)
time.sleep(.1)
with open(p / "guard", "w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    (p / (name + ".active")).unlink()
''')
            result = subprocess.run([sys.executable, str(SCRIPT), 'run', '--manifest', str(manifest), '--token-file', 'unused', '--timestamp-secret-file', str(secret), '--journal-dir', str(journals), '--owner', 'alice', '--lock', str(tmp / 'lock'), '--helper', str(helper), '--jobs', '4'], env={**os.environ, 'STATE': str(tmp)}, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(len(list(tmp.glob('*.started'))), 8)
            self.assertFalse((tmp / 'overlimit').exists())

    def test_timeout_kills_process_group_and_continues(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            secret = tmp / 'key'
            secret.write_text('x' * 32)
            secret.chmod(0o600)
            journals = tmp / 'journals'
            journals.mkdir(mode=0o700)
            manifest = tmp / 'sources.json'
            manifest.write_text(json.dumps(['https://github.com/manoma080/mirror-a', 'https://github.com/manoma080/mirror-b']))
            helper = tmp / 'helper.py'
            helper.write_text('''import os, pathlib, signal, subprocess, sys, time
if sys.argv[-1].endswith('-a'):
    p = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
    pathlib.Path(os.environ['PIDFILE']).write_text(str(p.pid))
    print('PRIVATE_SECRET', file=sys.stderr, flush=True)
    time.sleep(60)
else:
    time.sleep(.15)
''')
            result = subprocess.run([sys.executable, str(SCRIPT), 'run', '--manifest', str(manifest), '--token-file', 'unused', '--timestamp-secret-file', str(secret), '--journal-dir', str(journals), '--owner', 'alice', '--lock', str(tmp / 'lock'), '--helper', str(helper), '--jobs', '1', '--repository-timeout', '.5'], env={**os.environ, 'PIDFILE': str(tmp / 'pid')}, capture_output=True, text=True, timeout=8)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn('1 succeeded, 1 failed', result.stdout)
            self.assertIn('timed out', result.stdout)
            self.assertNotIn('PRIVATE_SECRET', result.stdout + result.stderr)
            pid = int((tmp / 'pid').read_text())
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    state = Path(f'/proc/{pid}/stat').read_text().split()[2]
                except FileNotFoundError:
                    break
                if state == 'Z':
                    break
                time.sleep(.02)
            else:
                self.fail('grandchild is still running')

    def test_sigterm_stops_running_children_and_releases_lock(self):
        import fcntl
        import signal
        import time
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            secret = tmp / 'key'
            secret.write_text('x' * 32)
            secret.chmod(0o600)
            journals = tmp / 'journals'
            journals.mkdir(mode=0o700)
            manifest = tmp / 'sources.json'
            manifest.write_text(json.dumps(['https://github.com/manoma080/mirror-a']))
            helper = tmp / 'helper.py'
            helper.write_text('import os,pathlib,subprocess,sys,time\np = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])\ntime.sleep(.2)\npathlib.Path(os.environ["MARK"]).write_text(str(os.getpid())+" "+str(p.pid))\ntime.sleep(60)\n')
            lock = tmp / 'lock'
            child = subprocess.Popen([sys.executable, str(SCRIPT), 'run', '--manifest', str(manifest), '--token-file', 'unused', '--timestamp-secret-file', str(secret), '--journal-dir', str(journals), '--owner', 'alice', '--lock', str(lock), '--helper', str(helper)], env={**os.environ, 'MARK': str(tmp / 'mark')}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while not (tmp / 'mark').exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue((tmp / 'mark').exists())
                pid, grandchild = map(int, (tmp / 'mark').read_text().split())
                child.send_signal(signal.SIGTERM)
                child.communicate(timeout=10)
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        state = Path(f'/proc/{grandchild}/stat').read_text().split()[2]
                    except FileNotFoundError:
                        break
                    if state == 'Z':
                        break
                    time.sleep(.02)
                else:
                    self.fail('grandchild survived cancellation')
                with lock.open('r+') as held:
                    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate()

    def test_signal_during_spawn_reaps_worker_and_releases_lock(self):
        import fcntl
        import signal
        import time
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            secret = tmp / 'key'
            secret.write_text('x' * 32)
            secret.chmod(0o600)
            journals = tmp / 'journals'
            journals.mkdir(mode=0o700)
            manifest = tmp / 'sources.json'
            manifest.write_text(json.dumps(['https://github.com/manoma080/mirror-a']))
            helper = tmp / 'helper.py'
            helper.write_text("import os,pathlib,time\npathlib.Path(os.environ['MARK']).write_text(str(os.getpid()))\ntime.sleep(60)\n")
            harness = tmp / 'harness.py'
            harness.write_text("""import os, signal, subprocess, sys, time
from pathlib import Path
from repo_mirror import forgejo_schedule
real = subprocess.Popen
def spawn(*args, **kwargs):
    p = real(*args, **kwargs)
    while not Path(os.environ['MARK']).exists(): time.sleep(.01)
    os.kill(os.getpid(), signal.SIGTERM)
    return p
forgejo_schedule.subprocess.Popen = spawn
sys.exit(forgejo_schedule.main(sys.argv[1:]))
""")
            lock = tmp / 'lock'
            command = [sys.executable, str(harness), 'run', '--manifest', str(manifest), '--token-file', 'unused', '--timestamp-secret-file', str(secret), '--journal-dir', str(journals), '--owner', 'alice', '--lock', str(lock), '--helper', str(helper)]
            env = {**os.environ, 'MARK': str(tmp / 'mark'), 'PYTHONPATH': str(ROOT)}
            worker = None
            try:
                result = subprocess.run(command, env=env, capture_output=True, timeout=8)
                worker = int((tmp / 'mark').read_text())
                self.assertEqual(result.returncode, 130, result.stderr)
                with self.assertRaises(ProcessLookupError):
                    os.kill(worker, 0)
                with lock.open('r+') as held:
                    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                if worker is None and (tmp / 'mark').exists():
                    worker = int((tmp / 'mark').read_text())
                if worker:
                    try: os.killpg(worker, signal.SIGKILL)
                    except ProcessLookupError: pass

    def test_completed_helper_does_not_leave_writing_descendant(self):
        import signal
        import time
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            secret = tmp / 'key'
            secret.write_text('x' * 32)
            secret.chmod(0o600)
            journals = tmp / 'journals'
            journals.mkdir(mode=0o700)
            manifest = tmp / 'sources.json'
            manifest.write_text(json.dumps(['https://github.com/manoma080/mirror-a']))
            helper = tmp / 'helper.py'
            helper.write_text("import os,pathlib,subprocess,sys,time\np=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\npathlib.Path(os.environ['MARK']).write_text(str(p.pid))\n")
            command = [sys.executable, str(SCRIPT), 'run', '--manifest', str(manifest), '--token-file', 'unused', '--timestamp-secret-file', str(secret), '--journal-dir', str(journals), '--owner', 'alice', '--lock', str(tmp / 'lock'), '--helper', str(helper)]
            pid = None
            try:
                result = subprocess.run(command, env={**os.environ, 'MARK': str(tmp / 'mark')}, capture_output=True, timeout=8)
                self.assertEqual(result.returncode, 0, result.stderr)
                pid = int((tmp / 'mark').read_text())
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    try: state = Path(f'/proc/{pid}/stat').read_text().split()[2]
                    except FileNotFoundError: break
                    if state == 'Z': break
                    time.sleep(.01)
                else: self.fail('descendant survived successful batch')
            finally:
                if pid is None and (tmp / 'mark').exists(): pid = int((tmp / 'mark').read_text())
                if pid:
                    try: os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError: pass

    def test_sigint_cleanup_is_bounded_and_keeps_lock_until_workers_stop(self):
        import fcntl
        import signal
        import time
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            secret = tmp / 'key'
            secret.write_text('x' * 32)
            secret.chmod(0o600)
            journals = tmp / 'journals'
            journals.mkdir(mode=0o700)
            manifest = tmp / 'sources.json'
            manifest.write_text(json.dumps([f'https://github.com/manoma080/mirror-{i}' for i in range(4)]))
            helper = tmp / 'helper.py'
            helper.write_text("import os,pathlib,signal,time\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\npathlib.Path(os.environ['STATE'],str(os.getpid())).touch()\ntime.sleep(60)\n")
            lock = tmp / 'lock'
            command = [sys.executable, str(SCRIPT), 'run', '--manifest', str(manifest), '--token-file', 'unused', '--timestamp-secret-file', str(secret), '--journal-dir', str(journals), '--owner', 'alice', '--lock', str(lock), '--helper', str(helper), '--jobs', '4']
            state = tmp / 'workers'
            state.mkdir()
            child = subprocess.Popen(command, env={**os.environ, 'STATE': str(state)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pids = []
            try:
                deadline = time.monotonic() + 8
                while len(list(state.iterdir())) < 4 and time.monotonic() < deadline: time.sleep(.01)
                pids = [int(p.name) for p in state.iterdir()]
                self.assertEqual(len(pids), 4)
                started = time.monotonic()
                child.send_signal(signal.SIGINT)
                time.sleep(.2)
                child.send_signal(signal.SIGTERM)
                with lock.open('r+') as held:
                    with self.assertRaises(BlockingIOError): fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(child.wait(timeout=5), 130)
                self.assertLess(time.monotonic() - started, 5)
                for pid in pids:
                    with self.assertRaises(ProcessLookupError): os.kill(pid, 0)
                with lock.open('r+') as held: fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
                for pid in pids:
                    try: os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError: pass

    def test_short_secret_is_rejected_before_child(self):
        from unittest.mock import patch
        from repo_mirror import forgejo_schedule
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            manifest = tmp / 'sources.json'
            manifest.write_text(json.dumps(['https://github.com/manoma080/mirror-a']))
            secret = tmp / 'key'
            secret.write_text('x' * 31)
            secret.chmod(0o600)
            journals = tmp / 'journals'
            journals.mkdir(mode=0o700)
            args = ['run', '--manifest', str(manifest), '--token-file', 'unused', '--owner', 'alice',
                    '--lock', str(tmp / 'lock'), '--timestamp-secret-file', str(secret), '--journal-dir', str(journals)]
            with patch('repo_mirror.forgejo_schedule.subprocess.Popen') as run:
                self.assertEqual(forgejo_schedule.main(args), 2)
                run.assert_not_called()

    def test_recovery_paths_are_passed_unchanged_to_each_child(self):
        from unittest.mock import patch
        from repo_mirror import forgejo_schedule
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            manifest = tmp / 'sources.json'
            manifest.write_text(json.dumps(['https://github.com/manoma080/mirror-a', 'https://github.com/manoma080/mirror-b']))
            secret = tmp / 'recovery-key'
            secret.write_text('test-recovery-value-' * 2)
            secret.chmod(0o600)
            journals = tmp / 'journals'
            journals.mkdir(mode=0o700)
            args = ['run', '--manifest', str(manifest), '--token-file', 'x', '--owner', 'alice',
                    '--lock', str(tmp / 'lock'), '--timestamp-secret-file', str(secret), '--journal-dir', str(journals)]
            with patch('repo_mirror.forgejo_schedule.subprocess.Popen', side_effect=lambda *a, **kw: __import__('unittest.mock', fromlist=['Mock']).Mock(poll=lambda: 0, pid=999999999)) as run:
                self.assertEqual(forgejo_schedule.main(args), 0)
                self.assertEqual(forgejo_schedule.main(args), 0)
            self.assertEqual(run.call_count, 4)
            for call in run.call_args_list:
                command = call.args[0]
                self.assertEqual(command[command.index('--timestamp-secret-file') + 1], str(secret))
                self.assertEqual(command[command.index('--journal-dir') + 1], str(journals))
                self.assertNotIn('test-recovery-value', command)

    def test_rejects_unsafe_recovery_paths_without_starting_child(self):
        from unittest.mock import patch
        from repo_mirror import forgejo_schedule
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            manifest = tmp / 'sources.json'
            manifest.write_text(json.dumps(['https://github.com/manoma080/mirror-a']))
            secret = tmp / 'key'
            secret.write_text('do-not-log-this-value-' * 2)
            secret.chmod(0o600)
            journals = tmp / 'journals'
            journals.mkdir(mode=0o700)
            link = tmp / 'link'
            link.symlink_to(secret)
            hardlink = tmp / 'hardlink'
            os.link(secret, hardlink)
            args = ['run', '--manifest', str(manifest), '--token-file', str(hardlink), '--owner', 'alice',
                    '--lock', str(tmp / 'lock'), '--timestamp-secret-file', str(secret), '--journal-dir', str(journals)]
            with patch('repo_mirror.forgejo_schedule.subprocess.Popen') as run:
                self.assertEqual(forgejo_schedule.main(args), 2)  # hardlink to credential
                args[args.index('--token-file') + 1] = 'unused'
                secret.chmod(0o644)
                self.assertEqual(forgejo_schedule.main(args), 2)
                secret.chmod(0o600)
                args[args.index('--timestamp-secret-file') + 1] = str(link)
                self.assertEqual(forgejo_schedule.main(args), 2)
                args[args.index('--timestamp-secret-file') + 1] = str(secret)
                journals.chmod(0o755)
                self.assertEqual(forgejo_schedule.main(args), 2)
                run.assert_not_called()

    def test_scheduler_templates_supply_recovery_paths(self):
        for name in ('forgejo-mirror.service.template', 'forgejo-mirror.cron.template'):
            template = (ROOT / 'scheduler' / name).read_text()
            self.assertIn('--timestamp-secret-file @TIMESTAMP_SECRET_FILE@', template)
            self.assertIn('--journal-dir @JOURNAL_DIR@', template)
            self.assertIn('@PYTHON@ @UPDATER@ --remote @REMOTE@ --state-dir @STATE_DIR@ --job-lock @JOB_LOCK_FILE@ --helper-python @HELPER_PYTHON@ --', template)
            self.assertNotIn('--manifest', template)
            self.assertNotIn('-m repo_mirror.forgejo_schedule', template)
            self.assertIn('--token-file @TOKEN_FILE@', template)
            self.assertIn('--lock @LOCK_FILE@', template)
            self.assertNotIn('repo-mirror-forgejo-feature', template)

    def test_app_mode_passes_refreshable_credentials_to_child_without_helper(self):
        from unittest.mock import patch
        from repo_mirror import forgejo_schedule
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            secret = tmp / 'recovery-key'
            secret.write_text('test-key-' * 4)
            secret.chmod(0o600)
            journals = tmp / 'journals'
            journals.mkdir(mode=0o700)
            manifest = tmp / 'sources.json'
            manifest.write_text(json.dumps(['https://github.com/manoma080/mirror-a']))
            with patch('repo_mirror.forgejo_schedule.subprocess.Popen', side_effect=lambda *a, **kw: __import__('unittest.mock', fromlist=['Mock']).Mock(poll=lambda: 0, pid=999999999)) as run:
                result = forgejo_schedule.main(['run', '--manifest', str(manifest), '--token-file', 'x',
                    '--timestamp-secret-file', str(secret), '--journal-dir', str(journals), '--owner', 'alice', '--lock', str(tmp / 'lock'), '--app-id', '123', '--private-key-file', '/key'])
            self.assertEqual(result, 0)
            command = run.call_args.args[0]
            self.assertEqual(command[0], sys.executable)
            self.assertIn('repo_mirror.forgejo_sync', command)
            self.assertIn('--private-key-file', command)
            self.assertNotIn('run', command)

    def test_run_refreshes_per_source_and_suppresses_child_output(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            secret = tmp / 'recovery-key'
            secret.write_text('test-key-' * 4)
            secret.chmod(0o600)
            journals = tmp / 'journals'
            journals.mkdir(mode=0o700)
            sources = ['https://github.com/manoma080/mirror-a', 'https://github.com/manoma080/mirror-b']
            manifest = tmp / 'sources.json'
            manifest.write_text(json.dumps(sources))
            calls = tmp / 'calls'
            helper = tmp / 'helper.py'
            helper.write_text('import os,sys\nwith open(os.environ["CALLS"], "a") as f: f.write(sys.argv[-1]+"\\n")\nprint("SECRET_FROM_CHILD")\n')
            result = subprocess.run([sys.executable, str(SCRIPT), 'run', '--manifest', str(manifest), '--token-file', str(tmp / 'token'), '--timestamp-secret-file', str(secret), '--journal-dir', str(journals), '--owner', 'alice', '--lock', str(tmp / 'lock'), '--helper', str(helper), '--helper-python', sys.executable], env={**os.environ, 'CALLS': str(calls)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertCountEqual(calls.read_text().splitlines(), sources)
            self.assertNotIn('SECRET_FROM_CHILD', result.stdout + result.stderr)

    def test_rejects_invalid_manifest_before_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            secret = tmp / 'recovery-key'
            secret.write_text('test-key-' * 4)
            secret.chmod(0o600)
            journals = tmp / 'journals'
            journals.mkdir(mode=0o700)
            manifest = tmp / 'sources.json'
            manifest.write_text(json.dumps(['https://github.com/manoma080/mirror-a', 'https://github.com/manoma080/MIRROR-a']))
            result = subprocess.run([sys.executable, str(SCRIPT), 'run', '--manifest', str(manifest), '--token-file', 'unused', '--timestamp-secret-file', str(secret), '--journal-dir', str(journals), '--owner', 'alice', '--lock', str(tmp / 'lock')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse((tmp / 'lock').exists())

    def test_lock_prevents_overlapping_runs(self):
        import fcntl
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            secret = tmp / 'recovery-key'
            secret.write_text('test-key-' * 4)
            secret.chmod(0o600)
            journals = tmp / 'journals'
            journals.mkdir(mode=0o700)
            manifest = tmp / 'sources.json'
            manifest.write_text(json.dumps(['https://github.com/manoma080/mirror-a']))
            lock = tmp / 'lock'
            lock.touch(mode=0o600)
            with lock.open('r+') as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                result = subprocess.run([sys.executable, str(SCRIPT), 'run', '--manifest', str(manifest), '--token-file', 'unused', '--timestamp-secret-file', str(secret), '--journal-dir', str(journals), '--owner', 'alice', '--lock', str(lock)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 75)

    def test_failure_does_not_stop_later_repositories(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            secret = tmp / 'recovery-key'
            secret.write_text('test-key-' * 4)
            secret.chmod(0o600)
            journals = tmp / 'journals'
            journals.mkdir(mode=0o700)
            manifest = tmp / 'sources.json'
            manifest.write_text(json.dumps(['https://github.com/manoma080/mirror-a', 'https://github.com/manoma080/mirror-b']))
            helper = tmp / 'helper.py'
            helper.write_text('import sys\nprint("secret", file=sys.stderr)\nsys.exit(1 if sys.argv[-1].endswith("-a") else 0)\n')
            result = subprocess.run([sys.executable, str(SCRIPT), 'run', '--manifest', str(manifest), '--token-file', 'unused', '--timestamp-secret-file', str(secret), '--journal-dir', str(journals), '--owner', 'alice', '--lock', str(tmp / 'lock'), '--helper', str(helper)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('1 succeeded, 1 failed', result.stdout)
            self.assertNotIn('secret', result.stdout + result.stderr)

    def test_failure_reports_only_allowlisted_child_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            secret = tmp / 'recovery-key'
            secret.write_text('test-key-' * 4)
            secret.chmod(0o600)
            journals = tmp / 'journals'
            journals.mkdir(mode=0o700)
            manifest = tmp / 'sources.json'
            manifest.write_text(json.dumps(['https://github.com/manoma080/mirror-a']))
            helper = tmp / 'helper.py'
            helper.write_text('import sys\nprint("FORGEJO_FAILURE: release metadata collision", file=sys.stderr)\nprint("FORGEJO_FAILURE: release metadata collision secret", file=sys.stderr)\nprint("secret https://example/token", file=sys.stderr)\nsys.exit(1)\n')
            result = subprocess.run([sys.executable, str(SCRIPT), 'run', '--manifest', str(manifest), '--token-file', 'unused', '--timestamp-secret-file', str(secret), '--journal-dir', str(journals), '--owner', 'alice', '--lock', str(tmp / 'lock'), '--helper', str(helper)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('release metadata collision', result.stdout + result.stderr)
            self.assertNotIn('secret', result.stdout + result.stderr)
            self.assertNotIn('https://example', result.stdout + result.stderr)

    def test_timer_has_startup_and_persistent_six_hour_schedule(self):
        timer = (ROOT / 'scheduler/forgejo-mirror.timer').read_text()
        self.assertIn('OnBootSec=', timer)
        self.assertIn('OnCalendar=*-*-* 00,06,12,18:00:00', timer)
        self.assertIn('Persistent=true', timer)

    def test_service_template_selects_refreshable_app_credentials(self):
        template = (ROOT / 'scheduler/forgejo-mirror.service.template').read_text()
        self.assertIn('--app-id @APP_ID@', template)
        self.assertIn('--private-key-file @PRIVATE_KEY_FILE@', template)

    def test_cron_template_selects_refreshable_app_credentials(self):
        template = (ROOT / 'scheduler/forgejo-mirror.cron.template').read_text()
        self.assertIn('--app-id @APP_ID@', template)
        self.assertIn('--private-key-file @PRIVATE_KEY_FILE@', template)
        self.assertNotIn('--use-github-token', template)

    def test_inventory_manifest_has_exactly_selected_destinations(self):
        expected = [
            'https://github.com/example-owner/example-app',
            'https://github.com/example-org-a/project-one',
            'https://github.com/example-org-a/project-two',
            'https://github.com/example-org-b/project-three',
            'https://github.com/manoma080/mirror-fixture-owner-fixture-app',
            'https://github.com/manoma080/mirror-fixture-org-a-project-four',
        ]
        from repo_mirror.forgejo_sync import parse_manifest

        fixture_manifest = {
            'destinations': {'local': {}},
            'repositories': [
                {'source': {'url': url}, 'mirrors': {'local': {}}}
                for url in expected
            ],
        }
        entries = parse_manifest(fixture_manifest)
        actual = [entry['url'] for entry in entries]
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), len(expected))
        self.assertEqual(len({url.lower() for url in actual}), len(expected))
        self.assertEqual(
            [entry['destination_name'] for entry in entries],
            [url.rsplit('/', 1)[-1] for url in expected],
        )
        skipped = {
            'https://github.com/example-org-c/project-skipped-one',
            'https://github.com/example-org-c/project-skipped-two',
            'https://github.com/manoma080/mirror-fixture-org-b-skipped-repo',
        }
        self.assertTrue(skipped.isdisjoint(actual))
