import unittest

from repo_mirror.logging_utils import error_detail
from repo_mirror.repository import find_tag_releases


class AmbiguousDraftDiagnosticTests(unittest.TestCase):
    def test_unsigned_untagged_draft_fails_safely_with_actionable_diagnostic(self):
        draft = {"id": 437729977, "tag_name": "untagged-446b630fff7654a5b702",
                 "name": "v1.36", "draft": True,
                 "body": "GoldHEN plugin (release + debug PRX)"}
        with self.assertRaises(ValueError) as raised:
            find_tag_releases(None, "https://api.github.com/repos/dest/repo", "v1.36",
                              releases=[draft], marker="source")
        self.assertEqual(error_detail(raised.exception),
                         "ambiguous untagged destination draft; manual reconciliation required")
        self.assertEqual(error_detail(ValueError("ambiguous untagged destination draft; manual reconciliation required secret")),
                         "ValueError")
