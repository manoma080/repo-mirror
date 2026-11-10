"""Tests for which destination releases count as still pending."""

import json
import unittest

from repo_mirror.repository import release_marker_is_pending

SENTINEL = "[repo-mirror:pending:v3]"


def body_with_phase(phase):
    state = {"digest": "a" * 64, "phase": phase, "replace": False, "skipped": [], "signature": "b" * 64}
    return "notes\n" + SENTINEL + json.dumps(state, sort_keys=True, separators=(",", ":"))


class MarkerSelectionTests(unittest.TestCase):
    def test_waiting_source_marker_is_pending(self):
        self.assertTrue(release_marker_is_pending(body_with_phase("WAITING_SOURCE")))

    def test_bound_marker_is_pending_until_final_release_patch(self):
        self.assertTrue(release_marker_is_pending(body_with_phase("BOUND")))

    def test_body_without_marker_is_not_pending(self):
        self.assertFalse(release_marker_is_pending("just notes"))
        self.assertFalse(release_marker_is_pending(None))

    def test_unreadable_marker_stays_pending(self):
        self.assertTrue(release_marker_is_pending("notes\n" + SENTINEL + "{broken"))


if __name__ == "__main__":
    unittest.main()
