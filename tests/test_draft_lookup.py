"""Tests for finding destination releases, including unpublished drafts."""

import unittest

from repo_mirror.repository import find_tag_releases


class FakeDestination:
    """Returns a fixed list of release objects from ``pages``."""

    def __init__(self, releases):
        self.releases = releases

    def pages(self, _path):
        return iter(self.releases)


class FindTagReleasesTests(unittest.TestCase):
    def test_exact_tag_match_wins(self):
        published = {"id": 1, "tag_name": "v1.36", "name": "v1.36", "draft": False}
        draft = {"id": 2, "tag_name": "untagged-abc", "name": "v1.36", "draft": True}
        found = find_tag_releases(FakeDestination([published, draft]), "t", "v1.36", "v1.36")
        self.assertEqual(found, [published])

    def test_untagged_draft_with_same_name_is_found(self):
        draft = {"id": 2, "tag_name": "untagged-abc", "name": "v1.36", "draft": True}
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            find_tag_releases(FakeDestination([draft]), "t", "v1.36", "v1.36")

    def test_untagged_published_or_other_name_is_ignored(self):
        other = {"id": 3, "tag_name": "untagged-def", "name": "v2.0", "draft": True}
        not_draft = {"id": 4, "tag_name": "untagged-ghi", "name": "v1.36", "draft": False}
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            find_tag_releases(FakeDestination([other, not_draft]), "t", "v1.36", "v1.36")

    def test_unnamed_draft_is_never_identified_by_missing_name(self):
        draft = {"id": 5, "tag_name": "untagged-xyz", "name": None, "draft": True}
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            find_tag_releases(FakeDestination([draft]), "t", "v1", None)


if __name__ == "__main__":
    unittest.main()
