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
    def test_app_mode_passes_refreshable_credentials_to_child_without_helper(self):
        from unittest.mock import patch
        from repo_mirror import forgejo_schedule
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            manifest = tmp / 'sources.json'
            manifest.write_text(json.dumps(['https://github.com/manoma080/mirror-a']))
            with patch('repo_mirror.forgejo_schedule.subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as run:
                result = forgejo_schedule.main(['run', '--manifest', str(manifest), '--token-file', 'x',
                    '--owner', 'alice', '--lock', str(tmp / 'lock'), '--app-id', '123', '--private-key-file', '/key'])
            self.assertEqual(result, 0)
            command = run.call_args.args[0]
            self.assertEqual(command[0], sys.executable)
            self.assertIn('repo_mirror.forgejo_sync', command)
            self.assertIn('--private-key-file', command)
            self.assertNotIn('run', command)

    def test_run_refreshes_per_source_and_suppresses_child_output(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            sources = ['https://github.com/manoma080/mirror-a', 'https://github.com/manoma080/mirror-b']
            manifest = tmp / 'sources.json'
            manifest.write_text(json.dumps(sources))
            calls = tmp / 'calls'
            helper = tmp / 'helper.py'
            helper.write_text('import os,sys\nwith open(os.environ["CALLS"], "a") as f: f.write(sys.argv[-1]+"\\n")\nprint("SECRET_FROM_CHILD")\n')
            result = subprocess.run([sys.executable, str(SCRIPT), 'run', '--manifest', str(manifest), '--token-file', str(tmp / 'token'), '--owner', 'alice', '--lock', str(tmp / 'lock'), '--helper', str(helper), '--helper-python', sys.executable], env={**os.environ, 'CALLS': str(calls)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls.read_text().splitlines(), sources)
            self.assertNotIn('SECRET_FROM_CHILD', result.stdout + result.stderr)

    def test_rejects_invalid_manifest_before_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            manifest = tmp / 'sources.json'
            manifest.write_text(json.dumps(['https://github.com/manoma080/mirror-a', 'https://github.com/manoma080/MIRROR-a']))
            result = subprocess.run([sys.executable, str(SCRIPT), 'run', '--manifest', str(manifest), '--token-file', 'unused', '--owner', 'alice', '--lock', str(tmp / 'lock')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse((tmp / 'lock').exists())

    def test_lock_prevents_overlapping_runs(self):
        import fcntl
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            manifest = tmp / 'sources.json'
            manifest.write_text(json.dumps(['https://github.com/manoma080/mirror-a']))
            lock = tmp / 'lock'
            lock.touch(mode=0o600)
            with lock.open('r+') as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                result = subprocess.run([sys.executable, str(SCRIPT), 'run', '--manifest', str(manifest), '--token-file', 'unused', '--owner', 'alice', '--lock', str(lock)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 75)

    def test_failure_does_not_stop_later_repositories(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            manifest = tmp / 'sources.json'
            manifest.write_text(json.dumps(['https://github.com/manoma080/mirror-a', 'https://github.com/manoma080/mirror-b']))
            helper = tmp / 'helper.py'
            helper.write_text('import sys\nprint("secret", file=sys.stderr)\nsys.exit(1 if sys.argv[-1].endswith("-a") else 0)\n')
            result = subprocess.run([sys.executable, str(SCRIPT), 'run', '--manifest', str(manifest), '--token-file', 'unused', '--owner', 'alice', '--lock', str(tmp / 'lock'), '--helper', str(helper)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('1 succeeded, 1 failed', result.stdout)
            self.assertNotIn('secret', result.stdout + result.stderr)

    def test_failure_reports_only_allowlisted_child_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            manifest = tmp / 'sources.json'
            manifest.write_text(json.dumps(['https://github.com/manoma080/mirror-a']))
            helper = tmp / 'helper.py'
            helper.write_text('import sys\nprint("FORGEJO_FAILURE: release metadata collision", file=sys.stderr)\nprint("FORGEJO_FAILURE: release metadata collision secret", file=sys.stderr)\nprint("secret https://example/token", file=sys.stderr)\nsys.exit(1)\n')
            result = subprocess.run([sys.executable, str(SCRIPT), 'run', '--manifest', str(manifest), '--token-file', 'unused', '--owner', 'alice', '--lock', str(tmp / 'lock'), '--helper', str(helper)], capture_output=True, text=True)
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
            'https://github.com/manoma080/mirror-Oliv3rmoon-Belladonna',
            'https://github.com/manoma080/mirror-Oliv3rmoon-Nightshade',
            'https://github.com/manoma080/mirror-heydemoura-EglantinePS5',
            'https://github.com/manoma080/mirror-Swordpdf-ProsperoEden',
            'https://github.com/manoma080/mirror-JMUtechnologies-ps5-linux-playtendoh-workbench',
            'https://github.com/manoma080/mirror-elripalda-Porpoise-Dolphin-Emulator-for-PS5',
            'https://github.com/manoma080/mirror-premohq-PlayTendoh',
            'https://github.com/manoma080/mirror-mihawk-99-PS5_RetroArch',
            'https://github.com/manoma080/mirror-mihawk-99-PS5_Lichii',
            'https://github.com/manoma080/mirror-mihawk-99-PS5_Hookshot',
            'https://github.com/manoma080/mirror-SamoZ256-hydra',
            'https://github.com/manoma080/mirror-aleksejsgx-RetroSync',
            'https://github.com/manoma080/mirror-seedds-nxnative',
            'https://github.com/manoma080/mirror-Gezine-CivSwitch',
            'https://github.com/manoma080/mirror-scarletnx-ScarletNX',
            'https://github.com/manoma080/mirror-Cor3Duo-kirikae',
            'https://github.com/manoma080/mirror-phantomptr-ps5upload',
            'https://github.com/manoma080/mirror-mihawk-99-PS5_Vulkan',
            'https://github.com/manoma080/mirror-mihawk-99-PS5_Mesa',
            'https://github.com/manoma080/mirror-JMUtechnologies-ecam-workbench',
        ]
        actual = json.loads((ROOT / 'forgejo-nintendo-sources.json').read_text())
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 20)
