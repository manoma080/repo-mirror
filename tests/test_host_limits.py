import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from mirror_test_support import patch_everywhere

import mirror
from repo_mirror.config_named import normalize_config
from repo_mirror.forgejo_sync import parse_manifest as parse_forgejo_manifest
from repo_mirror.host_limits import (
    HostAdmission,
    HostLimits,
    source_host,
    take_admissible,
)


class SourceHostTests(unittest.TestCase):
    def test_host_and_port(self):
        self.assertEqual(source_host("https://github.com/alice/demo"), "github.com")
        self.assertEqual(source_host("https://git.etawen.dev:8443/earthonion/x"), "git.etawen.dev:8443")
        self.assertEqual(source_host("https://GitHub.com"), "github.com")

    def test_rejects_unsafe_or_incomplete_urls(self):
        for value in ("http://github.com", "https://user:pw@github.com/x", "https:///path", None, 5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                source_host(value)


class HostLimitsTests(unittest.TestCase):
    def test_absent_config_keeps_the_default(self):
        limits = HostLimits.from_config(None, default=16)
        self.assertEqual(limits.cap("github.com"), 16)
        self.assertEqual(limits.cap("git.etawen.dev"), 16)

    def test_per_host_and_default_entries(self):
        limits = HostLimits.from_config(
            {"github.com": 16, "git.etawen.dev": 4, "default": 8}, default=16
        )
        self.assertEqual(limits.cap("github.com"), 16)
        self.assertEqual(limits.cap("git.etawen.dev"), 4)
        self.assertEqual(limits.cap("gitlab.com"), 8)

    def test_invalid_configs_are_rejected(self):
        cases = [
            {},
            {"github.com": 0},
            {"github.com": -1},
            {"github.com": "4"},
            {"github.com": True},
            {"GitHub.com": 4},
            {" https://github.com": 4},
            {"github .com": 4},
            {1: 4},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                HostLimits.from_config(value, default=16)

    def test_admission_counts_and_releases(self):
        admission = HostAdmission(HostLimits({"github.com": 1}, default=2))
        self.assertTrue(admission.available("github.com"))
        admission.acquire("github.com")
        self.assertFalse(admission.available("github.com"))
        with self.assertRaises(RuntimeError):
            admission.acquire("github.com")
        admission.release("github.com")
        self.assertTrue(admission.available("github.com"))
        with self.assertRaises(RuntimeError):
            admission.release("github.com")

    def test_saturated_host_does_not_block_other_hosts(self):
        admission = HostAdmission(HostLimits({"a.example": 1}, default=2))
        queue = [
            ("one", "https://a.example/x/one"),
            ("two", "https://a.example/x/two"),
            ("three", "https://b.example/x/three"),
        ]
        host_of = lambda item: source_host(item[1])  # noqa: E731
        first = take_admissible(queue, admission, host_of)
        assert first is not None
        admission.acquire(host_of(first))
        # The next a.example item is skipped while a b.example item proceeds.
        second = take_admissible(queue, admission, host_of)
        assert second is not None
        self.assertEqual(second[0], "three")
        admission.acquire(host_of(second))
        self.assertIsNone(take_admissible(queue, admission, host_of))
        admission.release(host_of(first))
        third = take_admissible(queue, admission, host_of)
        assert third is not None
        self.assertEqual(third[0], "two")


class ConfigPassthroughTests(unittest.TestCase):
    def test_named_github_config_keeps_the_limits(self):
        config = {
            "host_concurrency_limits": {"github.com": 16, "git.etawen.dev": 4},
            "destinations": {"github-manoma080": {"type": "github", "url": "https://github.com",
                                                  "owner": "dest", "auth": "mirrorbot"}},
            "repositories": [{"source": {"url": "https://github.com/alice/demo"},
                              "mirrors": {"github-manoma080": {"name": "mirror-demo"}}}],
        }
        normalized = normalize_config(config)
        self.assertEqual(normalized["host_concurrency_limits"], {"github.com": 16, "git.etawen.dev": 4})

    def test_named_config_rejects_invalid_limits(self):
        config = {
            "host_concurrency_limits": {"github.com": 0},
            "destinations": {"github-manoma080": {"type": "github", "url": "https://github.com",
                                                  "owner": "dest", "auth": "mirrorbot"}},
            "repositories": [{"source": {"url": "https://github.com/alice/demo"},
                              "mirrors": {"github-manoma080": {"name": "mirror-demo"}}}],
        }
        with self.assertRaises(ValueError):
            normalize_config(config)

    def test_forgejo_manifest_accepts_the_limits(self):
        manifest = {
            "host_concurrency_limits": {"github.com": 3},
            "destinations": {"forgejo-home": {"type": "forgejo", "url": "http://127.0.0.1:3300",
                                              "owner": "labadmin", "auth": "token"}},
            "repositories": [{"source": {"url": "https://github.com/alice/demo"},
                              "mirrors": {"forgejo-home": {"name": "mirror-demo"}}}],
        }
        entries = parse_forgejo_manifest(manifest)
        self.assertEqual(entries[0]["destination_name"], "mirror-demo")


class CliHostLimitsTests(unittest.TestCase):
    def setUp(self):
        secret = patch.dict(os.environ, {"MIRROR_TIMESTAMP_SECRET": "synthetic-test-only-secret"})
        secret.start()
        self.addCleanup(secret.stop)

    def run_cli(self, config):
        observed = []
        lock = threading.Lock()
        active = {}

        def fake_mirror(source, name, owner, src, dst, token, account_type="organization", journal=None):
            host = source_host(source["base_url"])
            with lock:
                active[host] = active.get(host, 0) + 1
                observed.append((host, active[host]))
            time.sleep(0.15)
            with lock:
                active[host] -= 1

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
                mirror.main(["--config", f.name])
        return observed

    def test_same_host_is_capped_while_other_hosts_continue(self):
        config = {
            "host_concurrency_limits": {"github.com": 1, "git.example": 2},
            "destination_account_type": "personal",
            "sources": [
                {"type": "github", "base_url": "https://github.com", "owner": "alice",
                 "repos": ["one", "two", "three"]},
                {"type": "forgejo", "base_url": "https://git.example", "owner": "bob",
                 "repos": ["four", "five"]},
            ],
        }
        observed = self.run_cli(config)
        per_host = {}
        for host, concurrency in observed:
            per_host[host] = max(per_host.get(host, 0), concurrency)
        self.assertEqual(len(observed), 5)
        self.assertEqual(per_host["github.com"], 1)
        self.assertLessEqual(per_host["git.example"], 2)


if __name__ == "__main__":
    unittest.main()
