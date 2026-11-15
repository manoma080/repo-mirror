import base64
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from repo_mirror import git_sync


class GitSourceAuthTests(unittest.TestCase):
    def test_github_source_snapshot_authenticates_each_remote_operation_with_refreshed_token(self):
        source = "https://github.com/alice/private.git"
        oid = "a" * 40
        remote_calls = []
        tokens = iter(["first", "second", "destination"])

        def run(args, *, env, **kwargs):
            if "ls-remote" in args and source in args:
                remote_calls.append((args, env.copy()))
                return SimpleNamespace(stdout=f"{oid}\trefs/heads/main\n".encode())
            if "fetch" in args and source in args:
                remote_calls.append((args, env.copy()))
                return SimpleNamespace(stdout=b"")
            if "rev-parse" in args:
                return SimpleNamespace(stdout=(oid + "\n").encode())
            if "ls-remote" in args:
                raise RuntimeError("stop after source snapshot")
            return SimpleNamespace(stdout=b"")

        with patch.object(git_sync.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "stop after source snapshot"):
                git_sync.mirror_git(source, "https://github.com/dest/repo.git", lambda: next(tokens), safety={
                    "source_default": "main", "dest_default": "main",
                    "archive_factory": Mock(), "switch_default": Mock(),
                })
        self.assertEqual(len(remote_calls), 2)
        for (args, env), token in zip(remote_calls, ("first", "second")):
            self.assertEqual(env.get("GIT_CONFIG_KEY_0"), "http.https://github.com/.extraheader")
            encoded = base64.b64encode(("x-access-token:" + token).encode()).decode()
            self.assertEqual(env.get("GIT_CONFIG_VALUE_0"), "AUTHORIZATION: basic " + encoded)
            self.assertNotIn(token, " ".join(args))


    def test_non_github_and_deceptive_sources_never_receive_token(self):
        sources = (
            "https://forge.example/alice/repo.git",
            "https://github.com.evil.example/alice/repo.git",
            "https://github.com@evil.example/alice/repo.git",
            "https://evil.example@github.com/alice/repo.git",
            "http://github.com/alice/repo.git",
            "https://github.com:444/alice/repo.git",
            "https://github.com/alice/repo.git?redirect=evil",
            "git@github.com:alice/repo.git",
        )
        for source in sources:
            with self.subTest(source=source):
                calls = []

                def run(args, *, env, **kwargs):
                    if source in args:
                        calls.append(env.copy())
                        return SimpleNamespace(stdout=b"")
                    return SimpleNamespace(stdout=b"")

                token = Mock(return_value="secret")
                with patch.object(git_sync.subprocess, "run", side_effect=run):
                    git_sync.mirror_git(source, "https://github.com/dest/repo.git", token, safety={
                        "source_default": "main", "dest_default": "main",
                        "archive_factory": Mock(), "switch_default": Mock(),
                    })
                self.assertEqual(len(calls), 1)
                self.assertNotIn("GIT_CONFIG_VALUE_0", calls[0])
                token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
