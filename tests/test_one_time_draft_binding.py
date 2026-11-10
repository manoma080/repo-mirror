import hashlib
import json
import unittest

from repo_mirror.repository import find_tag_releases


class OneTimeDraftBindingTests(unittest.TestCase):
    def test_plan_produces_production_authenticated_v3_marker(self):
        from repo_mirror.one_time_draft_binding import make_binding
        source = {"id": 141, "tag_name": "v1.36", "name": "v1.36", "body": "GoldHEN plugin (release + debug PRX)", "draft": False, "prerelease": False}
        destination = {"id": 437729977, "tag_name": "untagged-446b630fff7654a5b702", "name": "v1.36", "body": source["body"], "draft": True, "prerelease": False}
        files = {"yt_adblock-debug.prx": "585a2a266718648cc35b8bf0992f75490187f260fe623860c6deafe42d14cdf9", "yt_adblock.prx": "81f4e52199e928de5932c84209fc39969f7f1386600bd81b00cbab547eedcfdc"}
        assets = dict(files, **{"TIMESTAMPS.txt": "96f8c48f6d31b0ea87b053aef1a076b1519ed96d177406815e43580cedb90a48"})
        body = make_binding(source, destination, files, assets, "test-secret")
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"MIRROR_TIMESTAMP_SECRET": "test-secret"}):
            draft = dict(destination, body=body)
            self.assertEqual(find_tag_releases(None, "https://api.github.com/repos/manoma080/mirror-earthonion-yt_adskip_ps4", "v1.36", releases=[draft], marker="[repo-mirror: forgejo:https://git.etawen.dev/earthonion/yt_adskip_ps4]"), [draft])
        state = json.loads(body.split("\n[repo-mirror:pending:v3]", 1)[1])
        self.assertEqual((state["phase"], state["replace"]), ("WAITING_SOURCE", False))
