import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from repo_mirror.forgejo_destination_adapter import ForgejoDestinationAdapter
from repo_mirror.forgejo_releases import SyncConflict
from repo_mirror.forgejo_sync import safe_reason
from repo_mirror.repository import mirror_releases

REASON = 'Forgejo empty source body cannot be safely mirrored; inspect any pending release and reconcile manually before retrying'
ROOT = Path(__file__).resolve().parents[1]


def run_mirror(bodies):
    releases = [dict(id=i + 1, tag_name=f'unsafe-secret-tag-{i}', name='secret-name', body=body,
                     draft=False, prerelease=False) for i, body in enumerate(bodies)]
    class Source:
        def pages(self, url): return releases
        def request(self, method, url):
            if url.endswith('/latest'): return releases[0]
            return next(item.copy() for item in releases if str(item['id']) == url.rsplit('/', 1)[-1])
    class Client:
        origin = 'http://127.0.0.1:3000'
        base = '/api/v1/repos/team/project'
    adapter = ForgejoDestinationAdapter('team/project', 'token', client=Client())
    # Exercise legacy diagnostic classification independently of current policy.
    def validate(release):
        if release['body'] == 'unknown': raise ValueError('secret-token')
        raise SyncConflict(REASON)
    adapter.validate_release_source = validate
    with patch.dict(os.environ, {'MIRROR_TIMESTAMP_SECRET': 'secret-token'}):
        mirror_releases({'type': 'github', 'owner': 'alice'}, 'demo', 'team', Source(), adapter,
                        'https://source/repo', adapter.target, 'marker', initializing=True)


class EmptyBodyDiagnosticTests(unittest.TestCase):
    def classify(self, bodies):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(Exception) as caught:
            run_mirror(bodies)
        return safe_reason(caught.exception)

    def test_single_and_homogeneous_aggregate(self):
        self.assertEqual(self.classify(['']), REASON)
        self.assertEqual(self.classify(['', None]), REASON)

    def test_mixed_aggregate_is_not_misclassified(self):
        self.assertEqual(self.classify(['', 'unknown']), 'unexpected sync failure')
        self.assertEqual(self.classify(['unknown', '']), 'unexpected sync failure')

    def test_untrusted_messages_and_spoofed_aggregate_rejected(self):
        self.assertEqual(safe_reason(SyncConflict(REASON + ' secret-token')), 'unexpected sync failure')
        fake = RuntimeError('failure')
        fake.release_errors = [SyncConflict(REASON)]
        self.assertEqual(safe_reason(fake), 'unexpected sync failure')

    def test_scheduler_subprocess_propagates_real_mirror_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            (tmp / 'secret').write_text('recovery-key-' * 4)
            (tmp / 'secret').chmod(0o600)
            (tmp / 'journals').mkdir(mode=0o700)
            (tmp / 'manifest').write_text(json.dumps(['https://github.com/mirror-owner/mirror-a']))
            helper = tmp / 'helper.py'
            helper.write_text('import sys\nfrom tests.test_forgejo_empty_body_diagnostic import run_mirror\nfrom repo_mirror.forgejo_sync import safe_reason\ntry:\n run_mirror(["", None])\nexcept Exception as e:\n print("FORGEJO_FAILURE: " + safe_reason(e), file=sys.stderr)\n sys.exit(1)\n')
            env = dict(os.environ, PYTHONPATH=str(ROOT))
            result = subprocess.run([sys.executable, str(ROOT / 'repo_mirror/forgejo_schedule.py'), 'run',
                '--manifest', str(tmp / 'manifest'), '--token-file', 'unused', '--timestamp-secret-file', str(tmp / 'secret'),
                '--journal-dir', str(tmp / 'journals'), '--owner', 'alice', '--lock', str(tmp / 'lock'), '--helper', str(helper)],
                capture_output=True, text=True, env=env, cwd=ROOT)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 1)
            self.assertIn(REASON, output)
            self.assertNotIn('unsafe-secret-tag', output)
            self.assertNotIn('secret-token', output)
            self.assertNotIn('secret-name', output)
