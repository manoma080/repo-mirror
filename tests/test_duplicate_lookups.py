import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from repo_mirror import cli, repository
from repo_mirror.sources import source_marker


class DuplicateLookupTests(unittest.TestCase):
    def setUp(self):
        env_patch = patch.dict(
            os.environ,
            {
                "GH_TOKEN": "test-token",
                "DESTINATION_OWNER": "dest",
                "MIRROR_TIMESTAMP_SECRET": "synthetic-test-only-secret",
            },
            clear=True,
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def test_preflight_destination_lookup_reused_by_mirror_repository(self):
        existing_source = {
            "type": "github",
            "base_url": "https://github.com",
            "owner": "alice",
            "name": "existing-repo",
            "mirror": {"name": "existing-repo"},
        }
        existing_marker = source_marker(existing_source, "existing-repo")
        config = {
            "destinations": {"main": {"account_type": "organization"}},
            "repositories": [
                {
                    "source": {"url": "https://github.com/alice/existing-repo"},
                    "mirrors": {"main": {"name": "existing-repo"}},
                },
                {
                    "source": {"url": "https://github.com/alice/new-repo"},
                    "mirrors": {"main": {"name": "new-repo"}},
                },
            ],
        }
        lock = threading.Lock()
        repo_gets = []

        class FakeSrc:
            def restrict_origin(self, origin):
                pass

            def request(self, method, url, data=None, **kwargs):
                if url.endswith("/repos/alice/existing-repo"):
                    return {
                        "id": 101,
                        "clone_url": "https://github.com/alice/existing-repo.git",
                        "default_branch": "main",
                    }
                if url.endswith("/repos/alice/new-repo"):
                    return {
                        "id": 102,
                        "clone_url": "https://github.com/alice/new-repo.git",
                        "default_branch": "main",
                    }
                raise HTTPError(url, 404, "not found", {}, None)

        class FakeDst:
            def request(self, method, url, data=None, **kwargs):
                if method == "GET" and (
                    url.endswith("/repos/dest/existing-repo") or url.endswith("/repos/dest/new-repo")
                ):
                    with lock:
                        repo_gets.append(url)
                    if url.endswith("/repos/dest/existing-repo"):
                        return {
                            "id": 201,
                            "description": existing_marker,
                            "default_branch": "main",
                        }
                    raise HTTPError(url, 404, "not found", {}, None)
                if method == "POST" and url.endswith("/orgs/dest/repos"):
                    return {
                        "id": 202,
                        "full_name": f"dest/{data['name']}",
                        "description": data["description"],
                        "default_branch": None,
                    }
                return {}

        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as cfg:
            json.dump(config, cfg)
            cfg.flush()
            with (
                patch.object(cli, "SourceAPI", return_value=FakeSrc()),
                patch.object(cli, "API", return_value=FakeDst()),
                patch.object(repository, "mirror_git"),
                patch.object(repository, "mirror_releases"),
            ):
                cli.main(["--config", cfg.name])

        self.assertEqual(repo_gets.count("https://api.github.com/repos/dest/existing-repo"), 1)
        self.assertEqual(repo_gets.count("https://api.github.com/repos/dest/new-repo"), 1)

    def test_empty_release_discovery_skips_latest_lookup(self):
        latest_calls = []

        class EmptySource:
            def pages(self, url):
                return []

            def request(self, method, url, data=None, *, _calls=latest_calls, **kwargs):
                if url.endswith("/releases/latest"):
                    _calls.append(url)
                raise AssertionError(f"unexpected request: {method} {url}")

        class NoWriteDst:
            def request(self, *args, **kwargs):
                raise AssertionError("unexpected destination request")

        repository.mirror_releases(
            {"type": "github", "owner": "alice"},
            "demo",
            "dest",
            EmptySource(),
            NoWriteDst(),
            "https://api.github.com/repos/alice/demo",
            "https://api.github.com/repos/dest/demo",
            "marker",
            initializing=False,
            validate_snapshot=None,
        )
        self.assertEqual(latest_calls, [])

        # Non-empty discovery (even draft-only) must still query /releases/latest.
        non_empty_latest_calls = []

        class DraftOnlySource:
            def pages(self, url):
                if url.endswith("/releases"):
                    return [
                        {
                            "id": 1,
                            "tag_name": "v1",
                            "name": "v1",
                            "body": "draft",
                            "draft": True,
                            "prerelease": False,
                        }
                    ]
                return []

            def request(self, method, url, data=None, **kwargs):
                if url.endswith("/releases/latest"):
                    non_empty_latest_calls.append(url)
                    raise HTTPError(url, 404, "not found", {}, None)
                if url.endswith("/releases/1"):
                    return self.pages(url.rsplit("/", 1)[0])[0]
                raise HTTPError(url, 404, "not found", {}, None)

        class DraftDst:
            def __init__(self):
                self.releases = {}

            def pages(self, url):
                return []

            def request(self, method, url, data=None, **kwargs):
                if method == "GET" and "/releases/tags/" in url:
                    raise HTTPError(url, 404, "not found", {}, None)
                if method == "POST" and url.endswith("/releases"):
                    rel = dict(data, id=50)
                    self.releases[50] = rel
                    return rel
                if method == "PATCH" and "/releases/" in url:
                    return dict(self.releases[50], **data)
                return {}

        repository.mirror_releases(
            {"type": "github", "owner": "alice"},
            "demo",
            "dest",
            DraftOnlySource(),
            DraftDst(),
            "https://api.github.com/repos/alice/demo",
            "https://api.github.com/repos/dest/demo",
            "marker",
            initializing=False,
        )
        self.assertEqual(len(non_empty_latest_calls), 1)

    def test_empty_release_discovery_with_validator_fetches_latest_and_fails_closed(self):
        latest_calls = []

        class InconsistentSource:
            def pages(self, url):
                return []

            def request(self, method, url, data=None, **kwargs):
                if url.endswith("/releases/latest"):
                    latest_calls.append(url)
                    return {
                        "id": 1,
                        "tag_name": "v1",
                        "name": None,
                        "body": None,
                        "draft": False,
                        "prerelease": False,
                    }
                raise AssertionError(f"unexpected request: {method} {url}")

        class NoWriteDst:
            def request(self, *args, **kwargs):
                raise AssertionError("unexpected destination request")

            def pages(self, *args, **kwargs):
                raise AssertionError("unexpected destination pages")

        with self.assertRaisesRegex(ValueError, "source latest release not eligible in listing"):
            repository.mirror_releases(
                {"type": "github", "owner": "alice"},
                "demo",
                "dest",
                InconsistentSource(),
                NoWriteDst(),
                "https://api.github.com/repos/alice/demo",
                "https://api.github.com/repos/dest/demo",
                "marker",
                initializing=False,
                validate_snapshot=lambda releases, latest: repository.validate_source_latest(latest, releases),
            )
        self.assertEqual(latest_calls, ["https://api.github.com/repos/alice/demo/releases/latest"])

    def test_per_release_tag_lookup_reuses_destination_release_listing(self):
        releases = [
            {
                "id": 20,
                "tag_name": "v2",
                "name": "v2",
                "body": "notes v2",
                "draft": False,
                "prerelease": False,
                "target_commitish": "main",
                "published_at": "2026-02-01T00:00:00Z",
            },
            {
                "id": 10,
                "tag_name": "v1",
                "name": "v1",
                "body": "notes v1",
                "draft": False,
                "prerelease": False,
                "target_commitish": "main",
                "published_at": "2026-01-01T00:00:00Z",
            },
        ]

        class FakeSrc:
            def pages(self, url):
                if url.endswith("/releases"):
                    return [dict(r) for r in releases]
                return []

            def request(self, method, url, data=None, **kwargs):
                if url.endswith("/releases/latest"):
                    return dict(releases[0])
                if url.endswith("/releases/20"):
                    return dict(releases[0])
                if url.endswith("/releases/10"):
                    return dict(releases[1])
                raise HTTPError(url, 404, "not found", {}, None)

        class FakeDst:
            def __init__(self):
                self.tag_gets = []
                self.list_pages = 0
                self.posts = []
                self.patches = []
                self.releases = {
                    500: {
                        "id": 500,
                        "tag_name": "v2",
                        "name": "v2",
                        "body": "notes v2",
                        "draft": False,
                        "prerelease": False,
                    }
                }
                self.next_id = 501

            def pages(self, url):
                if url.endswith("/releases"):
                    self.list_pages += 1
                    return [dict(r) for r in self.releases.values()]
                return []

            def request(self, method, url, data=None, **kwargs):
                if method == "GET" and "/releases/tags/" in url:
                    self.tag_gets.append(url)
                    tag = url.rsplit("/releases/tags/", 1)[1]
                    for rel in self.releases.values():
                        if rel.get("tag_name") == tag:
                            return dict(rel)
                    raise HTTPError(url, 404, "not found", {}, None)
                if method == "POST" and url.endswith("/releases"):
                    self.posts.append(dict(data))
                    rid = self.next_id
                    self.next_id += 1
                    rel = dict(data, id=rid)
                    self.releases[rid] = rel
                    return dict(rel)
                if method == "PATCH" and "/releases/" in url:
                    self.patches.append((url, dict(data)))
                    rid = int(url.rsplit("/releases/", 1)[1])
                    self.releases[rid].update(data)
                    return dict(self.releases[rid])
                return {}

        dst = FakeDst()
        repository.mirror_releases(
            {"type": "github", "owner": "alice"},
            "demo",
            "dest",
            FakeSrc(),
            dst,
            "https://api.github.com/repos/alice/demo",
            "https://api.github.com/repos/dest/demo",
            "marker",
            initializing=False,
        )
        self.assertEqual(dst.list_pages, 1)
        self.assertEqual(dst.tag_gets, [])
        self.assertEqual([p["tag_name"] for p in dst.posts], ["v1"])
        self.assertTrue(any(data.get("make_latest") == "true" for _, data in dst.patches))

    def test_fixture_request_count_reduction(self):
        # Fixture shape:
        # - 2 pinned GitHub-to-GitHub repositories (existing destinations):
        #   1) alice/with-releases: 2 releases (v2 already on destination + latest, 1 matching asset;
        #      v1 missing on destination, 1 asset) -> 2 releases, 2 assets total.
        #   2) alice/no-releases: 0 releases, 0 assets.
        source_with = {
            "type": "github",
            "base_url": "https://github.com",
            "owner": "alice",
            "name": "with-releases",
            "mirror": {"name": "with-releases", "source_id": 101, "destination_id": 202},
        }
        source_empty = {
            "type": "github",
            "base_url": "https://github.com",
            "owner": "alice",
            "name": "no-releases",
            "mirror": {"name": "no-releases", "source_id": 102, "destination_id": 203},
        }
        marker_with = source_marker(source_with, "with-releases")
        marker_empty = source_marker(source_empty, "no-releases")

        asset_bytes = {
            "https://github.com/alice/with-releases/releases/download/v2/a2.bin": b"asset-v2",
            "https://github.com/alice/with-releases/releases/download/v1/a1.bin": b"asset-v1",
        }
        releases_with = [
            {
                "id": 20,
                "tag_name": "v2",
                "name": "Release 2",
                "body": "notes v2",
                "draft": False,
                "prerelease": False,
                "target_commitish": "main",
                "published_at": "2026-02-01T00:00:00Z",
                "assets": [
                    {
                        "id": 2001,
                        "name": "a2.bin",
                        "size": len(asset_bytes["https://github.com/alice/with-releases/releases/download/v2/a2.bin"]),
                        "browser_download_url": "https://github.com/alice/with-releases/releases/download/v2/a2.bin",
                        "content_type": "application/octet-stream",
                    }
                ],
            },
            {
                "id": 10,
                "tag_name": "v1",
                "name": "Release 1",
                "body": "notes v1",
                "draft": False,
                "prerelease": False,
                "target_commitish": "main",
                "published_at": "2026-01-01T00:00:00Z",
                "assets": [
                    {
                        "id": 1001,
                        "name": "a1.bin",
                        "size": len(asset_bytes["https://github.com/alice/with-releases/releases/download/v1/a1.bin"]),
                        "browser_download_url": "https://github.com/alice/with-releases/releases/download/v1/a1.bin",
                        "content_type": "application/octet-stream",
                    }
                ],
            },
        ]

        config = {
            "destinations": {"main": {"account_type": "organization"}},
            "repositories": [
                {
                    "source": {"url": "https://github.com/alice/with-releases", "id": 101},
                    "mirrors": {"main": {"name": "with-releases", "id": 202}},
                },
                {
                    "source": {"url": "https://github.com/alice/no-releases", "id": 102},
                    "mirrors": {"main": {"name": "no-releases", "id": 203}},
                },
            ],
        }

        lock = threading.Lock()
        counts = {"src_requests": 0, "src_pages": 0, "src_downloads": 0, "dst_requests": 0, "dst_pages": 0, "dst_downloads": 0, "dst_uploads": 0}

        class FixtureSrc:
            def restrict_origin(self, origin):
                pass

            def request(self, method, url, data=None, **kwargs):
                with lock:
                    counts["src_requests"] += 1
                if url.endswith("/repos/alice/with-releases"):
                    return {"id": 101, "clone_url": "https://github.com/alice/with-releases.git", "default_branch": "main"}
                if url.endswith("/repos/alice/no-releases"):
                    return {"id": 102, "clone_url": "https://github.com/alice/no-releases.git", "default_branch": "main"}
                if url.endswith("/with-releases/releases/latest"):
                    return dict(releases_with[0])
                if url.endswith("/no-releases/releases/latest"):
                    raise HTTPError(url, 404, "not found", {}, None)
                if "/with-releases/releases/" in url:
                    rid = int(url.rsplit("/", 1)[1])
                    for rel in releases_with:
                        if rel["id"] == rid:
                            return dict(rel)
                raise HTTPError(url, 404, "not found", {}, None)

            def pages(self, url):
                with lock:
                    counts["src_pages"] += 1
                if url.endswith("/with-releases/releases"):
                    return [dict(r) for r in releases_with]
                if url.endswith("/no-releases/releases"):
                    return []
                for rel in releases_with:
                    if url.endswith(f"/with-releases/releases/{rel['id']}/assets"):
                        return [dict(a) for a in rel["assets"]]
                return []

            def download(self, url, output, **kwargs):
                with lock:
                    counts["src_downloads"] += 1
                output.write(asset_bytes[url])

        class FixtureDst:
            def __init__(self):
                self.next_release_id = 501
                self.next_asset_id = 901
                self.releases = {
                    500: {
                        "id": 500,
                        "tag_name": "v2",
                        "name": "Release 2",
                        "body": "notes v2",
                        "draft": False,
                        "prerelease": False,
                        "upload_url": "https://uploads.github.com/repos/dest/with-releases/releases/500/assets{?name,label}",
                    }
                }
                self.assets = {
                    500: [
                        {
                            "id": 900,
                            "name": "a2.bin",
                            "size": len(asset_bytes["https://github.com/alice/with-releases/releases/download/v2/a2.bin"]),
                            "state": "uploaded",
                            "url": "https://api.github.com/repos/dest/with-releases/releases/assets/900",
                            "payload": asset_bytes["https://github.com/alice/with-releases/releases/download/v2/a2.bin"],
                        }
                    ]
                }

            def request(self, method, url, data=None, **kwargs):
                with lock:
                    counts["dst_requests"] += 1
                if method == "GET" and url.endswith("/repos/dest/with-releases"):
                    return {"id": 202, "description": marker_with, "default_branch": "main"}
                if method == "GET" and url.endswith("/repos/dest/no-releases"):
                    return {"id": 203, "description": marker_empty, "default_branch": "main"}
                if method == "PUT" and url.endswith("/actions/permissions"):
                    return {}
                if method == "GET" and "/with-releases/releases/tags/" in url:
                    tag = url.rsplit("/releases/tags/", 1)[1]
                    with lock:
                        for rel in self.releases.values():
                            if rel.get("tag_name") == tag:
                                return dict(rel)
                    raise HTTPError(url, 404, "not found", {}, None)
                if method == "POST" and url.endswith("/with-releases/releases"):
                    with lock:
                        rid = self.next_release_id
                        self.next_release_id += 1
                        rel = {
                            "id": rid,
                            "tag_name": data.get("tag_name"),
                            "name": data.get("name"),
                            "body": data.get("body"),
                            "draft": bool(data.get("draft", False)),
                            "prerelease": bool(data.get("prerelease", False)),
                            "upload_url": f"https://uploads.github.com/repos/dest/with-releases/releases/{rid}/assets{{?name,label}}",
                        }
                        self.releases[rid] = rel
                        self.assets[rid] = []
                        return dict(rel)
                if method == "PATCH" and "/with-releases/releases/" in url:
                    rid = int(url.rsplit("/releases/", 1)[1])
                    with lock:
                        self.releases[rid].update(data)
                        return dict(self.releases[rid])
                return {}

            def pages(self, url):
                with lock:
                    counts["dst_pages"] += 1
                    if url.endswith("/with-releases/releases"):
                        return [dict(r) for r in self.releases.values()]
                    if "/with-releases/releases/" in url and url.endswith("/assets"):
                        rid = int(url.rsplit("/releases/", 1)[1].split("/", 1)[0])
                        return [dict(a) for a in self.assets.get(rid, [])]
                return []

            def download(self, url, output):
                with lock:
                    counts["dst_downloads"] += 1
                    aid = int(url.rsplit("/", 1)[1])
                    for items in self.assets.values():
                        for asset in items:
                            if asset["id"] == aid:
                                output.write(asset["payload"])
                                return
                raise HTTPError(url, 404, "not found", {}, None)

            def upload(self, url, data, content_type):
                parsed = urlparse(url)
                name = parse_qs(parsed.query)["name"][0]
                rid = int(parsed.path.split("/releases/", 1)[1].split("/", 1)[0])
                payload = data.read()
                with lock:
                    counts["dst_uploads"] += 1
                    aid = self.next_asset_id
                    self.next_asset_id += 1
                    self.assets[rid].append(
                        {
                            "id": aid,
                            "name": name,
                            "size": len(payload),
                            "state": "uploaded",
                            "url": f"https://api.github.com/repos/dest/with-releases/releases/assets/{aid}",
                            "payload": payload,
                        }
                    )

        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as cfg:
            json.dump(config, cfg)
            cfg.flush()
            with (
                patch.object(cli, "SourceAPI", return_value=FixtureSrc()),
                patch.object(cli, "API", return_value=FixtureDst()),
                patch.object(repository, "mirror_git"),
            ):
                cli.main(["--config", cfg.name])

        total = sum(counts.values())
        # Baseline on 3f0086f is 77 total requests:
        # - Site 1 removes 2 dst_requests (1 per repo)
        # - Site 2 is dropped (pre-PUT /actions/permissions recheck_pins() restored)
        # - Site 3 removes 1 src_request (/no-releases/releases/latest)
        # - Site 4 removes 2 dst_requests (/releases/tags/v2 and /releases/tags/v1)
        #   plus 4 pin-check requests (2 src_requests + 2 dst_requests) from the
        #   two eliminated _concluding_read tag lookups (already checked at listing).
        # Reduced total: 77 - 9 = 68 requests.
        self.assertEqual(total, 68, f"unexpected total={total} counts={counts}")
