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


if __name__ == "__main__":
    unittest.main()
