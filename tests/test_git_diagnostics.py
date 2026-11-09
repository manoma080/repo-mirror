import subprocess
import unittest
from unittest.mock import patch

from repo_mirror import snapshot_push
from repo_mirror import git_sync
from repo_mirror.logging_utils import error_detail


class GitDiagnosticsTests(unittest.TestCase):
    def test_categories_and_unknown_never_echo_untrusted_data(self):
        cases = [
            (b'remote: fatal: pack exceeds maximum allowed size', '2 GB'),
            (b'remote: permission denied', 'permission'),
            (b'fatal: could not resolve host', 'network'),
            (b'unknown error', 'unclassified'),
            (None, 'unclassified'),
        ]
        for stderr, expected in cases:
            with self.subTest(stderr=stderr):
                error = subprocess.CalledProcessError(1, ['secret'], stderr=stderr)
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    with snapshot_push.git_phase('snapshot push'):
                        raise error
                self.assertIn(expected, error_detail(caught.exception))
                self.assertNotIn('secret', error_detail(caught.exception))

    def test_fetch_and_verify_phases(self):
        def fail(args):
            raise subprocess.CalledProcessError(1, args, stderr=b'connection reset /secret')
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            snapshot_push.capture_snapshot(fail, '/secret', 'secret')
        self.assertIn('snapshot fetch', error_detail(caught.exception))
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            snapshot_push._verify(fail, 'secret', {})
        self.assertIn('remote verification', error_detail(caught.exception))
        self.assertNotIn('secret', error_detail(caught.exception))

    def test_push_failure_reports_safe_category(self):
        error = subprocess.CalledProcessError(1, ['git', 'secret'], stderr=b'remote: error: GH001: Large files detected. /private/secret')
        with patch.object(snapshot_push, '_push', side_effect=error):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                snapshot_push.push_snapshot(None, 'secret', 'secret', {'refs/heads/main': 'a' * 40}, {})
        detail = error_detail(caught.exception)
        self.assertIn('snapshot push', detail)
        self.assertIn('100 MB', detail)
        self.assertNotIn('secret', detail)
        self.assertNotIn('/private', detail)

    def test_final_default_switch_has_distinct_phase(self):
        desired = {'refs/heads/main': 'a' * 40}
        with patch.object(git_sync, 'capture_snapshot', side_effect=[desired, {}]), patch.object(git_sync, 'push_snapshot'):
            with self.assertRaises(RuntimeError) as caught:
                git_sync.synchronize_snapshots(None, None, 'store', 'src', 'dst', 'main', None, None,
                                               lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('secret')))
        self.assertIn('default switch', error_detail(caught.exception))
        self.assertNotIn('secret', error_detail(caught.exception))
