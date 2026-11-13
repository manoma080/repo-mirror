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
            with patch('repo_mirror.forgejo_schedule.subprocess.Popen', side_effect=lambda *a, **kw: __import__('unittest.mock', fromlist=['Mock']).Mock(poll=lambda: 0)) as run:
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
            self.assertIn('@CHECKOUT@/forgejo-nintendo-sources.json', template)
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
            with patch('repo_mirror.forgejo_schedule.subprocess.Popen', side_effect=lambda *a, **kw: __import__('unittest.mock', fromlist=['Mock']).Mock(poll=lambda: 0)) as run:
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
            'https://github.com/manoma080/mirror-heydemoura-EglantinePS5',
            'https://github.com/manoma080/mirror-Swordpdf-ProsperoEden',
            'https://github.com/manoma080/mirror-JMUtechnologies-ps5-linux-playtendoh-workbench',
            'https://github.com/manoma080/mirror-premohq-PlayTendoh',
            'https://github.com/manoma080/mirror-mihawk-99-PS5_Lichii',
            'https://github.com/manoma080/mirror-mihawk-99-PS5_Hookshot',
            'https://github.com/manoma080/mirror-SamoZ256-hydra',
            'https://github.com/manoma080/mirror-seedds-nxnative',
            'https://github.com/manoma080/mirror-Gezine-CivSwitch',
            'https://github.com/manoma080/mirror-scarletnx-ScarletNX',
            'https://github.com/manoma080/mirror-Cor3Duo-kirikae',
        ]
        actual = json.loads((ROOT / 'forgejo-nintendo-sources.json').read_text())
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 11)
        self.assertEqual(len({url.lower() for url in actual}), 11)
        skipped = {
            'https://github.com/manoma080/mirror-phantomptr-ps5upload',
            'https://github.com/manoma080/mirror-Oliv3rmoon-Nightshade',
            'https://github.com/manoma080/mirror-Oliv3rmoon-Belladonna',
            'https://github.com/manoma080/mirror-aleksejsgx-RetroSync',
            'https://github.com/manoma080/mirror-elripalda-Porpoise-Dolphin-Emulator-for-PS5',
            'https://github.com/manoma080/mirror-mihawk-99-PS5_RetroArch',
            'https://github.com/manoma080/mirror-mihawk-99-PS5_Vulkan',
            'https://github.com/manoma080/mirror-mihawk-99-PS5_Mesa',
            'https://github.com/manoma080/mirror-JMUtechnologies-ecam-workbench',
        }
        self.assertTrue(skipped.isdisjoint(actual))
