import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from repo_mirror import git_sync


def git(*args):
    return subprocess.run(["git", *map(str, args)], check=True, capture_output=True).stdout


class WipeIntegrationTests(unittest.TestCase):
    def test_legacy_direct_call_without_safety_fails_before_git(self):
        from unittest.mock import patch

        with patch.object(git_sync.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "safety"):
                git_sync.mirror_git("https://source/repo", "https://github.com/owner/repo.git", "token")
        run.assert_not_called()

    def test_switch_default_refuses_concurrent_default_change(self):
        from unittest.mock import patch
        from repo_mirror import repository

        source = {"type": "github", "base_url": "https://github.com", "owner": "alice"}
        src = Mock()
        src.request.return_value = {"clone_url": "https://github.com/alice/demo.git", "default_branch": "main"}
        dst = Mock()
        dst.request.return_value = {"default_branch": "other"}
        with patch.object(repository, "get_existing", return_value={
            "description": repository.source_marker(source, "demo"), "default_branch": "old"
        }), patch.object(repository, "mirror_git", side_effect=lambda *args, **kw: kw["safety"]["switch_default"]("refs/heads/main")):
            with self.assertRaisesRegex(RuntimeError, "default branch changed"):
                repository.mirror_repository(source, "demo", "dest", src, dst, "token")
        self.assertFalse(any(call.args[0] == "PATCH" for call in dst.request.call_args_list))

    def test_repository_archive_factory_targets_created_archive(self):
        from unittest.mock import patch

        from repo_mirror import repository

        source = {"type": "github", "base_url": "https://github.com", "owner": "alice"}
        src, dst = Mock(), Mock()
        src.request.return_value = {"clone_url": "https://github.com/alice/demo.git", "default_branch": "main"}
        repo = {"description": repository.source_marker(source, "demo"), "default_branch": "old"}

        def exercise(*args, **kwargs):
            archive, switch = kwargs["safety"]["archive_factory"]()
            self.assertEqual(archive, "https://github.com/dest/archive.git")
            switch("refs/heads/old")
            raise RuntimeError("stop after factory")

        with (
            patch.object(repository, "get_existing", return_value=repo),
            patch.object(repository, "create_archive", return_value="https://github.com/dest/archive.git"),
            patch.object(repository, "set_archive_default") as switch,
            patch.object(repository, "mirror_git", side_effect=exercise),
            self.assertRaisesRegex(RuntimeError, "stop after factory"),
        ):
            repository.mirror_repository(source, "demo", "dest", src, dst, "token")
        switch.assert_called_once_with(dst, "https://api.github.com/repos/dest/archive", "refs/heads/old")

    def test_repository_supplies_safety_context(self):
        from unittest.mock import patch

        from repo_mirror import repository

        source = {"type": "github", "base_url": "https://github.com", "owner": "alice"}
        src = Mock()
        from urllib.error import HTTPError

        src.request.side_effect = lambda method, url: (
            (_ for _ in ()).throw(HTTPError(url, 404, "", {}, None))
            if url.endswith("/releases/latest")
            else {"clone_url": "https://github.com/alice/demo.git", "default_branch": "main"}
        )
        src.pages.return_value = []
        dst = Mock()
        dst.request.return_value = {
            "description": repository.source_marker(source, "demo"),
            "default_branch": "main",
        }
        with patch.object(repository, "mirror_git") as mirror:
            repository.mirror_repository(source, "demo", "dest", src, dst, "token")
        self.assertEqual(mirror.call_args.kwargs["safety"]["source_default"], "main")

    def test_mirror_git_uses_safe_snapshot_pipeline_when_context_supplied(self):
        from unittest.mock import patch

        with patch.object(git_sync, "synchronize_snapshots", return_value=True) as sync:
            result = git_sync.mirror_git(
                "https://source/repo",
                "https://github.com/owner/repo.git",
                "token",
                safety={
                    "source_default": "main",
                    "dest_default": "main",
                    "archive_factory": Mock(),
                    "switch_default": Mock(),
                },
            )
        self.assertTrue(result)
        sync.assert_called_once()

    def test_destination_default_race_fails_before_any_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            git("init", "-q", "-b", "main", work)
            git("-C", work, "config", "user.name", "Test")
            git("-C", work, "config", "user.email", "test@example.com")
            git("-C", work, "commit", "--allow-empty", "-qm", "initial")
            git("-C", work, "branch", "other")
            dest, source, store = [root / n for n in ("dest", "source", "store")]
            git("clone", "-q", "--bare", work, dest)
            git("clone", "-q", "--bare", work, source)
            git("init", "-q", "--bare", store)
            git("-C", dest, "symbolic-ref", "HEAD", "refs/heads/other")
            before = git("ls-remote", "--refs", dest)
            with self.assertRaises(RuntimeError):
                git_sync.synchronize_snapshots(
                    lambda args: git(*args),
                    lambda args: git(*args),
                    str(store),
                    str(source),
                    str(dest),
                    "main",
                    "main",
                    Mock(),
                    Mock(),
                )
            self.assertEqual(git("ls-remote", "--refs", dest), before)

    def test_wipe_archives_old_refs_before_updating_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            git("init", "-q", "-b", "main", work)
            git("-C", work, "config", "user.name", "Test")
            git("-C", work, "config", "user.email", "test@example.com")
            for i in range(4):
                (work / f"f{i}").write_text("old")
                git("-C", work, "add", ".")
                git("-C", work, "commit", "-qm", str(i))
            dest, source, archive, store = [root / n for n in ("dest", "source", "archive", "store")]
            git("clone", "-q", "--bare", work, dest)
            git("-C", work, "checkout", "-q", "--orphan", "replacement")
            git("-C", work, "rm", "-q", "-rf", ".")
            (work / "new").write_text("new")
            git("-C", work, "add", ".")
            git("-C", work, "commit", "-qm", "new")
            git("clone", "-q", "--bare", work, source)
            git("-C", source, "update-ref", "-d", "refs/heads/main")
            for path in (archive, store):
                git("init", "-q", "--bare", path)
            git("-C", dest, "branch", "bisect/oot-1")
            old = git("ls-remote", "--refs", dest)
            def set_archive_default(ref):
                git("-C", archive, "symbolic-ref", "HEAD", ref)
            factory = Mock(return_value=(str(archive), set_archive_default))
            failing_factory = Mock(side_effect=RuntimeError("archive unavailable"))
            with self.assertRaisesRegex(RuntimeError, "archive unavailable"):
                git_sync.synchronize_snapshots(
                    lambda args: git(*args),
                    lambda args: git(*args),
                    str(store),
                    str(source),
                    str(dest),
                    "replacement",
                    "main",
                    failing_factory,
                    Mock(),
                )
            self.assertEqual(git("ls-remote", "--refs", dest), old)
            bad_archive = root / "bad-archive"
            git("init", "-q", "--bare", bad_archive)
            git("-C", bad_archive, "symbolic-ref", "HEAD", "refs/heads/bisect/oot-1")
            with self.assertRaisesRegex(RuntimeError, "archive default branch could not be verified"):
                git_sync.synchronize_snapshots(
                    lambda args: git(*args), lambda args: git(*args), str(store), str(source), str(dest),
                    "replacement", "main", lambda: (str(bad_archive), lambda ref: None), Mock(),
                )
            self.assertEqual(git("ls-remote", "--refs", dest), old)
            git_sync.synchronize_snapshots(
                lambda args: git(*args),
                lambda args: git(*args),
                str(store),
                str(source),
                str(dest),
                "replacement",
                "main",
                factory,
                lambda ref: git("-C", dest, "symbolic-ref", "HEAD", ref),
            )
            factory.assert_called_once()
            self.assertEqual(git("ls-remote", "--refs", archive), old)
            self.assertEqual(git("-C", archive, "symbolic-ref", "HEAD").strip(), b"refs/heads/main")
            self.assertEqual(git("ls-remote", "--refs", source), git("ls-remote", "--refs", dest))
