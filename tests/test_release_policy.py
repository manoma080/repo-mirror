import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from mirror_test_support import patch_everywhere

import mirror

SOURCE = {"type": "github", "base_url": "https://github.com", "owner": "alice"}


class ReleasePolicyTests(unittest.TestCase):
    def setUp(self):
        secret = patch.dict("os.environ", {"MIRROR_TIMESTAMP_SECRET": "synthetic-test-only-secret"})
        secret.start()
        self.addCleanup(secret.stop)

    def test_initialization_rejects_shared_untagged_draft_name(self):
        class Source:
            def request(self, method, url):
                if url.endswith("/releases/latest"):
                    raise HTTPError(url, 404, "", {}, io.BytesIO())
                return {"clone_url": "https://github.com/alice/demo.git"}

            def pages(self, url):
                return [
                    {"id": 1, "tag_name": "a", "name": "same", "draft": True},
                    {"id": 2, "tag_name": "b", "name": "same", "draft": True},
                ]

        class Destination:
            def __init__(self):
                self.writes = []

            def request(self, method, url, data=None):
                if url.endswith("/repos/dest/mirror-alice-demo"):
                    return {
                        "description": mirror.source_marker(SOURCE, "demo") + " [repo-mirror:initializing:v1]"
                    }
                if "/releases/tags/" in url:
                    raise HTTPError(url, 404, "", {}, io.BytesIO())
                self.writes.append((method, url, data))
                return {}

            def pages(self, url):
                return [{"id": 5, "tag_name": "untagged-x", "name": "same", "draft": True}]

        dst = Destination()
        with patch_everywhere("mirror_git"), self.assertRaisesRegex(RuntimeError, "releases failed"):
            mirror.mirror_repository(SOURCE, "demo", "dest", Source(), dst, "token")
        self.assertFalse(any("/releases" in url for _, url, _ in dst.writes))

    def test_existing_release_gets_new_assets_and_updated_metadata(self):
        class Source:
            def request(self, *args):
                if args[1].endswith("/releases/latest"):
                    raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
                        args[1], 404, "", {}, None
                    )
                if "/releases/" in args[1]:
                    url, release_id = args[1].rsplit("/", 1)
                    return next(release for release in self.pages(url) if str(release["id"]) == release_id)
                return {"clone_url": "https://github.com/alice/demo.git"}

            def pages(self, url):
                if url.endswith("/releases"):
                    return [{"id": 1, "tag_name": "v1", "name": "new", "body": "notes"}]
                return [{"name": "a", "size": 1, "browser_download_url": "https://github.com/a"}]

            def download(self, url, out):
                out.write(b"a")

        class Destination:
            def __init__(self):
                self.release = {
                    "id": 2,
                    "name": "old",
                    "body": "old",
                    "upload_url": "https://uploads.github.com/x{?name}",
                }
                self.assets = []

            def request(self, method, url, data=None):
                if url.endswith("/repos/dest/mirror-alice-demo"):
                    return {"description": mirror.source_marker(SOURCE, "demo")}
                if "/releases/tags/" in url:
                    return self.release.copy()
                if method == "PATCH" and url.endswith("/releases/2"):
                    self.release.update(data)
                return {}

            def pages(self, url):
                return self.assets.copy()

            def download(self, url, out):
                out.write(self.content)

            def upload(self, url, data, content_type):
                self.content = data.read()
                self.assets.append(
                    {
                        "name": "a",
                        "size": len(self.content),
                        "state": "uploaded",
                        "url": "https://api.github.com/a",
                    }
                )

        dst = Destination()
        with patch_everywhere("mirror_git"):
            mirror.mirror_repository(SOURCE, "demo", "dest", Source(), dst, "token")
        self.assertEqual(dst.release["name"], "new")
        self.assertEqual(dst.release["body"], "notes")
        self.assertEqual([a["name"] for a in dst.assets], ["a"])

    def test_existing_mirror_considers_all_missing_releases(self):
        class Source:
            def request(self, *args):
                if args[1].endswith("/releases/latest"):
                    raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
                        args[1], 404, "", {}, None
                    )
                if "/releases/" in args[1]:
                    url, release_id = args[1].rsplit("/", 1)
                    return next(release for release in self.pages(url) if str(release["id"]) == release_id)
                return {"clone_url": "https://github.com/alice/demo.git"}

            def pages(self, url):
                return [{"id": n + 1, "tag_name": str(n)} for n in range(12)] if url.endswith("/releases") else []

        class Destination:
            def __init__(self):
                self.tags = []

            def pages(self, url):
                return []

            def request(self, method, url, data=None):
                if url.endswith("/repos/dest/mirror-alice-demo"):
                    return {"description": mirror.source_marker(SOURCE, "demo")}
                if "/releases/tags/" in url:
                    self.tags.append(url.rsplit("/", 1)[-1])
                    return {"id": 1, "name": "old"}
                return {}

        dst = Destination()
        with patch_everywhere("mirror_git"):
            mirror.mirror_repository(SOURCE, "demo", "dest", Source(), dst, "token")
        self.assertEqual(dst.tags, [str(n) for n in range(12)])

    def test_new_repository_persists_initialization_until_history_is_complete(self):
        class Source:
            def request(self, *args):
                if args[1].endswith("/releases/latest"):
                    raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
                        args[1], 404, "", {}, None
                    )
                return {"clone_url": "https://github.com/alice/demo.git"}

            def pages(self, url):
                raise RuntimeError("interrupted")

        class Destination:
            def __init__(self):
                self.description = None

            def request(self, method, url, data=None):
                if method == "GET":
                    raise HTTPError(url, 404, "", {}, io.BytesIO())
                if method == "POST":
                    self.description = data["description"]
                    return {"full_name": "dest/mirror-alice-demo"}
                return {}

        dst = Destination()
        with patch_everywhere("mirror_git"), self.assertRaisesRegex(RuntimeError, "interrupted"):
            mirror.mirror_repository(SOURCE, "demo", "dest", Source(), dst, "token")
        self.assertNotEqual(dst.description, mirror.source_marker(SOURCE, "demo"))
        mirror.check_existing({"description": dst.description}, mirror.source_marker(SOURCE, "demo"))
