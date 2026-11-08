import base64
import hashlib
import hmac
import json
import re
import unittest
from unittest.mock import patch

from mirror_test_support import patch_everywhere
from test_published_digest import DigestDestination, DigestSource
from test_published_digest import digest as _digest
from test_reconcile_assets import SOURCE, Destination, Source

import mirror

SECRET = "synthetic-test-only-secret"


def bound_body_with_digest(dst, new_digest):
    """Re-sign the interrupted run's BOUND marker with a foreign digest."""
    base, encoded = dst.release["body"].split("[repo-mirror:pending:v3]")
    state = json.loads(encoded)
    state.pop("signature", None)
    state["digest"] = new_digest
    payload = {
        "version": "v3",
        "source": mirror.source_marker(SOURCE, "demo"),
        "tag": "v1",
        "target": mirror.GITHUB + "/repos/dest/mirror-alice-demo",
        "release_id": dst.release["id"],
        "state": state,
    }
    message = (
        b"repo-mirror:release-recovery:v3\0"
        + json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    )
    state["signature"] = hmac.new(SECRET.encode(), message, hashlib.sha256).hexdigest()
    return (
        base
        + "[repo-mirror:pending:v3]"
        + json.dumps(state, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    )


class MutatingSource(Source):
    """Applies ``mutation`` to asset lists from the second fetch onward."""

    def __init__(self, mutation):
        self.mutation = mutation
        self.count = 0

    def pages(self, url):
        result = super().pages(url)
        if not url.endswith("/releases"):
            self.count += 1
            if self.count > 1:
                self.mutation(result)
        return result


def volatile_only(assets):
    for asset in assets:
        asset["download_count"] = 999999
        asset["updated_at"] = "2099-01-01T00:00:00Z"
        asset["created_at"] = "2099-01-01T00:00:00Z"
        asset["browser_download_url"] = "https://github.com/volatile-changed"
        asset["uploader"] = {"login": "someone-else"}
        asset["label"] = "new-label"
        asset["content_type"] = "application/x-changed"


def digest_change(assets):
    assets[0]["digest"] = "sha256:" + "0" * 64


def size_change(assets):
    assets[0]["size"] = 999


def name_change(assets):
    assets[0]["name"] = "renamed"


def id_change(assets):
    assets[0]["id"] = 424242


def asset_removed(assets):
    assets.pop()


def asset_added(assets):
    assets.append({"name": "extra", "size": 1, "browser_download_url": "https://github.com/extra"})


class StableManifestTests(unittest.TestCase):
    def setUp(self):
        secret = patch.dict("os.environ", {"MIRROR_TIMESTAMP_SECRET": "synthetic-test-only-secret"})
        secret.start()
        self.addCleanup(secret.stop)

    def run_mirror(self, src, dst):
        with patch_everywhere("mirror_git"):
            return mirror.mirror_repository(SOURCE, "demo", "dest", src, dst, "token")

    def test_volatile_metadata_only_change_does_not_abort(self):
        dst = Destination()
        dst.fail = False
        self.run_mirror(MutatingSource(volatile_only), dst)
        self.assertEqual(dst.assets["changed"], b"new")
        self.assertEqual(dst.release["body"], "notes")

    def test_stable_field_changes_abort(self):
        for mutation in (digest_change, size_change, name_change, id_change, asset_removed, asset_added):
            with self.subTest(mutation=mutation.__name__):
                dst = Destination()
                dst.fail = False
                with self.assertRaises(mirror.ReleaseRecoveryError):
                    self.run_mirror(MutatingSource(mutation), dst)
                self.assertEqual(dst.deleted, [])

    def test_volatile_change_during_pending_resume_does_not_abort(self):
        dst = Destination()
        with patch_everywhere("mirror_git"), self.assertRaises(RuntimeError):
            mirror.mirror_repository(SOURCE, "demo", "dest", Source(), dst, "token")
        self.assertIn("[repo-mirror:pending:v3]", dst.release["body"])
        dst.fail = False
        self.run_mirror(MutatingSource(volatile_only), dst)
        self.assertEqual(dst.assets["changed"], b"new")
        self.assertEqual(dst.release["body"], "notes")

    def test_stable_change_during_pending_resume_aborts(self):
        dst = Destination()
        with patch_everywhere("mirror_git"), self.assertRaises(RuntimeError):
            mirror.mirror_repository(SOURCE, "demo", "dest", Source(), dst, "token")
        dst.fail = False
        deleted_before = list(dst.deleted)
        with self.assertRaises(mirror.ReleaseRecoveryError):
            self.run_mirror(MutatingSource(size_change), dst)
        self.assertEqual(dst.deleted, deleted_before)
        self.assertNotIn("changed", dst.assets)

    def interrupted_bound_destination(self):
        """A destination whose release is BOUND to a foreign (polluted) digest."""
        dst = Destination()
        with patch_everywhere("mirror_git"), self.assertRaises(RuntimeError):
            mirror.mirror_repository(SOURCE, "demo", "dest", Source(), dst, "token")
        self.assertIn('"phase":"BOUND"', dst.release["body"])
        # Simulate a pre-fix BOUND marker: digest was hashed over the full
        # provider asset object, so it can never match the stable digest.
        dst.release["body"] = bound_body_with_digest(dst, "a" * 64)
        dst.fail = False
        return dst

    def test_polluted_bound_digest_migrates_when_destination_matches(self):
        dst = self.interrupted_bound_destination()
        # Destination still holds the pre-interruption bytes, which differ
        # from the current source, so first make it match the source.
        dst.assets = {"changed": b"new", "new": b"n"}
        self.run_mirror(Source(), dst)
        self.assertEqual(dst.release["body"], "notes")
        self.assertEqual(dst.assets["changed"], b"new")

    def test_polluted_bound_partial_upload_resumes_missing_assets(self):
        # An interrupted upload leaves only some assets at the destination.
        # Present assets that byte-match the source must not block the
        # migration; the missing asset is uploaded by the normal asset loop.
        dst = self.interrupted_bound_destination()
        dst.assets = {"changed": b"new"}
        deleted_before = list(dst.deleted)
        self.run_mirror(Source(), dst)
        self.assertEqual(dst.release["body"], "notes")
        self.assertEqual(dst.assets["changed"], b"new")
        self.assertIn("new", dst.assets)
        self.assertEqual(dst.deleted, deleted_before)

    def test_polluted_bound_empty_destination_uploads_all_assets(self):
        # BOUND release with no assets at the destination (the run stopped
        # right after binding). Nothing to verify at the destination, so the
        # normal asset loop uploads everything; no release is deleted.
        dst = self.interrupted_bound_destination()
        dst.assets = {}
        deleted_before = list(dst.deleted)
        self.run_mirror(Source(), dst)
        self.assertEqual(dst.release["body"], "notes")
        self.assertEqual(dst.assets.get("changed"), b"new")
        self.assertEqual(dst.deleted, deleted_before)

    def test_polluted_bound_digest_stays_manual_when_destination_differs(self):
        dst = self.interrupted_bound_destination()
        deleted_before = list(dst.deleted)
        with self.assertRaises(mirror.ReleaseRecoveryError):
            self.run_mirror(Source(), dst)
        self.assertEqual(dst.deleted, deleted_before)
        self.assertIn("[repo-mirror:pending:v3]", dst.release["body"])


# --- Legacy v1/v2 pending markers with a real GitHub asset shape ---


def real_github_asset(asset):
    """Fill a fake asset out to the shape the GitHub API actually returns.

    Real assets always carry ``created_at`` and ``digest`` alongside volatile
    provider fields (``download_count``, ``documentation_url``, ``uploader``,
    the full asset ``url``/``node_id``, ...). Pre-fix manifests appended
    ``created_at`` (after the published digest) when present, so a stored
    pending-marker digest for a real asset was computed over that form.
    """
    asset.update(
        {
            "id": 123456,
            "node_id": "RA_kwDOEXAMPLE",
            "url": "https://api.github.com/repos/alice/demo/releases/assets/123456",
            "label": "",
            "state": "uploaded",
            "content_type": "application/octet-stream",
            "download_count": 17,
            "created_at": "2024-03-01T12:00:00Z",
            "updated_at": "2024-03-01T12:00:00Z",
            "browser_download_url": "https://github.com/alice/demo/releases/download/v1/changed",
            "documentation_url": "https://docs.github.com/rest/releases/assets",
            "uploader": {
                "login": "alice",
                "id": 42,
                "avatar_url": "https://avatars.github.com/u/42",
                "type": "User",
            },
        }
    )
    return asset


def old_format_fingerprint(asset):
    """Digest the pre-fix code stored in v1/v2 pending markers.

    Entry order mirrors the historical main manifest: the five base fields,
    then the published digest when present, then ``created_at`` when present.
    Volatile fields (download_count, documentation_url, uploader, the full
    asset object) were never part of this form.
    """
    entry = [asset.get(k) for k in ("name", "size", "id", "updated_at", "browser_download_url")]
    published = asset.get("digest")
    if published is not None:
        entry.append(
            published.lower()
            if isinstance(published, str) and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", published)
            else published
        )
    if asset.get("created_at") is not None:
        entry.append(asset["created_at"])
    return hashlib.sha256(json.dumps([entry], ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def legacy_marker(version, fingerprint):
    return (
        "[repo-mirror:pending:"
        + version
        + "]"
        + base64.urlsafe_b64encode(json.dumps(["old", fingerprint]).encode()).decode()
    )


class RealGitHubAssetSource(DigestSource):
    """DigestSource whose assets carry the full real GitHub shape."""

    def pages(self, url):
        assets = super().pages(url)
        if not url.endswith("/releases"):
            for asset in assets:
                real_github_asset(asset)
        return assets


class RealGitHubLegacyTests(StableManifestTests):
    def test_real_github_shape_legacy_marker_migrates_via_byte_verification(self):
        for version in ("v1", "v2"):
            with self.subTest(version=version):
                src = RealGitHubAssetSource(_digest(b"new"))
                dst = DigestDestination(_digest(b"new"))
                fingerprint = old_format_fingerprint(src.pages("assets")[0])
                marker = legacy_marker(version, fingerprint)
                dst.release["name"] = marker
                self.run_mirror(src, dst)
                self.assertEqual(dst.release["name"], "new")
                self.assertEqual(dst.deleted, [])
                self.assertEqual(dst.assets["changed"], b"new")

    def test_real_github_shape_legacy_marker_refuses_changed_destination(self):
        # A digest-bearing marker with created_at must not take the direct
        # upgrade path: a destination that no longer matches the source
        # stays a manual-review stop.
        src = RealGitHubAssetSource(_digest(b"new"))
        dst = DigestDestination(None, b"old")
        fingerprint = old_format_fingerprint(src.pages("assets")[0])
        marker = legacy_marker("v2", fingerprint)
        dst.release["name"] = marker
        with self.assertRaises(mirror.ReleaseRecoveryError):
            self.run_mirror(src, dst)
        self.assertEqual(dst.release["name"], marker)
        self.assertEqual(dst.deleted, [])
        self.assertEqual(dst.assets["changed"], b"old")

    def test_legacy_migration_needs_no_volatile_fields(self):
        # download_count / documentation_url / uploader / label / content
        # type changing between the marker's creation and the resume run
        # must not break recognition: they were never in the stored digest.
        class VolatileRealSource(RealGitHubAssetSource):
            def __init__(self, metadata):
                super().__init__(metadata)
                self.count = 0

            def pages(self, url):
                assets = super().pages(url)
                if not url.endswith("/releases"):
                    self.count += 1
                    for asset in assets:
                        asset["download_count"] = 1000 + self.count
                        asset["documentation_url"] = "https://docs.github.com/changed"
                        asset["uploader"] = {"login": "someone-else"}
                        asset["label"] = "changed-label"
                        asset["content_type"] = "text/plain"
                return assets

        src = VolatileRealSource(_digest(b"new"))
        dst = DigestDestination(_digest(b"new"))
        # Marker built from the asset as first seen (download_count=17).
        fingerprint = old_format_fingerprint(RealGitHubAssetSource(_digest(b"new")).pages("assets")[0])
        dst.release["name"] = legacy_marker("v2", fingerprint)
        self.run_mirror(src, dst)
        self.assertEqual(dst.release["name"], "new")
        self.assertEqual(dst.deleted, [])


if __name__ == "__main__":
    unittest.main()
