import json
import multiprocessing
import os
import queue
import tempfile
import unittest
from unittest.mock import patch

from repo_mirror import forgejo_sync
from repo_mirror.forgejo_sync import journal_lock


def _hold_lock(path, ready, release):
    with journal_lock(path):
        ready.put('entered')
        release.wait(10)


def _hold_destination(home, identity, ready, release):
    with patch('pwd.getpwuid') as getpwuid:
        getpwuid.return_value.pw_dir = home
        with forgejo_sync.destination_locks(3300, 'Local', [identity]):
            ready.put('entered')
            release.wait(10)


def _run_main(argv, ready):
    with patch.dict(os.environ, {'GITHUB_TOKEN': 'github'}), \
         patch.object(forgejo_sync, '_sync_requested', side_effect=lambda *args: ready.put('entered') or 0):
        ready.put(forgejo_sync.main(argv))


def _run_blocking_main(argv, ready, release):
    def sync(*args):
        ready.put('entered')
        release.wait(10)
        return 0
    with patch.dict(os.environ, {'GITHUB_TOKEN': 'github'}), \
         patch.object(forgejo_sync, '_sync_requested', side_effect=sync):
        ready.put(forgejo_sync.main(argv))


class JournalLockTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        getpwuid = patch('pwd.getpwuid')
        self.addCleanup(getpwuid.stop)
        getpwuid.start().return_value.pw_dir = home.name

    def test_case_alias_waits_and_crash_releases_destination_lock(self):
        with tempfile.TemporaryDirectory() as home:
            ctx = multiprocessing.get_context('fork')
            first_ready, second_ready = ctx.Queue(), ctx.Queue()
            first_release, release = ctx.Event(), ctx.Event()
            first = ctx.Process(target=_hold_destination, args=(home, 'mirror-owner/mirror-test', first_ready, first_release))
            second = ctx.Process(target=_hold_destination, args=(home, 'MANOMA080/MIRROR-TEST', second_ready, release))
            first.start()
            try:
                self.assertEqual(first_ready.get(timeout=5), 'entered')
                second.start()
                with self.assertRaises(queue.Empty):
                    second_ready.get(timeout=0.3)
                first.terminate()
                first.join(5)
                self.assertEqual(second_ready.get(timeout=5), 'entered')
            finally:
                release.set()
                first.join(5)
                if second.pid:
                    second.join(5)
                    if second.is_alive():
                        second.terminate()
                        second.join()

    def test_replacing_lock_directory_does_not_split_destination_lock(self):
        with tempfile.TemporaryDirectory() as home:
            ctx = multiprocessing.get_context('fork')
            first_ready, second_ready = ctx.Queue(), ctx.Queue()
            release = ctx.Event()
            first = ctx.Process(target=_hold_destination, args=(home, 'mirror-owner/mirror-test', first_ready, release))
            second = ctx.Process(target=_hold_destination, args=(home, 'mirror-owner/mirror-test', second_ready, release))
            first.start()
            try:
                self.assertEqual(first_ready.get(timeout=5), 'entered')
                lockdir = os.path.join(home, '.repo-mirror-forgejo-locks')
                if os.path.isdir(lockdir):
                    os.rename(lockdir, lockdir + '.old')
                    os.mkdir(lockdir, 0o700)
                second.start()
                with self.assertRaises(queue.Empty):
                    second_ready.get(timeout=0.4)
                release.set()
                self.assertEqual(second_ready.get(timeout=5), 'entered')
            finally:
                release.set()
                first.join(5)
                if second.pid:
                    second.join(5)
                    if second.is_alive():
                        second.terminate()
                        second.join()

    def test_destination_lock_ignores_untrusted_filesystem_namespace(self):
        with tempfile.TemporaryDirectory() as home, patch('pwd.getpwuid') as getpwuid:
            getpwuid.return_value.pw_dir = home
            lockdir = os.path.join(home, '.repo-mirror-forgejo-locks')
            os.symlink(home, lockdir)
            with forgejo_sync.destination_locks(3300, 'local', ['mirror-owner/mirror-test']):
                self.assertTrue(os.path.islink(lockdir))

    def test_forked_child_does_not_extend_parent_lock(self):
        ctx = multiprocessing.get_context('fork')
        release = ctx.Event()
        with tempfile.TemporaryDirectory() as home:
            with forgejo_sync.destination_locks(3300, 'local', ['mirror-owner/mirror-test']):
                child = ctx.Process(target=release.wait, args=(10,))
                child.start()
            ready = ctx.Queue()
            contender = ctx.Process(target=_hold_destination, args=(home, 'mirror-owner/mirror-test', ready, release))
            contender.start()
            try:
                self.assertEqual(ready.get(timeout=3), 'entered')
            finally:
                release.set()
                child.join(5)
                contender.join(5)
                for proc in (child, contender):
                    if proc.is_alive():
                        proc.terminate()
                        proc.join()

    def test_same_destination_different_journals_serialize(self):
        with tempfile.TemporaryDirectory() as directory:
            url = 'https://github.com/mirror-owner/mirror-test'
            for name, contents in [('config', json.dumps([url])), ('token', 'token'), ('secret', 'synthetic-recovery-fixture-value-32-bytes')]:
                path = os.path.join(directory, name)
                with open(path, 'w') as stream:
                    stream.write(contents)
                os.chmod(path, 0o600)
            journals = [os.path.join(directory, str(i)) for i in range(2)]
            for path in journals:
                os.mkdir(path, 0o700)
            base = ['--config', os.path.join(directory, 'config'), '--source', url,
                    '--token-file', os.path.join(directory, 'token'), '--timestamp-secret-file',
                    os.path.join(directory, 'secret'), '--owner', 'local', '--use-github-token']
            ctx = multiprocessing.get_context('fork')
            first_ready, second_ready, release = ctx.Queue(), ctx.Queue(), ctx.Event()
            first = ctx.Process(target=_run_blocking_main, args=(base + ['--journal-dir', journals[0]], first_ready, release))
            second = ctx.Process(target=_run_main, args=(base + ['--journal-dir', journals[1]], second_ready))
            with patch('pwd.getpwuid') as getpwuid:
                getpwuid.return_value.pw_dir = directory
                first.start()
                try:
                    self.assertEqual(first_ready.get(timeout=5), 'entered')
                    second.start()
                    with self.assertRaises(queue.Empty):
                        second_ready.get(timeout=0.3)
                    release.set()
                    # Independent producers can publish in either order after unlock.
                    self.assertEqual(first_ready.get(timeout=5), 0)
                    self.assertEqual(second_ready.get(timeout=5), 'entered')
                    self.assertEqual(second_ready.get(timeout=5), 0)
                finally:
                    release.set()
                    first.join(5)
                    if second.pid:
                        second.join(5)
                        if second.is_alive():
                            second.terminate()
                            second.join()
    def test_distinct_destinations_share_journal_directory_concurrently(self):
        with tempfile.TemporaryDirectory() as directory:
            urls = ['https://github.com/mirror-owner/mirror-one', 'https://github.com/mirror-owner/mirror-two']
            for name, contents in [('config', json.dumps(urls)), ('token', 'token'), ('secret', 'synthetic-recovery-fixture-value-32-bytes')]:
                path = os.path.join(directory, name)
                with open(path, 'w') as stream:
                    stream.write(contents)
                os.chmod(path, 0o600)
            base = ['--config', os.path.join(directory, 'config'), '--token-file', os.path.join(directory, 'token'),
                    '--timestamp-secret-file', os.path.join(directory, 'secret'), '--journal-dir', directory,
                    '--owner', 'local', '--use-github-token']
            ctx = multiprocessing.get_context('fork')
            first_ready, second_ready, release = ctx.Queue(), ctx.Queue(), ctx.Event()
            first = ctx.Process(target=_run_blocking_main, args=(base + ['--source', urls[0]], first_ready, release))
            second = ctx.Process(target=_run_main, args=(base + ['--source', urls[1]], second_ready))
            with tempfile.TemporaryDirectory() as lock_home, patch('pwd.getpwuid') as getpwuid:
                getpwuid.return_value.pw_dir = lock_home
                first.start()
                try:
                    self.assertEqual(first_ready.get(timeout=5), 'entered')
                    second.start()
                    self.assertEqual(second_ready.get(timeout=3), 'entered')
                finally:
                    release.set()
                    first.join(5)
                    if second.pid:
                        second.join(5)
                        if second.is_alive():
                            second.terminate()
                            second.join()

    def test_main_waits_for_existing_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            url = 'https://github.com/mirror-owner/mirror-test'
            for name, contents in [('config', json.dumps([url])), ('token', 'token'), ('secret', 'synthetic-recovery-fixture-value-32-bytes')]:
                path = os.path.join(directory, name)
                with open(path, 'w') as stream:
                    stream.write(contents)
                os.chmod(path, 0o600)
            argv = ['--config', os.path.join(directory, 'config'), '--source', url,
                    '--token-file', os.path.join(directory, 'token'), '--timestamp-secret-file',
                    os.path.join(directory, 'secret'), '--journal-dir', directory,
                    '--owner', 'local', '--use-github-token']
            ctx = multiprocessing.get_context('fork')
            ready = ctx.Queue()
            # Fork before acquiring the lock, avoiding inheritance of its fd.
            release = ctx.Event()
            holder = ctx.Process(target=_hold_lock, args=(directory, ready, release))
            holder.start()
            process = ctx.Process(target=_run_main, args=(argv, ready))
            try:
                self.assertEqual(ready.get(timeout=5), 'entered')
                process.start()
                with self.assertRaises(queue.Empty):
                    ready.get(timeout=0.3)
                release.set()
                self.assertEqual(ready.get(timeout=5), 'entered')
                self.assertEqual(ready.get(timeout=5), 0)
                process.join(5)
                self.assertEqual(process.exitcode, 0)
            finally:
                release.set()
                holder.join(5)
                if process.is_alive():
                    process.terminate()
                    process.join()

    def test_rejects_symlink_and_insecure_directory(self):
        with tempfile.TemporaryDirectory() as parent:
            directory = os.path.join(parent, 'journal')
            os.mkdir(directory, 0o700)
            link = os.path.join(parent, 'link')
            os.symlink(directory, link)
            with self.assertRaises(OSError):
                with journal_lock(link):
                    pass
            os.chmod(directory, 0o755)
            with self.assertRaises(ValueError):
                with journal_lock(directory):
                    pass

    def test_lock_released_on_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'stop'):
                with journal_lock(directory):
                    raise RuntimeError('stop')
            with journal_lock(directory):
                pass

    def test_serializes_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = multiprocessing.get_context('fork')
            ready = ctx.Queue()
            release = ctx.Event()
            first = ctx.Process(target=_hold_lock, args=(directory, ready, release))
            second = ctx.Process(target=_hold_lock, args=(directory, ready, release))
            first.start()
            try:
                self.assertEqual(ready.get(timeout=5), 'entered')
                second.start()
                with self.assertRaises(queue.Empty):
                    ready.get(timeout=0.3)
                release.set()
                self.assertEqual(ready.get(timeout=5), 'entered')
                second.join(5)
                self.assertEqual(second.exitcode, 0)
            finally:
                release.set()
                first.join(5)
                if second.is_alive():
                    second.terminate()
                    second.join()
            self.assertEqual(first.exitcode, 0)
