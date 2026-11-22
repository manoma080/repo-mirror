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

    def test_truncated_large_push_is_named_as_the_two_gib_limit(self):
        # Captured from a real github.com push of a 2.34 GiB pack: the server
        # answers HTTP 500, then closes the connection while the client waits
        # for the unpack status, so Git only reports a hung-up peer.
        stderr = (
            b"error: RPC failed; HTTP 500 curl 22 The requested URL returned error: 500\n"
            b"send-pack: unexpected disconnect while reading sideband packet\n"
            b"fatal: the remote end hung up unexpectedly\n"
        )
        error = subprocess.CalledProcessError(1, ["git", "push"], stderr=stderr)
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            with snapshot_push.git_phase("snapshot push"):
                raise error
        detail = error_detail(caught.exception)
        self.assertIn("snapshot push", detail)
        self.assertIn("2 GiB", detail)
        self.assertIn("retry after a fresh safety check", detail)

    def test_hung_up_during_fetch_stays_a_network_failure(self):
        error = subprocess.CalledProcessError(
            1, ["git", "fetch"], stderr=b"fatal: the remote end hung up unexpectedly"
        )
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            with snapshot_push.git_phase("snapshot fetch"):
                raise error
        self.assertIn("network failure", error_detail(caught.exception))

    def test_git_http_status_is_named_without_echoing_the_message(self):
        cases = [
            (b"fatal: unable to access 'https://git.example/o/r/': The requested URL returned error: 429", "HTTP 429"),
            (b"error: RPC failed; HTTP 429 curl 22 The requested URL returned error: 429", "HTTP 429"),
            (b"fatal: unable to access 'https://git.example/o/r/': The requested URL returned error: 503", "HTTP 503"),
            (b"fatal: unable to access 'https://git.example/o/r/': The requested URL returned error: 404", "HTTP 404"),
        ]
        for stderr, expected in cases:
            with self.subTest(stderr=stderr):
                error = subprocess.CalledProcessError(1, ["git"], stderr=stderr)
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    with snapshot_push.git_phase("snapshot fetch"):
                        raise error
                detail = error_detail(caught.exception)
                self.assertIn("snapshot fetch", detail)
                self.assertIn(expected, detail)
                self.assertNotIn("https://", detail)
                self.assertNotIn("git.example", detail)

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
                snapshot_push.push_snapshot(lambda args: b'size-pack: 0\n', 'secret', 'secret', {'refs/heads/main': 'a' * 40}, {})
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
