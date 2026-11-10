import unittest

from repo_mirror.repository import find_tag_releases


class Destination:
    def __init__(self, releases):
        self.releases = releases

    def pages(self, url):
        return self.releases


class DraftIdentitySafetyTests(unittest.TestCase):
    def test_unnamed_untagged_draft_blocks_creation(self):
        dst = Destination([{"id": 1, "tag_name": "untagged-x", "name": None, "draft": True}])
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            find_tag_releases(dst, "target", "v1", None)

    def test_unique_name_is_not_identity(self):
        dst = Destination([{"id": 1, "tag_name": "untagged-x", "name": "same", "draft": True}])
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            find_tag_releases(dst, "target", "v1", "same")

    def test_id_bound_recovery_marker_authenticates_untagged_draft(self):
        import hashlib
        import hmac
        import json
        from unittest.mock import patch
        from repo_mirror.repository import select_existing_releases

        state = {"phase": "WAITING_SOURCE", "digest": None, "replace": False, "skipped": []}
        payload = {"version": "v3", "source": "source", "tag": "v1", "target": "target", "release_id": 5, "state": state}
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
        signature = hmac.new(b"secret", b"repo-mirror:release-recovery:v3\0" + encoded, hashlib.sha256).hexdigest()
        draft = {"id": 5, "tag_name": "untagged-x", "name": None, "draft": True,
                 "body": "notes\n[repo-mirror:pending:v3]" + json.dumps(dict(state, signature=signature))}
        with patch.dict("os.environ", {"MIRROR_TIMESTAMP_SECRET": "secret"}):
            self.assertEqual(find_tag_releases(Destination([draft]), "target", "v1", marker="source"), [draft])
            self.assertEqual(select_existing_releases([{"tag_name": "v1"}], [draft], set(), "source", "target"), [{"tag_name": "v1"}])
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                find_tag_releases(Destination([draft]), "target", "other", marker="source")
