import json
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch


from repo_mirror import cli


class ConcurrentMirroringTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.tmp_path = Path(temporary_directory.name)
        self.stderr = io.StringIO()
        stderr_redirect = redirect_stderr(self.stderr)
        stderr_redirect.__enter__()
        self.addCleanup(stderr_redirect.__exit__, None, None, None)
        env_patch = patch.dict(os.environ, {
            "MIRROR_TIMESTAMP_SECRET": "secret",
            "GH_TOKEN": "token",
            "DESTINATION_OWNER": "dest",
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_runtime_overhead_excludes_parallel_repository_time(self):
        import re

        config = self.tmp_path / "config.json"
        config.write_text(
            json.dumps(
                {
                    "destination_account_type": "organization",
                    "sources": [
                        {
                            "type": "github",
                            "base_url": "https://github.com",
                            "owner": "alice",
                            "repos": ["one", "two"],
                        }
                    ],
                }
            )
        )
        with (
            patch.object(cli, "get_existing", return_value=None),
            patch.object(cli, "check_existing"),
            patch.object(cli, "mirror_repository", side_effect=lambda *a, **kw: time.sleep(0.15)),
        ):
            cli.main(["--config", str(config)])
        log = self.stderr.getvalue()
        match = re.search(r"runtime total elapsed=([\d.]+)s overhead=([\d.]+)s", log)
        assert match and float(match.group(2)) < 0.1


    def test_concurrent_journal_updates_persist_all_entries(self):
        import os
        from concurrent.futures import ThreadPoolExecutor

        from repo_mirror.journal import FailureJournal

        path = self.tmp_path / "failures.json"
        journal = FailureJournal(path)
        keys = [f"{i:064x}:{i:064x}" for i in range(2)]
        first_replace = threading.Event()
        real_replace = os.replace
        calls = []

        def delayed_replace(src, dst):
            calls.append(1)
            if len(calls) == 1:
                first_replace.set()
                time.sleep(0.2)
            real_replace(src, dst)

        with (
            patch("repo_mirror.journal.os.replace", side_effect=delayed_replace),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first = pool.submit(journal.set, keys[0], True)
            assert first_replace.wait(2)
            second = pool.submit(journal.set, keys[1], True)
            first.result()
            second.result()
        assert FailureJournal(path).failures == set(keys)


    def test_simultaneous_token_requests_mint_only_once(self):
        from concurrent.futures import ThreadPoolExecutor

        from repo_mirror.github_api import AppTokenProvider

        provider = AppTokenProvider("123", "key", "owner")
        barrier = threading.Barrier(4)
        minted = []

        def mint():
            minted.append(1)
            time.sleep(0.1)
            return "fresh", time.time() + 3600

        def request():
            barrier.wait()
            return provider()

        with patch.object(provider, "_mint", side_effect=mint), ThreadPoolExecutor(max_workers=4) as pool:
            assert list(pool.map(lambda _: request(), range(4))) == ["fresh"] * 4
        assert len(minted) == 1


    def test_four_concurrent_repositories_have_grouped_logs_and_keep_failures(self):
        config = self.tmp_path / "config.json"
        config.write_text(
            json.dumps(
                {
                    "destination_account_type": "organization",
                    "sources": [
                        {
                            "type": "github",
                            "base_url": "https://github.com",
                            "owner": "alice",
                            "repos": [f"repo{i}" for i in range(6)],
                        }
                    ],
                }
            )
        )
        lock = threading.Lock()
        active = 0
        peak = 0
        barrier = threading.Barrier(4, timeout=2)

        def mirror(source, name, *args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            print(f"{name} begin", file=sys.stderr)
            if int(name[-1]) < 4:
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass
            print(f"{name} end", file=sys.stderr)
            with lock:
                active -= 1
            if name == "repo2":
                raise ValueError("unsafe detail")

        with (
            patch.object(cli, "get_existing", return_value=None),
            patch.object(cli, "check_existing"),
            patch.object(cli, "mirror_repository", side_effect=mirror),
        ):
            with self.assertRaisesRegex(RuntimeError, "1 repositories failed"):
                cli.main(["--config", str(config)])
        log = self.stderr.getvalue()
        assert peak == 4
        for i in range(6):
            section = (
                log.split(f": alice/repo{i} =====", 1)[1]
                .split("===== Repository", 1)[0]
                .split("===== Summary", 1)[0]
            )
            assert f"repo{i} begin" in section and f"repo{i} end" in section
            assert all(f"repo{j} begin" not in section for j in range(6) if j != i)
        assert "unsafe detail" not in log


    def test_temporary_log_creation_failure_does_not_stop_remaining_repositories(self):
        config = self.tmp_path / "config.json"
        config.write_text(json.dumps({"destination_account_type": "organization", "sources": [{
            "type": "github", "base_url": "https://github.com", "owner": "alice",
            "repos": [f"repo{i}" for i in range(6)],
        }]}))
        real_tempfile = cli.tempfile.TemporaryFile
        calls = 0
        lock = threading.Lock()
        processed = []

        def temporary_file(*args, **kwargs):
            nonlocal calls
            with lock:
                calls += 1
                first = calls == 1
            if first:
                raise OSError("private path")
            return real_tempfile(*args, **kwargs)

        with (patch.object(cli, "get_existing", return_value=None),
              patch.object(cli, "check_existing"),
              patch.object(cli.tempfile, "TemporaryFile", side_effect=temporary_file),
              patch.object(cli, "mirror_repository", side_effect=lambda source, name, *a, **kw: processed.append(name))):
            with self.assertRaisesRegex(RuntimeError, "1 repositories failed"):
                cli.main(["--config", str(config)])
        log = self.stderr.getvalue()
        assert len(processed) == 5
        assert "repositories=6 succeeded=5 failed=1" in log
        assert "private path" not in log


    def test_log_read_failure_does_not_stop_remaining_repositories(self):
        config = self.tmp_path / "config.json"
        config.write_text(json.dumps({"destination_account_type": "organization", "sources": [{
            "type": "github", "base_url": "https://github.com", "owner": "alice",
            "repos": [f"repo{i}" for i in range(6)],
        }]}))
        real_tempfile = cli.tempfile.TemporaryFile
        calls = 0
        lock = threading.Lock()
        processed = []

        class BrokenLog:
            def __init__(self, wrapped):
                self.wrapped = wrapped
            def __getattr__(self, name):
                return getattr(self.wrapped, name)
            def seek(self, *args):
                raise OSError("private log path")

        def temporary_file(*args, **kwargs):
            nonlocal calls
            with lock:
                calls += 1
                first = calls == 1
            file = real_tempfile(*args, **kwargs)
            return BrokenLog(file) if first else file

        with (patch.object(cli, "get_existing", return_value=None),
              patch.object(cli, "check_existing"),
              patch.object(cli.tempfile, "TemporaryFile", side_effect=temporary_file),
              patch.object(cli, "mirror_repository", side_effect=lambda source, name, *a, **kw: processed.append(name))):
            with self.assertRaisesRegex(RuntimeError, "1 repositories failed"):
                cli.main(["--config", str(config)])
        assert len(processed) == 6
        log = self.stderr.getvalue()
        assert "repositories=6 succeeded=5 failed=1" in log
        assert "private log path" not in log
