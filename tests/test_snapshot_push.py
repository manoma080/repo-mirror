import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repo_mirror import snapshot_push
from repo_mirror.snapshot_push import (
    SnapshotError,
    archive_snapshot,
    capture_snapshot,
    is_seed_preimage,
    push_snapshot,
)


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


class SnapshotSeedTests(unittest.TestCase):
    """First push of a large history, sent in slices under the 2 GiB limit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.work = root / "work"
        git("init", "-q", "-b", "main", self.work)
        git("-C", self.work, "config", "user.name", "Test")
        git("-C", self.work, "config", "user.email", "test@example.com")
        self.commits = []
        for index in range(6):
            (self.work / "file").write_text(str(index))
            git("-C", self.work, "add", "file")
            git("-C", self.work, "commit", "-qm", f"commit {index}")
            self.commits.append(git("-C", self.work, "rev-parse", "HEAD").decode().strip())
        git("-C", self.work, "tag", "-a", "v1", self.commits[2], "-m", "tag")
        git("clone", "-q", "--bare", self.work, root / "source")
        git("init", "-q", "--bare", root / "store")
        git("init", "-q", "--bare", root / "dest")
        self.store = str(root / "store")
        self.source = str(root / "source")
        self.dest = str(root / "dest")
        self.snapshot = capture_snapshot(run, self.store, self.source)

    def _record(self, calls):
        def run_and_record(args):
            calls.append(list(args))
            return run(args)
        return run_and_record

    @staticmethod
    def _specs(calls, ref):
        return [arg for call in calls for arg in call if isinstance(arg, str) and arg.endswith(":" + ref)]

    def test_empty_destination_over_limit_is_seeded_in_slices(self):
        calls = []
        with patch.object(snapshot_push, "SEED_LIMIT_BYTES", 0), patch.object(snapshot_push, "SEED_STEP", 2):
            push_snapshot(self._record(calls), self.store, self.dest, self.snapshot, {})
        self.assertEqual(git("ls-remote", "--refs", self.dest), git("ls-remote", "--refs", self.source))
        self.assertGreaterEqual(len(self._specs(calls, "refs/heads/main")), 3)

    def test_small_empty_destination_uses_one_atomic_push(self):
        calls = []
        with patch.object(snapshot_push, "SEED_LIMIT_BYTES", 10 ** 12):
            push_snapshot(self._record(calls), self.store, self.dest, self.snapshot, {})
        self.assertEqual(len([call for call in calls if "push" in call]), 1)
        self.assertEqual(git("ls-remote", "--refs", self.dest), git("ls-remote", "--refs", self.source))

    def test_partial_seed_resumes_from_the_destination_ancestor(self):
        git("-C", self.work, "push", "-q", self.dest, f"{self.commits[1]}:refs/heads/main")
        observed = capture_snapshot(run, self.store, self.dest)
        self.assertTrue(is_seed_preimage(run, self.store, observed, self.snapshot))
        calls = []
        with patch.object(snapshot_push, "SEED_STEP", 2):
            push_snapshot(self._record(calls), self.store, self.dest, self.snapshot, observed, resume=True)
        self.assertEqual(git("ls-remote", "--refs", self.dest), git("ls-remote", "--refs", self.source))
        specs = self._specs(calls, "refs/heads/main")
        self.assertIn(f"{self.commits[3]}:refs/heads/main", specs)
        self.assertNotIn(f"{self.commits[0]}:refs/heads/main", specs)

    def test_seed_resume_rejects_a_destination_that_diverged(self):
        other = Path(self.tmp.name) / "other"
        git("init", "-q", "-b", "main", other)
        git("-C", other, "config", "user.name", "Test")
        git("-C", other, "config", "user.email", "test@example.com")
        (other / "file").write_text("unrelated")
        git("-C", other, "add", "file")
        git("-C", other, "commit", "-qm", "unrelated")
        git("-C", other, "push", "-q", self.dest, "HEAD:refs/heads/main")
        observed = capture_snapshot(run, self.store, self.dest)
        self.assertFalse(is_seed_preimage(run, self.store, observed, self.snapshot))

    def test_seed_halves_the_slice_after_a_size_rejection(self):
        calls = []
        rejected = {"count": 1}

        def flaky(args):
            calls.append(list(args))
            if (
                rejected["count"]
                and "push" in args
                and any(isinstance(arg, str) and arg.endswith(":refs/heads/main") for arg in args)
            ):
                rejected["count"] -= 1
                raise subprocess.CalledProcessError(1, args, stderr=b"fatal: the remote end hung up unexpectedly")
            return run(args)

        with patch.object(snapshot_push, "SEED_STEP", 4):
            push_snapshot(flaky, self.store, self.dest, self.snapshot, {}, resume=True)
        self.assertEqual(git("ls-remote", "--refs", self.dest), git("ls-remote", "--refs", self.source))
        self.assertIn(f"{self.commits[1]}:refs/heads/main", self._specs(calls, "refs/heads/main"))


class SeedMarkerTests(unittest.TestCase):
    """A marker distinguishes a crash after seeding from a foreign repository."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.work = root / "work"
        git("init", "-q", "-b", "main", self.work)
        git("-C", self.work, "config", "user.name", "Test")
        git("-C", self.work, "config", "user.email", "test@example.com")
        (self.work / "file").write_text("content")
        git("-C", self.work, "add", "file")
        git("-C", self.work, "commit", "-qm", "first")
        git("-C", self.work, "branch", "other")
        git("clone", "-q", "--bare", self.work, root / "source")
        git("init", "-q", "--bare", root / "store")
        git("init", "-q", "--bare", root / "dest")
        self.store = str(root / "store")
        self.source = str(root / "source")
        self.dest = str(root / "dest")

    def test_equal_refs_are_a_preimage(self):
        snapshot = capture_snapshot(run, self.store, self.source)
        self.assertTrue(is_seed_preimage(run, self.store, dict(snapshot), snapshot))

    def test_marker_resumes_a_seed_that_only_lost_its_head(self):
        from repo_mirror import git_sync

        snapshot = capture_snapshot(run, self.store, self.source)
        specs = [f"{oid}:{ref}" for ref, oid in snapshot.items()]
        git("-C", self.work, "push", "-q", self.dest, *specs)
        git("-C", self.dest, "symbolic-ref", "HEAD", "refs/heads/other")
        git("-C", self.work, "push", "-q", self.dest, f"{snapshot['refs/heads/main']}:refs/repo-mirror/seed-in-progress")

        def switch(ref, initial_refs=None):
            git("-C", self.dest, "symbolic-ref", "HEAD", ref)

        git_sync.synchronize_snapshots(
            run, run, self.store, self.source, self.dest, "main", "main", lambda: (None, None), switch
        )
        self.assertEqual(
            git("-C", self.dest, "symbolic-ref", "HEAD").decode().strip(), "refs/heads/main"
        )
        self.assertEqual(git("ls-remote", "--refs", self.dest, "refs/repo-mirror/seed-in-progress"), b"")
        self.assertEqual(git("ls-remote", "--refs", self.dest), git("ls-remote", "--refs", self.source))

    def test_marker_and_partial_refs_finish_an_interrupted_seed(self):
        from repo_mirror import git_sync
        from unittest.mock import patch

        snapshot = capture_snapshot(run, self.store, self.source)
        first = git("-C", self.work, "rev-list", "--max-parents=0", "main").decode().strip()
        git("-C", self.work, "push", "-q", self.dest, f"{first}:refs/heads/main")
        git("-C", self.work, "push", "-q", self.dest, f"{first}:refs/repo-mirror/seed-in-progress")
        git("-C", self.dest, "symbolic-ref", "HEAD", "refs/heads/main")

        def switch(ref, initial_refs=None):
            git("-C", self.dest, "symbolic-ref", "HEAD", ref)

        with patch.object(snapshot_push, "SEED_STEP", 1), patch.object(snapshot_push, "SEED_LIMIT_BYTES", 0):
            git_sync.synchronize_snapshots(
                run, run, self.store, self.source, self.dest, "main", "main", lambda: (None, None), switch
            )
        self.assertEqual(git("ls-remote", "--refs", self.dest), git("ls-remote", "--refs", self.source))
        self.assertEqual(git("ls-remote", "--refs", self.dest, "refs/repo-mirror/seed-in-progress"), b"")


class SeedOrderTests(unittest.TestCase):
    """Slices must follow ancestry, not commit dates."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.work = root / "work"
        git("init", "-q", "-b", "main", self.work)
        git("-C", self.work, "config", "user.name", "Test")
        git("-C", self.work, "config", "user.email", "test@example.com")

        def commit(name, date):
            (self.work / name).write_text(name)
            git("-C", self.work, "add", name)
            subprocess.run(
                ["git", "-C", str(self.work), "commit", "-qm", name],
                check=True,
                capture_output=True,
                env={**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
            )

        # A branch whose child commits are dated before their merge base: default
        # reverse date order emits "mainline" (a child) before its parent "base".
        commit("base", "2020-01-01T00:00:00+0000")
        git("-C", self.work, "branch", "side")
        commit("mainline", "2018-01-01T00:00:00+0000")
        git("-C", self.work, "checkout", "-q", "side")
        commit("side1", "2019-01-01T00:00:00+0000")
        commit("side2", "2019-06-01T00:00:00+0000")
        git("-C", self.work, "checkout", "-q", "main")
        subprocess.run(
            ["git", "-C", str(self.work), "merge", "-q", "--no-ff", "side", "-m", "merge"],
            check=True,
            capture_output=True,
            env={
                **os.environ,
                "GIT_AUTHOR_DATE": "2022-01-01T00:00:00+0000",
                "GIT_COMMITTER_DATE": "2022-01-01T00:00:00+0000",
            },
        )
        git("clone", "-q", "--bare", self.work, root / "source")
        git("init", "-q", "--bare", root / "store")
        git("init", "-q", "--bare", root / "dest")
        self.store = str(root / "store")
        self.source = str(root / "source")
        self.dest = str(root / "dest")

    def test_seeded_slices_follow_ancestry_and_not_dates(self):
        date_order = git("-C", self.work, "rev-list", "--reverse", "main").decode().split()
        topo_order = git("-C", self.work, "rev-list", "--reverse", "--topo-order", "main").decode().split()
        self.assertNotEqual(date_order, topo_order)
        snapshot = capture_snapshot(run, self.store, self.source)
        with patch.object(snapshot_push, "SEED_LIMIT_BYTES", 0), patch.object(snapshot_push, "SEED_STEP", 1):
            push_snapshot(run, self.store, self.dest, snapshot, {})
        self.assertEqual(git("ls-remote", "--refs", self.dest), git("ls-remote", "--refs", self.source))
