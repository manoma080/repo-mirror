import contextlib
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import HTTPSHandler, build_opener

from mirror_test_support import patch_everywhere
from test_reconcile_assets import SOURCE, Destination, Source

import mirror
from repo_mirror import github_api
from repo_mirror.forgejo_releases import GitHubReleases
from repo_mirror.forgejo_source_adapter import ForgejoSourceAdapter, _AssetRedirect
from repo_mirror.github_api import (
    API,
    APICallCounter,
    AppTokenProvider,
    SafeRedirect,
    SourceAPI,
    api_call_metrics_snapshot,
    format_api_call_summary,
    reset_api_call_metrics,
)


class _StubResponse:
    def __init__(self, body=b"{}", status=200, headers=None, url="https://api.github.com/repos/a/b"):
        self._buf = io.BytesIO(body)
        self.status = status
        self.code = status
        self.msg = "OK"
        msg = Message()
        for k, v in (headers or {}).items():
            msg[k] = v
        self.headers = msg
        self._url = url

    def read(self, size=-1):
        return self._buf.read(size)

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def geturl(self):
        return self._url

    def info(self):
        return self.headers

    def close(self):
        self._buf.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _RedirectingHTTPSHandler(HTTPSHandler):
    """Test handler that replaces default HTTPSHandler and serves queued responses/redirects."""

    def __init__(self, responses):
        super().__init__()
        self._responses = list(responses)

    def https_open(self, req):
        status, headers, body = self._responses.pop(0)
        return _StubResponse(body=body, status=status, headers=headers, url=req.full_url)


class APICallMetricsTests(unittest.TestCase):
    def setUp(self):
        reset_api_call_metrics()
        with github_api._cooldown_lock:
            github_api._cooldowns.clear()
        self.addCleanup(reset_api_call_metrics)
        self.addCleanup(self._clear_cooldowns)

    def _clear_cooldowns(self):
        with github_api._cooldown_lock:
            github_api._cooldowns.clear()

    def test_request_counts_success_and_retries(self):
        api = API("test-token")
        err = HTTPError("https://api.github.com/repos/a/b", 503, "Unavailable", {}, None)
        self.addCleanup(err.close)
        api.opener.open = MagicMock(side_effect=[err, _StubResponse(b'{"ok": true}')])
        with patch.object(github_api.time, "sleep"):
            result = api.request("GET", "https://api.github.com/repos/a/b")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            api_call_metrics_snapshot(),
            {("api.github.com", "destination", "API", "GET"): 2},
        )

    def test_cooldown_fast_fail_does_not_count_attempt(self):
        api = API("test-token")
        api.opener.open = MagicMock(side_effect=AssertionError("opener must not be called"))
        with github_api._cooldown_lock:
            github_api._cooldowns["api.github.com"] = time.monotonic() + 3600
        with self.assertRaisesRegex(RuntimeError, "already rate limited"):
            api.request("GET", "https://api.github.com/repos/a/b")
        self.assertEqual(api_call_metrics_snapshot(), {})

    def test_per_host_per_role_and_per_operation_separation(self):
        src = SourceAPI(None)
        src.opener.open = MagicMock(side_effect=lambda *a, **k: _StubResponse(b"[]"))
        dst = API("test-token")
        dst.opener.open = MagicMock(side_effect=lambda *a, **k: _StubResponse(b"{}"))

        src.request("GET", "https://forge.example.test/api/v1/repos/alice/demo")
        dst.request("GET", "https://api.github.com/repos/dest/demo")
        dst.request("PUT", "https://api.github.com/repos/dest/demo/actions/permissions", {"enabled": False})
        dst.upload(
            "https://uploads.github.com/repos/dest/demo/releases/1/assets?name=a.bin",
            io.BytesIO(b"abc"),
            "application/octet-stream",
        )

        self.assertEqual(
            api_call_metrics_snapshot(),
            {
                ("forge.example.test", "source", "API", "GET"): 1,
                ("api.github.com", "destination", "API", "GET"): 1,
                ("api.github.com", "destination", "API", "PUT"): 1,
                ("uploads.github.com", "destination", "API", "POST"): 1,
            },
        )
        summary = format_api_call_summary()
        self.assertNotIn("\n", summary)
        self.assertEqual(
            summary,
            "api_calls total=4 github=2 uploads=1 other=1 graphql=0 "
            "by_role=destination:3,source:1 "
            "by_operation=api_get:2,api_post:1,api_put:1",
        )
        self.assertNotIn("forge.example.test", summary)
        self.assertNotIn("https://", summary)

    def test_authenticated_download_and_redirect_hop_counted_once_each(self):
        dst = API("test-token")
        dst.opener = build_opener(
            SafeRedirect(),
            _RedirectingHTTPSHandler(
                [
                    (302, {"Location": "https://objects.githubusercontent.com/signed-asset"}, b""),
                    (200, {"Content-Length": "4"}, b"data"),
                ]
            ),
        )
        output = io.BytesIO()
        dst.download("https://api.github.com/repos/dest/demo/releases/assets/10", output)
        self.assertEqual(output.getvalue(), b"data")
        self.assertEqual(
            api_call_metrics_snapshot(),
            {
                ("api.github.com", "destination", "asset", "GET"): 1,
                ("objects.githubusercontent.com", "destination", "asset-hop", "GET"): 1,
            },
        )

    def test_unauthenticated_download_counts_each_redirect_hop(self):
        src = API()
        hop1 = MagicMock(status=302)
        hop1.getheader.side_effect = lambda k, d=None: "https://cdn.example.test/file.bin" if k == "Location" else d
        hop2 = MagicMock(status=200)
        hop2.getheader.side_effect = lambda k, d=None: "3" if k == "Content-Length" else d
        hop2.read.side_effect = [b"xyz", b""]

        conn1 = MagicMock()
        conn1.getresponse.return_value = hop1
        conn2 = MagicMock()
        conn2.getresponse.return_value = hop2

        output = io.BytesIO()
        with (
            patch.object(github_api.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]),
            patch.object(github_api.socket, "socket"),
            patch.object(github_api.ssl, "create_default_context"),
            patch.object(github_api.http.client, "HTTPSConnection", side_effect=[conn1, conn2]),
        ):
            src.download("https://forge.example.test/alice/demo/releases/download/v1/file.bin", output)

        self.assertEqual(output.getvalue(), b"xyz")
        self.assertEqual(
            api_call_metrics_snapshot(),
            {
                ("forge.example.test", "source", "asset-hop", "GET"): 1,
                ("cdn.example.test", "source", "asset-hop", "GET"): 1,
            },
        )

    def test_app_token_provider_mint_counts_attempts_and_retries(self):
        provider = AppTokenProvider("12345", "synthetic-key", "dest", "personal")
        err = HTTPError("https://api.github.com/users/dest/installation", 502, "Bad Gateway", {}, None)
        self.addCleanup(err.close)
        install_resp = _StubResponse(json.dumps({"id": 7, "account": {"login": "dest"}}).encode())
        token_resp = _StubResponse(
            json.dumps({"token": "ghs_test", "expires_at": "2099-01-01T00:00:00Z"}).encode()
        )
        fake_opener = MagicMock()
        fake_opener.open.side_effect = [err, install_resp, token_resp]

        with (
            patch.object(github_api, "build_opener", return_value=fake_opener),
            patch.object(
                github_api.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout=b"sig"),
            ),
            patch.object(github_api.time, "sleep"),
        ):
            token = provider()

        self.assertEqual(token, "ghs_test")
        self.assertEqual(
            api_call_metrics_snapshot(),
            {
                ("api.github.com", "destination", "API", "GET"): 2,
                ("api.github.com", "destination", "API", "POST"): 1,
            },
        )

    def test_github_releases_download_once_counted_via_safe_redirect(self):
        client = GitHubReleases("alice/demo", "source-token")
        client.api.opener = build_opener(
            _AssetRedirect(),
            _RedirectingHTTPSHandler(
                [
                    (302, {"Location": "https://release-assets.githubusercontent.com/blob"}, b""),
                    (200, {"Content-Length": "5"}, b"hello"),
                ]
            ),
        )
        adapter = ForgejoSourceAdapter("alice/demo", "source-token", client=client)
        asset = {
            "id": 42,
            "size": 5,
            "browser_download_url": "https://github.com/alice/demo/releases/download/v1/a.bin",
        }
        adapter._assets[asset["browser_download_url"]] = asset
        output = io.BytesIO()
        adapter.download(asset["browser_download_url"], output)
        self.assertEqual(output.getvalue(), b"hello")
        self.assertEqual(
            api_call_metrics_snapshot(),
            {
                ("api.github.com", "source", "asset", "GET"): 1,
                ("release-assets.githubusercontent.com", "source", "asset-hop", "GET"): 1,
            },
        )

    def test_thread_safe_concurrent_increments(self):
        counter = APICallCounter()
        workers = 16
        per_worker = 250
        barrier = threading.Barrier(workers)

        def run_worker(worker_id):
            barrier.wait()
            role = "destination" if worker_id % 2 == 0 else "source"
            host = "api.github.com" if worker_id % 4 != 3 else "uploads.github.com"
            method = "POST" if host == "uploads.github.com" else "GET"
            for _ in range(per_worker):
                counter.record(host, role, "API", method)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(run_worker, range(workers)))

        snapshot = counter.snapshot()
        self.assertEqual(sum(snapshot.values()), workers * per_worker)
        summary = counter.format_summary()
        self.assertIn(f"api_calls total={workers * per_worker} ", summary)
        self.assertIn("github=3000 ", summary)
        self.assertIn("uploads=1000 ", summary)

    def test_empty_summary_format_is_stable(self):
        counter = APICallCounter()
        self.assertEqual(
            counter.format_summary(),
            "api_calls total=0 github=0 uploads=0 other=0 graphql=0 by_role=none by_operation=none",
        )

    def test_cli_summary_emits_api_calls_line_and_preserves_existing_summary(self):
        output = io.StringIO()
        dst = Destination()
        dst.fail = False
        config = {
            "destinations": {"main": {"account_type": "organization"}},
            "repositories": [
                {"source": {"url": f"https://github.com/{SOURCE['owner']}/demo"}, "mirrors": {"main": {}}}
            ],
        }
        # Seed a stale count to prove cli.main resets metrics at the start of a run.
        github_api.record_api_call("api.github.com", "destination", "API", "GET")

        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as f:
            json.dump(config, f)
            f.flush()
            with (
                patch.dict(
                    os.environ,
                    {
                        "GH_TOKEN": "secret-token",
                        "DESTINATION_OWNER": "dest",
                        "MIRROR_TIMESTAMP_SECRET": "synthetic-test-only-secret",
                    },
                    clear=True,
                ),
                patch_everywhere("SourceAPI", return_value=Source()),
                patch_everywhere("API", return_value=dst),
                patch_everywhere("mirror_git"),
                contextlib.redirect_stderr(output),
            ):
                mirror.main(["--config", f.name])

        text = output.getvalue()
        self.assertIn("===== Summary =====", text)
        self.assertIn("summary sources=1 discovery_failed=0 repositories=1 succeeded=1 failed=0", text)
        self.assertIn(
            "api_calls total=0 github=0 uploads=0 other=0 graphql=0 by_role=none by_operation=none",
            text,
        )
        # Ensure the api_calls line is a single line immediately in the summary block.
        summary_lines = [line for line in text.splitlines() if line.startswith("api_calls ")]
        self.assertEqual(len(summary_lines), 1)
        self.assertIn("api_calls total=", summary_lines[0])
        self.assertIn("github=", summary_lines[0])
        self.assertNotIn("secret-token", text)
        self.assertNotIn("https://", text)


if __name__ == "__main__":
    unittest.main()
