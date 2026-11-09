import subprocess
import tempfile
import unittest
from pathlib import Path

from repo_mirror.snapshot_push import SnapshotError, archive_snapshot, capture_snapshot, push_snapshot


def git(*args):
    return subprocess.run(["git", *map(str, args)], check=True, capture_output=True).stdout


def run(args):
    return git(*args)


class SnapshotPushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        git("init", "-q", "-b", "old", self.work)
        git("-C", self.work, "config", "user.name", "Test")
        git("-C", self.work, "config", "user.email", "test@example.com")
        (self.work / "file").write_text("content")
        git("-C", self.work, "add", "file")
        git("-C", self.work, "commit", "-qm", "first")
        git("-C", self.work, "tag", "-a", "v1", "-m", "tag")
        git("clone", "-q", "--bare", self.work, self.root / "source")
        git("init", "-q", "--bare", self.root / "store")
        git("init", "-q", "--bare", self.root / "archive")
        self.store = str(self.root / "store")
        self.source = str(self.root / "source")
        self.archive = str(self.root / "archive")

    def test_archive_preserves_annotated_tag_object(self):
        snapshot = capture_snapshot(run, self.store, self.source)
        archive_snapshot(run, self.store, self.archive, snapshot)
        self.assertEqual(git("ls-remote", "--refs", self.source), git("ls-remote", "--refs", self.archive))

    def test_archive_rejects_nonempty(self):
        snapshot = capture_snapshot(run, self.store, self.source)
        archive_snapshot(run, self.store, self.archive, snapshot)
        with self.assertRaises(SnapshotError):
            archive_snapshot(run, self.store, self.archive, snapshot)

    def test_archive_rejects_non_branch_or_tag_refs(self):
        snapshot = capture_snapshot(run, self.store, self.source)
        git("-C", self.work, "push", self.archive, "HEAD:refs/custom/existing")
        with self.assertRaises(SnapshotError):
            archive_snapshot(run, self.store, self.archive, snapshot)

    def test_default_switch_and_concurrent_new_ref(self):
        observed = capture_snapshot(run, self.store, self.source)
        desired = {"refs/heads/new": observed["refs/heads/old"], "refs/tags/v1": observed["refs/tags/v1"]}
        # This ref was created after observation and must not be deleted.
        git("-C", self.source, "update-ref", "refs/heads/concurrent", observed["refs/heads/old"])

        def switch(ref):
            git("-C", self.source, "symbolic-ref", "HEAD", ref)

        push_snapshot(run, self.store, self.source, desired, observed, switch)
        refs = git("ls-remote", "--refs", self.source).decode()
        self.assertIn("refs/heads/concurrent", refs)
        self.assertNotIn("refs/heads/old", refs)
        self.assertIn("refs/heads/new", refs)

    def test_stale_deletion_rejects_all_updates_atomically(self):
        observed = capture_snapshot(run, self.store, self.source)
        git("-C", self.work, "commit", "--allow-empty", "-qm", "second")
        git("-C", self.work, "push", self.source, "HEAD:refs/tags/v1", "--force")
        before = git("ls-remote", "--refs", self.source)
        desired = {"refs/heads/old": observed["refs/heads/old"], "refs/heads/new": observed["refs/heads/old"]}
        with self.assertRaises(subprocess.CalledProcessError):
            push_snapshot(run, self.store, self.source, desired, observed)
        self.assertEqual(git("ls-remote", "--refs", self.source), before)

    def test_modified_observed_ref_is_not_deleted(self):
        observed = capture_snapshot(run, self.store, self.source)
        git("-C", self.work, "commit", "--allow-empty", "-qm", "second")
        git("-C", self.work, "push", self.source, "HEAD:refs/heads/old")
        desired = {"refs/tags/v1": observed["refs/tags/v1"]}
        with self.assertRaises(SnapshotError):
            push_snapshot(run, self.store, self.source, desired, observed)
        self.assertIn(b"refs/heads/old", git("ls-remote", "--refs", self.source))
