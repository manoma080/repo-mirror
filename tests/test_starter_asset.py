import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from mirror_test_support import patch_everywhere
from test_mirror import pending

import mirror


class StarterAssetTests(unittest.TestCase):
    def setUp(self):
        secret = patch.dict("os.environ", {"MIRROR_TIMESTAMP_SECRET": "synthetic-test-only-secret"})
        secret.start()
        self.addCleanup(secret.stop)

    def test_zero_byte_starter_requires_manual_removal_even_for_empty_source(self):
        source = {"type": "github", "base_url": "https://github.com", "owner": "alice", "name": "demo"}

        class Src:
            def request(self, *args):
                if args[1].endswith("/releases/latest"):
                    raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
                        args[1], 404, "", {}, None
                    )
                if args[1].endswith("/releases/1"):
                    return self.pages(args[1].rsplit("/", 1)[0])[0]
                return {"clone_url": "https://github.com/alice/demo.git"}

            def pages(self, url):
                if url.endswith("/releases"):
                    return [{"id": 1, "tag_name": "v1"}]
                return [{"name": "empty", "size": 0, "browser_download_url": "https://github.com/empty"}]

            def download(self, url, output):
                pass

        class Dst:
            def __init__(self):
                self.calls = []

            def request(self, method, url, data=None):
                self.calls.append((method, url))
                if url.endswith("/repos/dest/mirror-alice-demo"):
                    return {"description": mirror.source_marker(source, "demo")}
                if "/releases/tags/" in url:
                    return {"id": 2, "name": pending("empty", 0, "https://github.com/empty")}
                return {}

            def pages(self, url):
                return [{"name": "empty", "size": 0, "state": "starter", "url": "https://api.github.com/a"}]

            def download(self, url, output):
                pass

            def upload(self, *args):
                self.calls.append(("UPLOAD", ""))

        dst = Dst()
        with patch_everywhere("mirror_git"):
            with self.assertRaisesRegex(RuntimeError, "(?i)starter.*manual"):
                mirror.mirror_repository(source, "demo", "dest", Src(), dst, "token")
        self.assertFalse(any(method in ("DELETE", "UPLOAD", "PATCH") for method, url in dst.calls))

    def test_main_reports_changed_manifest_recovery_without_source_data(self):
        source = {"type": "github", "base_url": "https://github.com", "owner": "alice", "name": "demo"}

        class Src:
            def request(self, *args):
                if args[1].endswith("/releases/latest"):
                    raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
                        args[1], 404, "", {}, None
                    )
                if args[1].endswith("/releases/1"):
                    return self.pages(args[1].rsplit("/", 1)[0])[0]
                return {"clone_url": "https://github.com/alice/demo.git"}

            def pages(self, url):
                if url.endswith("/releases"):
                    return [{"id": 1, "tag_name": "https://example.test/?token=secret"}]
                return [
                    {"name": "secret-file", "size": 1, "browser_download_url": "https://github.com/secret"}
                ]

        class Dst:
            def request(self, method, url, data=None):
                if "/releases/tags/" in url:
                    return {"id": 2, "name": pending("other", 1, "https://github.com/other")}
                if url.endswith("/repos/dest/mirror-alice-demo"):
                    return {"description": mirror.source_marker(source, "demo")}
                return {}

        stderr = io.StringIO()
        with tempfile.NamedTemporaryFile(mode="w") as f:
            json.dump({
                "graphql": False,
                "destinations": {"main": {"account_type": "personal"}},
                "repositories": [{"source": {"url": "https://github.com/alice/demo"}, "mirrors": {"main": {}}}],
            }, f)
            f.flush()
            with (
                patch.dict(os.environ, {"GH_TOKEN": "token", "DESTINATION_OWNER": "dest"}),
                patch_everywhere("SourceAPI", return_value=Src()),
                patch_everywhere("API", side_effect=[object(), Dst()]),
                patch_everywhere("mirror_git"),
                redirect_stderr(stderr),
                self.assertRaises(RuntimeError),
            ):
                mirror.main(["--config", f.name])
        self.assertIn("assets changed", stderr.getvalue())
        self.assertIn("manual", stderr.getvalue())
        self.assertIn("repository 1", stderr.getvalue())
        self.assertNotIn("mirror-alice-demo", stderr.getvalue())
        self.assertNotIn("secret", stderr.getvalue())

    def test_main_reports_manual_action_for_starter_asset(self):
        stderr = io.StringIO()
        with tempfile.NamedTemporaryFile(mode="w") as f:
            json.dump({
                "graphql": False,
                "destinations": {"main": {"account_type": "personal"}},
                "repositories": [{"source": {"url": "https://github.com/alice/demo"}, "mirrors": {"main": {}}}],
            }, f)
            f.flush()
            with (
                patch.dict(os.environ, {"GH_TOKEN": "token", "DESTINATION_OWNER": "dest"}),
                patch_everywhere("get_existing", return_value=None),
                patch_everywhere("mirror_repository", side_effect=mirror.StarterAssetError()),
                redirect_stderr(stderr),
                self.assertRaises(RuntimeError),
            ):
                mirror.main(["--config", f.name])
        self.assertRegex(stderr.getvalue().lower(), "starter.*manual")
        self.assertIn("repository 1", stderr.getvalue())
        self.assertNotIn("mirror-alice-demo", stderr.getvalue())

    def _run_v3_starter_case(
        self,
        *,
        source_assets,
        dest_assets,
        upload_error=None,
        use_journal=False,
    ):
        source = {"type": "github", "base_url": "https://github.com", "owner": "alice", "name": "demo"}
        source_bytes = {a["name"]: b"A" * a["size"] for a in source_assets}

        class Src:
            def request(self, *args):
                if args[1].endswith("/releases/latest"):
                    raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
                        args[1], 404, "", {}, None
                    )
                if args[1].endswith("/releases/1"):
                    return {"id": 1, "tag_name": "v1", "name": "v1", "draft": False, "prerelease": False}
                return {"clone_url": "https://github.com/alice/demo.git"}

            def pages(self, url):
                if url.endswith("/releases"):
                    return [{"id": 1, "tag_name": "v1", "name": "v1", "draft": False, "prerelease": False}]
                return list(source_assets)

            def download(self, url, output):
                name = url.rsplit("/", 1)[-1]
                output.write(source_bytes[name])

        class Dst:
            def __init__(self):
                self.calls = []
                self.release = None
                self.assets = [dict(a) for a in dest_assets]
                self.blobs = {a["url"]: b"" for a in dest_assets if a.get("state") == "uploaded"}

            def request(self, method, url, data=None):
                self.calls.append((method, url))
                if url.endswith("/repos/dest/mirror-alice-demo"):
                    return {"description": mirror.source_marker(source, "demo")}
                if "/releases/tags/" in url:
                    raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
                        url, 404, "", {}, None
                    )
                if method == "POST" and url.endswith("/releases"):
                    self.release = dict(data, id=2, upload_url="https://uploads.github.com/x{?name}")
                    return dict(self.release)
                if method == "PATCH" and url.endswith("/releases/2"):
                    self.release.update(data)
                    return dict(self.release)
                if method == "GET" and url.endswith("/releases/2"):
                    return dict(self.release)
                if method == "DELETE" and "/releases/assets/" in url:
                    aid = int(url.rsplit("/", 1)[-1])
                    self.assets = [a for a in self.assets if a.get("id") != aid]
                    return {}
                return {}

            def pages(self, url):
                if url.endswith("/releases"):
                    return []
                return [dict(a) for a in self.assets]

            def download(self, url, output):
                if any(a.get("url") == url and a.get("state") == "starter" for a in self.assets):
                    raise AssertionError("must not download starter asset")
                output.write(self.blobs.get(url, b""))

            def upload(self, upload_url, data, content_type):
                self.calls.append(("UPLOAD", upload_url))
                if upload_error is not None:
                    raise upload_error
                name = upload_url.split("name=")[-1]
                payload = data.read()
                asset_url = f"https://api.github.com/{name}"
                self.blobs[asset_url] = payload
                self.assets.append(
                    {
                        "id": 99,
                        "name": name,
                        "size": len(payload),
                        "state": "uploaded",
                        "url": asset_url,
                    }
                )
                return self.assets[-1]

        dst = Dst()
        if use_journal:
            with tempfile.TemporaryDirectory() as td:
                journal = mirror.FailureJournal(os.path.join(td, "failures.json"))
                with patch_everywhere("mirror_git"):
                    mirror.mirror_repository(source, "demo", "dest", Src(), dst, "token", journal=journal)
                return dst, journal.failures
        with patch_everywhere("mirror_git"):
            mirror.mirror_repository(source, "demo", "dest", Src(), dst, "token")
        return dst, None

    def test_v3_zero_byte_starter_auto_heals_and_reuploads(self):
        dst, _ = self._run_v3_starter_case(
            source_assets=[{"id": 10, "name": "app.bin", "size": 5, "browser_download_url": "https://github.com/app.bin"}],
            dest_assets=[{"id": 20, "name": "app.bin", "size": 0, "state": "starter", "url": "https://api.github.com/20"}],
        )
        delete_idx = next(i for i, (m, u) in enumerate(dst.calls) if m == "DELETE" and u.endswith("/releases/assets/20"))
        upload_idx = next(i for i, (m, _) in enumerate(dst.calls) if m == "UPLOAD")
        self.assertLess(delete_idx, upload_idx)
        self.assertNotIn("[repo-mirror:pending:v3]", dst.release["body"])
        self.assertEqual([a["state"] for a in dst.assets], ["uploaded"])
        self.assertEqual(dst.assets[0]["size"], 5)

    def test_v3_starter_auto_heal_surfaces_reupload_failure(self):
        for use_journal in (False, True):
            expected = "release asset uploads failed" if use_journal else "upload failed"
            with (
                self.subTest(use_journal=use_journal),
                self.assertRaisesRegex(RuntimeError, expected),
            ):
                self._run_v3_starter_case(
                    source_assets=[
                        {"id": 10, "name": "app.bin", "size": 5, "browser_download_url": "https://github.com/app.bin"}
                    ],
                    dest_assets=[
                        {"id": 20, "name": "app.bin", "size": 0, "state": "starter", "url": "https://api.github.com/20"}
                    ],
                    upload_error=RuntimeError("upload failed"),
                    use_journal=use_journal,
                )

    def test_starter_auto_heal_refuses_non_placeholder_cases(self):
        cases = (
            (
                "nonzero_size",
                [{"id": 10, "name": "app.bin", "size": 5, "browser_download_url": "https://github.com/app.bin"}],
                [{"id": 20, "name": "app.bin", "size": 10, "state": "starter", "url": "https://api.github.com/20"}],
            ),
            (
                "orphaned_name",
                [{"id": 10, "name": "app.bin", "size": 5, "browser_download_url": "https://github.com/app.bin"}],
                [{"id": 20, "name": "other.bin", "size": 0, "state": "starter", "url": "https://api.github.com/20"}],
            ),
            (
                "missing_id",
                [{"id": 10, "name": "app.bin", "size": 5, "browser_download_url": "https://github.com/app.bin"}],
                [{"name": "app.bin", "size": 0, "state": "starter", "url": "https://api.github.com/20"}],
            ),
        )
        for label, src_assets, dst_assets in cases:
            with self.subTest(label=label), self.assertRaises(mirror.StarterAssetError):
                self._run_v3_starter_case(source_assets=src_assets, dest_assets=dst_assets)

    def test_legitimate_zero_byte_uploaded_asset_is_never_deleted(self):
        dst, _ = self._run_v3_starter_case(
            source_assets=[{"id": 10, "name": "empty", "size": 0, "browser_download_url": "https://github.com/empty"}],
            dest_assets=[{"id": 20, "name": "empty", "size": 0, "state": "uploaded", "url": "https://api.github.com/20"}],
        )
        self.assertFalse(any(m in ("DELETE", "UPLOAD") for m, _ in dst.calls))
        self.assertNotIn("[repo-mirror:pending:v3]", dst.release["body"])
