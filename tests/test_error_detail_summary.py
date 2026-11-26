"""Tests that a multi-release failure reports the first real error in the log."""

import unittest
from urllib.error import HTTPError

from repo_mirror.logging_utils import error_detail


class ErrorDetailSummaryTests(unittest.TestCase):
    def test_summary_names_count_and_first_release_error(self):
        first = HTTPError("https://example.invalid/a", 504, "Gateway Timeout", {}, None)
        first.mirror_operation = ("source", "asset", "GET")
        summary = RuntimeError("2 releases failed")
        summary.release_errors = [first, RuntimeError("asset download failed")]
        self.assertEqual(error_detail(summary), "2 releases failed; first: HTTP 504 (source asset GET)")

    def test_plain_runtime_error_is_unchanged(self):
        self.assertEqual(error_detail(RuntimeError("something else")), "RuntimeError")

    def test_scheduler_config_errors_round_trip_and_unknown_collapses(self):
        safelisted = (
            "invalid local configuration authority",
            "invalid manual dispatch",
            "Forgejo asset byte limit must be configured",
            "destination account type mismatch",
            "invalid installation or port",
            "invalid repository timeout",
            "incomplete app credentials",
            "invalid configuration repository",
            "invalid recovery secret file",
            "recovery secret too short",
            "recovery secret must be separate",
            "invalid journal directory",
            "lock must be an owner-only regular file",
            "configuration changed since fetch",
            "invalid port",
            "invalid owner",
            "invalid source URL",
            "duplicate configured source",
            "duplicate requested source",
            "conflicting destination",
            "requested source not exactly configured",
            "invalid GitHub App configuration",
        )
        for message in safelisted:
            with self.subTest(message=message):
                self.assertEqual(error_detail(ValueError(message)), message)

        self.assertEqual(
            error_detail(ValueError("unexpected secret https://private.example/token")),
            "ValueError",
        )


if __name__ == "__main__":
    unittest.main()
