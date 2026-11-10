import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from test_latest_alignment import Destination, Source, release, run


class UntaggedDestination(Destination):
    def request(self, method, url, data=None):
        if method == "GET" and "/releases/tags/" in url:
            item = self.releases.get(url.rsplit("/", 1)[-1])
            if item and item.get("draft"):
                raise HTTPError(url, 404, "", {}, io.BytesIO())
        result = super().request(method, url, data)
        if method == "POST" and url.endswith("/releases") and data.get("draft"):
            self.releases[data["tag_name"]]["tag_name"] = "untagged-generated"
        return result


class DraftLifecycleTests(unittest.TestCase):
    def setUp(self):
        p = patch.dict("os.environ", {"MIRROR_TIMESTAMP_SECRET": "synthetic-test-only-secret"})
        p.start()
        self.addCleanup(p.stop)

    def test_completed_draft_rerun_and_publication(self):
        draft = dict(release(1), draft=True, body="notes")
        def no_latest():
            return HTTPError("https://x", 404, "", {}, io.BytesIO())
        dst = UntaggedDestination(False)
        run(Source([draft], no_latest()), dst)
        run(Source([draft], no_latest()), dst)
        published = dict(draft, draft=False, body="published")
        run(Source([published], published), dst)
        self.assertEqual(len(dst.posts), 1)
        self.assertFalse(dst.releases["1"]["draft"])
        self.assertEqual(dst.releases["1"]["body"], "published")

    def test_signed_draft_from_previous_run_does_not_block_new_release(self):
        draft = dict(release(1), draft=True, body="notes")
        published = release(2)
        dst = UntaggedDestination(False)
        run(Source([draft], HTTPError("https://x", 404, "", {}, io.BytesIO())), dst)
        run(Source([published, draft], published), dst)
        self.assertEqual([item["tag_name"] for item in dst.posts], ["1", "2"])
        self.assertTrue(dst.releases["1"]["draft"])
        self.assertFalse(dst.releases["2"]["draft"])

    def test_unknown_draft_still_blocks_new_release_with_authenticated_draft(self):
        draft = dict(release(1), draft=True)
        published = release(2)
        dst = UntaggedDestination(False)
        run(Source([draft], HTTPError("https://x", 404, "", {}, io.BytesIO())), dst)
        dst.releases["unknown"] = {"id": 99, "tag_name": "untagged-unknown", "draft": True}
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            run(Source([published, draft], published), dst)
        self.assertEqual([item["tag_name"] for item in dst.posts], ["1"])

    def test_synthetic_source_draft_is_not_copied_as_real_tag(self):
        draft = dict(release(1), draft=True, tag_name="untagged-synthetic")
        dst = UntaggedDestination(False)
        with self.assertRaisesRegex(ValueError, "source.*untagged"):
            run(Source([draft], HTTPError("https://x", 404, "", {}, io.BytesIO())), dst)
        self.assertEqual(dst.posts, [])

    def test_completed_marker_cannot_identify_a_different_release_id(self):
        draft = dict(release(1), draft=True)
        dst = UntaggedDestination(False)
        run(Source([draft], HTTPError("https://x", 404, "", {}, io.BytesIO())), dst)
        dst.releases["1"]["id"] = 77
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            run(Source([draft], HTTPError("https://x", 404, "", {}, io.BytesIO())), dst)
        self.assertEqual(len(dst.posts), 1)

    def test_older_synthetic_source_draft_is_rejected_even_if_exact_tag_exists(self):
        latest = release(2)
        draft = dict(release(1), draft=True, tag_name="untagged-synthetic")
        dst = UntaggedDestination(False)
        dst.releases["untagged-synthetic"] = dict(draft, id=10, draft=False)
        dst.releases["2"] = dict(latest, id=20)
        with self.assertRaisesRegex(ValueError, "source.*untagged"):
            run(Source([latest, draft], latest), dst)
        self.assertEqual(dst.releases["untagged-synthetic"]["id"], 10)
