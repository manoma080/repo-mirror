import subprocess
import tempfile
import unittest
from pathlib import Path


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout


class WipeGitDetectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        self.run_git("init", "-q")
        self.run_git("config", "user.name", "Test")
        self.run_git("config", "user.email", "test@example.com")

    def run_git(self, *args):
        return git(self.repo, *args)

    def detect(self, old, new, refs):
        from repo_mirror.wipe_detection import detect_wipe

        return detect_wipe(lambda args: self.run_git(*args), old, new, refs)

    def commit(self, message):
        self.run_git("add", "-A")
        self.run_git("commit", "-qm", message, "--allow-empty")
        return self.run_git("rev-parse", "HEAD").strip().decode()

    def test_replaced_paths_with_same_file_count_are_not_a_wipe(self):
        for i in range(4):
            (self.repo / f"old{i}").write_text(str(i))
        old = self.commit("old")
        self.run_git("checkout", "--orphan", "replacement")
        self.run_git("rm", "-q", "-rf", ".")
        for i in range(4):
            (self.repo / f"new{i}").write_text(str(i))
        new = self.commit("replace all paths")
        result = self.detect(old, new, ["refs/heads/replacement"])
        self.assertEqual(result.unreachable_commit_count, 1)
        self.assertEqual(result.lost_file_count, 0)
        self.assertFalse(result.is_wipe)

    def test_boundary_paths_and_commits_and_tag_reachability(self):
        for name in ("keep", "line\nbreak", "tab\tname", "unicode-λ"):
            (self.repo / name).write_text("data")
        first = self.commit("first")
        self.commit("second")
        self.commit("third")
        old = self.commit("fourth")
        self.run_git("tag", "-a", "retained", "-m", "tag", first)
        self.run_git("checkout", "--orphan", "replacement")
        self.run_git("rm", "-q", "-rf", ".")
        (self.repo / "keep").write_text("changed content")
        new = self.commit("replacement")
        result = self.detect(old, new, ["refs/heads/replacement", "refs/tags/retained"])
        self.assertTrue(result.is_wipe)
        self.assertEqual((result.old_file_count, result.lost_file_count), (4, 3))
        self.assertEqual((result.old_commit_count, result.unreachable_commit_count), (4, 3))
        self.run_git("branch", "rescue", old)
        self.assertFalse(self.detect(old, new, ["refs/heads/replacement", "refs/heads/rescue"]).is_wipe)

    def test_file_and_commit_conditions_both_required(self):
        for name in ("a", "b", "c", "d"):
            (self.repo / name).write_text(name)
        old = self.commit("old")
        self.run_git("checkout", "--orphan", "replacement")
        new = self.commit("unrelated same files")
        result = self.detect(old, new, ["refs/heads/replacement"])
        self.assertEqual(result.unreachable_commit_count, 1)
        self.assertEqual(result.lost_file_count, 0)
        self.assertFalse(result.is_wipe)
        before_deletion = new
        for name in ("a", "b", "c"):
            (self.repo / name).unlink()
        new = self.commit("delete files")
        result = self.detect(before_deletion, new, ["refs/heads/replacement"])
        self.assertEqual(result.lost_file_count, 3)
        self.assertEqual(result.unreachable_commit_count, 0)
        self.assertFalse(result.is_wipe)

    def test_empty_old_tree_and_noncommit_tags(self):
        old = self.commit("empty")
        tree = self.run_git("rev-parse", old + "^{tree}").strip().decode()
        self.run_git("tag", "-a", "tree-tag", "-m", "tree", tree)
        self.run_git("checkout", "--orphan", "replacement")
        (self.repo / "new").write_text("new")
        new = self.commit("new")
        result = self.detect(old, new, ["refs/heads/replacement", "refs/tags/tree-tag"])
        self.assertEqual(result.old_file_count, 0)
        self.assertEqual(result.unreachable_commit_count, 1)
        self.assertFalse(result.is_wipe)

    def test_reachability_uses_constant_number_of_traversals(self):
        from repo_mirror.wipe_detection import detect_wipe

        first = self.commit("first")
        old = self.commit("second")
        self.run_git("checkout", "--orphan", "replacement")
        new = self.commit("new")
        refs = []
        for i in range(30):
            name = f"refs/tags/retained-{i}"
            self.run_git("update-ref", name, first)
            refs.append(name)
        calls = []

        def run(args):
            calls.append(args)
            return self.run_git(*args)

        result = detect_wipe(run, old, new, refs)
        self.assertEqual((result.old_commit_count, result.unreachable_commit_count), (2, 1))
        traversals = [args for args in calls if any("rev-list" in arg for arg in args)]
        self.assertLessEqual(len(traversals), 2, traversals)

    def test_only_supplied_refs_protect_commits(self):
        first = self.commit("first")
        old = self.commit("second")
        self.run_git("tag", "-a", "inner", "-m", "inner", first)
        self.run_git("tag", "-a", "outer", "-m", "outer", "inner")
        self.run_git("branch", "stale", old)
        self.run_git("checkout", "--orphan", "replacement")
        new = self.commit("replacement")
        self.assertEqual(self.detect(old, new, []).unreachable_commit_count, 2)
        self.assertEqual(self.detect(old, new, ["refs/tags/outer"]).unreachable_commit_count, 1)
        self.assertEqual(self.detect(old, new, ["refs/heads/stale"]).unreachable_commit_count, 0)

    def test_missing_ref_fails_closed(self):
        old = self.commit("old")
        with self.assertRaises(subprocess.CalledProcessError):
            self.detect(old, old, ["refs/heads/missing"])

    def test_detects_replaced_history_and_paths(self):
        from repo_mirror.wipe_detection import detect_wipe

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-q")
            git(repo, "config", "user.name", "Test")
            git(repo, "config", "user.email", "test@example.com")
            for i in range(4):
                (repo / f"file{i}").write_text(str(i))
                git(repo, "add", ".")
                git(repo, "commit", "-qm", f"old{i}")
            old = git(repo, "rev-parse", "HEAD").strip().decode()
            git(repo, "checkout", "--orphan", "replacement")
            git(repo, "rm", "-q", "-rf", ".")
            (repo / "new").write_text("new")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "new")
            new = git(repo, "rev-parse", "HEAD").strip().decode()
            result = detect_wipe(lambda args: git(repo, *args), old, new, ["refs/heads/replacement"])
            self.assertTrue(result.is_wipe)
            self.assertEqual((result.old_file_count, result.lost_file_count), (4, 3))
            self.assertEqual((result.old_commit_count, result.unreachable_commit_count), (4, 4))
