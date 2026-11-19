import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from mirror_test_support import patch_everywhere

import mirror
from repo_mirror.forgejo_schedule import inactive_message, parse_inactive
from repo_mirror.forgejo_sync import INACTIVE_PREFIX, _source_url, report_inactive
from repo_mirror.repository import latest_release_time
from repo_mirror.snapshot_push import capture_snapshot
from repo_mirror.staleness import observe, observing, parse_inactive_days, stale


def git(*args, env=None):
    environment = dict(os.environ, **(env or {}))
    return subprocess.run(["git", *map(str, args)], check=True, capture_output=True, env=environment).stdout


def run(args):
    return git(*args)


class SnapshotActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        work = root / "work"
        git("init", "-q", "-b", "main", work)
        git("-C", work, "config", "user.name", "Test")
        git("-C", work, "config", "user.email", "test@example.com")
        (work / "file").write_text("content")
        git("-C", work, "add", "file")
        git("-C", work, "commit", "-qm", "first",
            env={"GIT_AUTHOR_DATE": "2020-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2020-01-01T00:00:00+0000"})
        git("-C", work, "tag", "-a", "v2", "-m", "release",
            env={"GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000"})
        git("clone", "-q", "--bare", work, root / "source")
        git("init", "-q", "--bare", root / "store")
        self.store = str(root / "store")
        self.source = str(root / "source")

    def test_newest_annotated_tag_date_wins_over_commit_date(self):
        snapshot = capture_snapshot(run, self.store, self.source)
        self.assertEqual(snapshot.latest, datetime(2026, 1, 1, tzinfo=UTC))
        self.assertEqual(set(snapshot), {"refs/heads/main", "refs/tags/v2"})

    def test_lightweight_tag_reports_the_commit_date(self):
        git("-C", str(Path(self.tmp.name) / "source"), "tag", "light", "refs/heads/main")
        snapshot = capture_snapshot(run, self.store, self.source)
        self.assertEqual(snapshot.latest, datetime(2026, 1, 1, tzinfo=UTC))
        self.assertIn("refs/tags/light", snapshot)

    def test_empty_source_has_no_activity(self):
        empty = Path(self.tmp.name) / "empty"
        git("init", "-q", "--bare", empty)
        snapshot = capture_snapshot(run, self.store, str(empty))
        self.assertFalse(snapshot)
        self.assertIsNone(snapshot.latest)


class ObservationTests(unittest.TestCase):
    def test_collector_keeps_the_newest_value_per_kind(self):
        with observing() as activity:
            observe("last_commit", datetime(2024, 1, 1, tzinfo=UTC))
            observe("last_commit", datetime(2020, 1, 1, tzinfo=UTC))
            observe("last_release", datetime(2025, 6, 1, tzinfo=UTC))
            observe("last_release", None)
        self.assertEqual(activity["last_commit"], datetime(2024, 1, 1, tzinfo=UTC))
        self.assertEqual(activity["last_release"], datetime(2025, 6, 1, tzinfo=UTC))

    def test_observation_without_a_collector_is_ignored(self):
        observe("last_commit", datetime(2024, 1, 1, tzinfo=UTC))

    def test_threshold_boundary_and_disable(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        stamp = datetime(2025, 1, 1, tzinfo=UTC)
        self.assertEqual(stale({"last_commit": stamp}, 365, now=now), (365, stamp))
        self.assertIsNone(stale({"last_commit": stamp}, 366, now=now))
        self.assertIsNone(stale({"last_commit": stamp}, 0, now=now))
        self.assertIsNone(stale({}, 365, now=now))

    def test_release_timestamp_prefers_publication_and_ignores_absent_values(self):
        releases = [
            {"created_at": "2024-01-01T00:00:00Z", "published_at": "2025-02-02T00:00:00Z"},
            {"created_at": "2026-03-03T00:00:00Z"},
            {"released_at": None},
            {"created_at": "not-a-date", "published_at": None},
        ]
        self.assertEqual(latest_release_time(releases), datetime(2026, 3, 3, tzinfo=UTC))
        self.assertIsNone(latest_release_time([]))

    def test_threshold_input_validation(self):
        self.assertEqual(parse_inactive_days("0"), 0)
        self.assertEqual(parse_inactive_days("365"), 365)
        for bad in ("-1", "abc", "1.5"):
            with self.assertRaises(ValueError):
                parse_inactive_days(bad)


class InactiveProtocolTests(unittest.TestCase):
    def test_report_round_trips_through_the_scheduler_parser(self):
        output = io.StringIO()
        with redirect_stderr(output):
            report_inactive("https://github.com/alice/demo", "2024-03-01", 640)
        line = output.getvalue().splitlines()[0]
        self.assertTrue(line.startswith(INACTIVE_PREFIX))
        self.assertEqual(
            parse_inactive(line[len(INACTIVE_PREFIX):].encode("utf-8")),
            {"url": "https://github.com/alice/demo", "since": "2024-03-01", "idle_days": 640},
        )

    def test_parser_rejects_malformed_records(self):
        def record(**value):
            return json.dumps(value).encode("utf-8")

        self.assertIsNone(parse_inactive(b"not json"))
        self.assertIsNone(parse_inactive(record(url="u", since="2024-03-01", idle_days=1, extra=2)))
        self.assertIsNone(parse_inactive(record(url="", since="2024-03-01", idle_days=1)))
        self.assertIsNone(parse_inactive(record(url="u", since="2024-3-1", idle_days=1)))
        self.assertIsNone(parse_inactive(record(url="u", since="2024-03-01", idle_days="1")))
        self.assertIsNone(parse_inactive(record(url="u", since="2024-03-01", idle_days=-1)))


    def test_worker_reports_the_configured_url_not_the_owner_name_key(self):
        # The scheduler matches records against configured URLs; the worker's
        # identity is an owner/name key, so the report must be rebuilt.
        entry = {"url": "https://github.com/alice/demo"}
        self.assertEqual(_source_url("alice/demo", entry), "https://github.com/alice/demo")

    def test_scheduler_only_reports_configured_urls(self):
        record = {"url": "https://github.com/alice/demo", "since": "2024-03-01", "idle_days": 640}
        self.assertEqual(
            inactive_message(record, {"https://github.com/alice/demo"}),
            "INACTIVE since 2024-03-01 (640 days)",
        )
        self.assertIsNone(inactive_message(record, {"https://github.com/other/demo"}))
        self.assertIsNone(inactive_message(None, {"https://github.com/alice/demo"}))


class CliInactiveWarningTests(unittest.TestCase):
    def setUp(self):
        secret = patch.dict(os.environ, {"MIRROR_TIMESTAMP_SECRET": "synthetic-test-only-secret"})
        secret.start()
        self.addCleanup(secret.stop)

    def run_cli(self, threshold):
        config = {
            "destination_account_type": "personal",
            "sources": [{"type": "github", "base_url": "https://github.com", "owner": "alice", "repos": ["demo"]}],
        }

        def fake_mirror(source, name, *args, **kwargs):
            observe("last_commit", datetime(2020, 1, 1, tzinfo=UTC))

        stderr = io.StringIO()
        with tempfile.NamedTemporaryFile(mode="w") as f:
            json.dump(config, f)
            f.flush()
            with (
                patch.dict(os.environ, {"GH_TOKEN": "token", "DESTINATION_OWNER": "dest"}),
                patch_everywhere("get_existing", return_value=None),
                patch_everywhere("mirror_repository", side_effect=fake_mirror),
                redirect_stderr(stderr),
            ):
                mirror.main(["--config", f.name, "--inactive-after-days", threshold])
        return stderr.getvalue()

    def test_summary_lists_the_inactive_repository(self):
        output = self.run_cli("365")
        self.assertIn("inactive: 1 of 1", output)
        self.assertIn("inactive repository 1 (source 1/1, alice/demo): last activity 2020-01-01", output)

    def test_zero_disables_the_warning(self):
        self.assertNotIn("inactive", self.run_cli("0"))


if __name__ == "__main__":
    unittest.main()
