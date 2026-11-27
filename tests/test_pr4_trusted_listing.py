import contextlib
import hashlib
import io
import unittest
from unittest.mock import patch

import mirror
from mirror_test_support import patch_everywhere
from repo_mirror.forgejo_source_adapter import ForgejoSourceAdapter, PublicForgejoSourceAdapter
from repo_mirror.github_api import SourceAPI
from repo_mirror.repository import mirror_releases
from test_reconcile_assets import SOURCE, Destination


def sha256_digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


class ListingAPI:
    def __init__(self, assets_spec, source_origin="https://github.com/alice/demo"):
        self.assets_spec = assets_spec
        self.source_origin = source_origin

    def request(self, method, url, data=None):
        if url.endswith("/releases/latest"):
            raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(url, 404, "", {}, None)
        if url.endswith("/assets"):
            return [dict(a) for a in self.assets_spec]
        if "/releases/1" in url:
            return {"id": 1, "tag_name": "v1", "name": "v1", "body": "notes"}
        return {"clone_url": self.source_origin + ".git"}

    def pages(self, url):
        if url.endswith("/releases"):
            return [{"id": 1, "tag_name": "v1", "name": "v1", "body": "notes"}]
        return [dict(a) for a in self.assets_spec]


class RecordingClient:
    def __init__(self, blobs):
        self.blobs = dict(blobs)
        self.downloads = []

    def _download_once(self, asset, out, **kwargs):
        self.downloads.append(asset["name"])
        out.write(self.blobs[asset["name"]])


class RecordingGitHubDestination(Destination):
    def __init__(self, assets_blobs, assets_digests):
        super().__init__()
        self.release = {
            "id": 2,
            "name": "v1",
            "body": "notes",
            "upload_url": "https://uploads.github.com/x{?name}",
        }
        self.assets = dict(assets_blobs)
        self.assets_digests = dict(assets_digests)
        self.fail = False
        self.downloads = []
        self._ids = {}

    def _id_for(self, name):
        if name not in self._ids:
            self._ids[name] = len(self._ids) + 1
        return self._ids[name]

    def _name_for(self, aid):
        for name, asset_id in self._ids.items():
            if asset_id == aid and name in self.assets:
                return name
        raise KeyError(aid)

    def request(self, method, url, data=None):
        if method == "DELETE":
            aid = int(url.rsplit("/", 1)[-1])
            name = self._name_for(aid)
            self.deleted.append(name)
            del self.assets[name]
            self.assets_digests.pop(name, None)
            return {}
        if method == "PATCH" and "/releases/assets/" in url:
            aid = int(url.rsplit("/", 1)[-1])
            old_name = self._name_for(aid)
            new_name = data["name"]
            self.assets[new_name] = self.assets.pop(old_name)
            self._ids[new_name] = self._ids.pop(old_name)
            if old_name in self.assets_digests:
                self.assets_digests[new_name] = self.assets_digests.pop(old_name)
            return {}
        return super().request(method, url, data)

    def pages(self, url):
        return [
            {
                "id": self._id_for(n),
                "name": n,
                "size": len(b),
                "state": "uploaded",
                "digest": self.assets_digests.get(n),
                "url": "https://api.github.com/assets/" + n,
            }
            for n, b in self.assets.items()
        ]

    def download(self, url, out):
        name = url.rsplit("/", 1)[-1]
        self.downloads.append(name)
        out.write(self.assets[name])

    def upload(self, url, data, content_type):
        super().upload(url, data, content_type)
        name = url.split("name=")[1]
        self.assets_digests[name] = sha256_digest(self.assets[name])


def _make_assets(blobs, digests, timestamp="2026-01-01T00:00:00Z", origin="https://github.com/alice/demo"):
    return [
        {
            "id": 100 + i,
            "name": name,
            "size": len(blob),
            "state": "uploaded",
            "digest": digests.get(name),
            "updated_at": timestamp,
            "browser_download_url": f"{origin}/releases/download/v1/{name}",
        }
        for i, (name, blob) in enumerate(blobs.items(), 1)
    ]


class PR4TrustedListingTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {"MIRROR_TIMESTAMP_SECRET": "synthetic-test-only-secret"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _run(self, src, dst):
        log = io.StringIO()
        with patch_everywhere("mirror_git"), contextlib.redirect_stderr(log):
            mirror.mirror_repository(SOURCE, "demo", "dest", src, dst, "token")
        return log.getvalue()

    def test_unchanged_asset_with_digest_skips_with_zero_gets(self):
        blobs = {"app.tar.gz": b"unchanged"}
        digests = {"app.tar.gz": sha256_digest(b"unchanged")}
        client = RecordingClient(blobs)
        src = ForgejoSourceAdapter("alice/demo", "token", api=ListingAPI(_make_assets(blobs, digests)), client=client)
        dst = RecordingGitHubDestination(blobs, digests)

        log = self._run(src, dst)

        self.assertEqual(client.downloads, [])
        self.assertEqual([d for d in dst.downloads if d != "REPO-MIRROR-MANIFEST.json"], [])
        self.assertEqual(dst.deleted, [])
        self.assertIn("skipped=1", log)
        self.assertIn("reason=matching-published-digests", log)

    def test_wrapped_github_source_api_with_digest_skips_with_zero_gets(self):
        blobs = {"app.tar.gz": b"unchanged"}
        digests = {"app.tar.gz": sha256_digest(b"unchanged")}
        api = SourceAPI("source-token")
        listing = ListingAPI(_make_assets(blobs, digests))
        dst = RecordingGitHubDestination(blobs, digests)

        with (
            patch.object(api, "request", side_effect=listing.request),
            patch.object(api, "pages", side_effect=listing.pages),
            patch("repo_mirror.forgejo_source_adapter.ForgejoSourceAdapter.download", side_effect=AssertionError("unexpected source download")),
            patch_everywhere("mirror_git"),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            mirror_releases(
                SOURCE,
                "demo",
                "dest",
                api,
                dst,
                "https://api.github.com/repos/alice/demo",
                "https://api.github.com/repos/dest/mirror-alice-demo",
                "marker",
                initializing=False,
            )

        self.assertEqual([d for d in dst.downloads if d != "REPO-MIRROR-MANIFEST.json"], [])
        self.assertEqual(dst.deleted, [])

    def test_unchanged_asset_with_checksum_file_digest_skips_with_zero_asset_gets(self):
        payload = b"unchanged"
        sum_line = f"{hashlib.sha256(payload).hexdigest()}  app.tar.gz\n".encode()
        blobs = {"app.tar.gz": payload, "SHA256SUMS": sum_line}
        src_digests = {"app.tar.gz": None, "SHA256SUMS": sha256_digest(sum_line)}
        dst_digests = {"app.tar.gz": sha256_digest(payload), "SHA256SUMS": sha256_digest(sum_line)}
        client = RecordingClient(blobs)
        src = ForgejoSourceAdapter("alice/demo", "token", api=ListingAPI(_make_assets(blobs, src_digests)), client=client)
        dst = RecordingGitHubDestination(blobs, dst_digests)

        log = self._run(src, dst)

        self.assertEqual([d for d in client.downloads if d == "app.tar.gz"], [])
        self.assertEqual([d for d in dst.downloads if d == "app.tar.gz"], [])
        self.assertEqual(dst.deleted, [])
        self.assertIn("skipped=2", log)

    def test_unchanged_asset_without_digest_still_downloads_and_compares(self):
        blobs = {"app.tar.gz": b"unchanged"}
        src_digests = {"app.tar.gz": None}
        dst_digests = {"app.tar.gz": sha256_digest(b"unchanged")}
        client = RecordingClient(blobs)
        src = ForgejoSourceAdapter("alice/demo", "token", api=ListingAPI(_make_assets(blobs, src_digests)), client=client)
        dst = RecordingGitHubDestination(blobs, dst_digests)

        # First run populates REPO-MIRROR-MANIFEST.json; second run must still
        # NOT skip via verified-timestamp-manifest when source digest is absent.
        self._run(src, dst)
        self.assertEqual(client.downloads, ["app.tar.gz"])
        self.assertIn("app.tar.gz", dst.downloads)

        client.downloads.clear()
        dst.downloads.clear()
        self._run(src, dst)
        self.assertEqual(client.downloads, ["app.tar.gz"])
        self.assertIn("app.tar.gz", dst.downloads)
        self.assertEqual(dst.deleted, [])

    def test_changed_asset_same_name_and_size_without_digest_is_not_skipped(self):
        src_blobs = {"app.tar.gz": b"new"}
        dst_blobs = {"app.tar.gz": b"old"}
        src_digests = {"app.tar.gz": None}
        dst_digests = {"app.tar.gz": sha256_digest(b"old")}
        client = RecordingClient(src_blobs)
        listing = ListingAPI(_make_assets(src_blobs, src_digests))
        src = ForgejoSourceAdapter("alice/demo", "token", api=listing, client=client)
        dst = RecordingGitHubDestination(dst_blobs, dst_digests)

        # Populate a signed manifest first while destination matches "old",
        # then silently replace source bytes with same name, size, and timestamp.
        client.blobs["app.tar.gz"] = b"old"
        self._run(src, dst)
        client.blobs["app.tar.gz"] = b"new"
        client.downloads.clear()
        dst.downloads.clear()

        self._run(src, dst)

        self.assertEqual(client.downloads, ["app.tar.gz"])
        self.assertIn("app.tar.gz", dst.downloads)
        self.assertEqual(dst.deleted, ["app.tar.gz", "REPO-MIRROR-MANIFEST.json"])
        self.assertEqual(dst.assets["app.tar.gz"], b"new")

    def test_changed_asset_with_mismatching_digest_is_not_skipped(self):
        src_blobs = {"app.tar.gz": b"new"}
        dst_blobs = {"app.tar.gz": b"old"}
        src_digests = {"app.tar.gz": sha256_digest(b"new")}
        dst_digests = {"app.tar.gz": sha256_digest(b"old")}
        client = RecordingClient(src_blobs)
        src = ForgejoSourceAdapter("alice/demo", "token", api=ListingAPI(_make_assets(src_blobs, src_digests)), client=client)
        dst = RecordingGitHubDestination(dst_blobs, dst_digests)

        self._run(src, dst)

        self.assertEqual(client.downloads, ["app.tar.gz"])
        self.assertEqual([d for d in dst.downloads if d != "REPO-MIRROR-MANIFEST.json"], ["app.tar.gz"])
        self.assertEqual(dst.deleted, ["app.tar.gz"])
        self.assertEqual(dst.assets["app.tar.gz"], b"new")

    def test_public_forgejo_source_keeps_existing_trust_level(self):
        blobs = {"app.tar.gz": b"unchanged"}
        src_digests = {"app.tar.gz": sha256_digest(b"unchanged")}
        dst_digests = {"app.tar.gz": None}
        origin = "https://code.example/alice/demo"
        listing = ListingAPI(_make_assets(blobs, src_digests, origin=origin), source_origin=origin)
        src_downloads = []

        def fake_api_download(url, out):
            src_downloads.append(url)
            out.write(blobs["app.tar.gz"])

        listing.download = fake_api_download
        src = PublicForgejoSourceAdapter(origin, api=listing)
        dst = RecordingGitHubDestination(blobs, dst_digests)

        forgejo_source = {"type": "forgejo", "base_url": "https://code.example", "owner": "alice"}
        with patch_everywhere("mirror_git"), contextlib.redirect_stderr(io.StringIO()):
            mirror_releases(
                forgejo_source,
                "demo",
                "dest",
                src,
                dst,
                src.base,
                "https://api.github.com/repos/dest/mirror-alice-demo",
                "marker",
                initializing=False,
            )

        # PublicForgejoSourceAdapter must NOT use authenticated-source-digest.
        self.assertEqual(len(src_downloads), 1)
        self.assertEqual(dst.deleted, [])


if __name__ == "__main__":
    unittest.main()
