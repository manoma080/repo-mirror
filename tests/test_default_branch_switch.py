import unittest
from unittest.mock import patch

from test_starter_asset import patch_everywhere  # noqa: F401  (same harness as the sibling suite)

from repo_mirror import repository as mirror


class _Src:
    """Minimal source: metadata lookup plus an empty release list."""

    def __init__(self):
        self.default_branch = "trunk"

    def request(self, method, url, data=None):
        return {
            "id": 11,
            "default_branch": self.default_branch,
            "clone_url": "https://github.com/alice/demo.git",
        }

    def pages(self, url):
        return []

    def download(self, url, output):
        pass


class _Dst:
    """Destination whose default branch converges to ``trunk`` after ``stale_reads``."""

    def __init__(self, stale_reads, target_branch="trunk"):
        self.stale_reads = stale_reads
        self.target_branch = target_branch
        self.default_branch = "main"
        self.reads = 0
        self.post_patch_reads = 0
        self.patches = 0

    def request(self, method, url, data=None):
        if method == "GET":
            self.reads += 1
            observed = self.default_branch
            if self.patches:
                self.post_patch_reads += 1
                if self.post_patch_reads > self.stale_reads:
                    observed = self.target_branch
            return {"id": 22, "default_branch": observed, "description": mirror.source_marker(_SOURCE, "demo")}
        if method == "PATCH":
            self.patches += 1
            return {}
        return {}

    def pages(self, url):
        return []

    def download(self, url, output):
        pass

    def upload(self, *args, **kwargs):
        raise AssertionError("no upload expected")


_SOURCE = {"type": "github", "base_url": "https://github.com", "owner": "alice", "name": "demo"}


def _run(dst, sleeping=None):
    """Drive mirror_repository with mirror_git replaced by a callback that switches the default."""

    def fake_git(clone_url, dest_url, token, update_default, **kwargs):
        kwargs["safety"]["switch_default"]("refs/heads/trunk")

    patches = [patch.object(mirror, "mirror_git", fake_git), patch.object(mirror, "mirror_releases", lambda *a, **k: None)]
    if sleeping is not None:
        patches.append(patch.object(mirror.time, "sleep", sleeping))
    for p in patches:
        p.start()
    try:
        return mirror.mirror_repository(_SOURCE, "demo", "dest", _Src(), dst, "token")
    finally:
        for p in patches:
            p.stop()


class DefaultBranchSwitchRetryTests(unittest.TestCase):
    def test_stale_read_after_patch_does_not_fail_the_run(self):
        dst = _Dst(stale_reads=2)
        slept = []
        _run(dst, sleeping=lambda seconds: slept.append(seconds))
        self.assertEqual(dst.patches, 1, "the default should be patched exactly once")
        self.assertTrue(slept, "a stale read should back off before re-reading")

    def test_branch_that_never_converges_still_fails(self):
        dst = _Dst(stale_reads=10_000)
        with self.assertRaisesRegex(RuntimeError, "destination default branch change not verified"):
            _run(dst, sleeping=lambda seconds: None)


if __name__ == "__main__":
    unittest.main()
