import contextlib
import io
import json
import os
import socket
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
from repo_mirror import github_api, gitlab_assets
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
from repo_mirror.gitlab_source_adapter import GitLabSourceAdapter, fetch_group_projects


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
            {("github", "destination", "API", "GET"): 2},
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
                ("other", "source", "API", "GET"): 1,
                ("github", "destination", "API", "GET"): 1,
                ("github", "destination", "API", "PUT"): 1,
                ("uploads", "destination", "API", "POST"): 1,
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
                ("github", "destination", "asset", "GET"): 1,
                ("other", "destination", "asset-hop", "GET"): 1,
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
                ("other", "source", "asset-hop", "GET"): 2,
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
                ("github", "destination", "API", "GET"): 2,
                ("github", "destination", "API", "POST"): 1,
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
                ("github", "source", "asset", "GET"): 1,
                ("other", "source", "asset-hop", "GET"): 1,
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

    def test_gitlab_source_adapter_and_group_projects_counted_in_other(self):
        redirect_err = HTTPError(
            "https://gitlab.example.test/api/v4/projects/group%2Fdemo",
            302,
            "Found",
            {"Location": "https://gitlab.example.test/api/v4/projects/42"},
            None,
        )
        self.addCleanup(redirect_err.close)
        project_body = json.dumps(
            {
                "id": 42,
                "visibility": "public",
                "path_with_namespace": "group/demo",
                "http_url_to_repo": "https://gitlab.example.test/group/demo.git",
            }
        ).encode()

        class _GitLabHandler(HTTPSHandler):
            def __init__(self, items):
                super().__init__()
                self._items = list(items)

            def https_open(self, req):
                item = self._items.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        adapter = GitLabSourceAdapter(
            "https://gitlab.example.test",
            "group/demo",
            42,
            handlers=(
                _GitLabHandler(
                    [
                        redirect_err,
                        _StubResponse(
                            body=project_body,
                            url="https://gitlab.example.test/api/v4/projects/42",
                        ),
                    ]
                ),
            ),
        )
        project = adapter.project()
        self.assertEqual(project["id"], 42)

        projects = fetch_group_projects(
            "https://gitlab.example.test",
            "group",
            handlers=(
                _GitLabHandler(
                    [
                        _StubResponse(
                            body=b'[{"id": 1}]',
                            headers={"X-Next-Page": "2"},
                            url="https://gitlab.example.test/api/v4/groups/group/projects?page=1",
                        ),
                        _StubResponse(
                            body=b'[{"id": 2}]',
                            headers={"X-Next-Page": ""},
                            url="https://gitlab.example.test/api/v4/groups/group/projects?page=2",
                        ),
                    ]
                ),
            ),
        )
        self.assertEqual(len(projects), 2)

        self.assertEqual(
            api_call_metrics_snapshot(),
            {("other", "source", "API", "GET"): 4},
        )
        summary = format_api_call_summary()
        self.assertEqual(
            summary,
            "api_calls total=4 github=0 uploads=0 other=4 graphql=0 "
            "by_role=source:4 by_operation=api_get:4",
        )
        self.assertNotIn("gitlab.example.test", summary)

    def test_gitlab_assets_download_counts_redirects_and_429_retries(self):
        rate_limited = MagicMock(status=429)
        rate_limited.getheader.side_effect = lambda k, d=None: "0" if k == "Retry-After" else d
        redirect = MagicMock(status=302)
        redirect.getheader.side_effect = (
            lambda k, d=None: "https://cdn.example.test/releases/v1/app.tar.gz" if k == "Location" else d
        )
        ok = MagicMock(status=200)
        ok.getheader.side_effect = lambda k, d=None: "4" if k == "Content-Length" else d
        ok.read1.side_effect = [b"data", b""]

        conns = []
        for resp in (rate_limited, redirect, ok):
            conn = MagicMock()
            conn.getresponse.return_value = resp
            conns.append(conn)

        with (
            patch.object(
                gitlab_assets.socket,
                "getaddrinfo",
                return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
            ),
            patch.object(gitlab_assets.socket, "socket"),
            patch.object(gitlab_assets.ssl, "create_default_context"),
            patch.object(gitlab_assets.http.client, "HTTPSConnection", side_effect=conns),
            patch.object(gitlab_assets.time, "sleep"),
        ):
            with gitlab_assets.download_asset(
                "https://gitlab.example.test/group/demo/uploads/app.tar.gz",
                max_bytes=1024,
            ) as dl:
                self.assertFalse(dl.oversize)
                self.assertEqual(dl.file.read(), b"data")

        self.assertEqual(
            api_call_metrics_snapshot(),
            {("other", "source", "asset", "GET"): 3},
        )
        summary = format_api_call_summary()
        self.assertIn("api_calls total=3 github=0 uploads=0 other=3 ", summary)
        self.assertNotIn("gitlab.example.test", summary)
        self.assertNotIn("cdn.example.test", summary)

    def test_loopback_hosts_are_excluded(self):
        counter = APICallCounter()
        for host in ("127.0.0.1", "::1", "[::1]", "localhost", "LOCALHOST", "localhost.", "127.0.1.1"):
            counter.record(host, "destination", "API", "GET")
        self.assertEqual(counter.snapshot(), {})
        self.assertEqual(
            counter.format_summary(),
            "api_calls total=0 github=0 uploads=0 other=0 graphql=0 by_role=none by_operation=none",
        )
        counter.record("gitlab.example.test", "source", "API", "GET")
        self.assertEqual(counter.snapshot(), {("other", "source", "API", "GET"): 1})

    def test_bounded_key_space_for_many_distinct_hostnames(self):
        counter = APICallCounter()
        for i in range(10000):
            counter.record(f"cdn-{i}.example.test", "source", "asset-hop", "GET")
        snapshot = counter.snapshot()
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot, {("other", "source", "asset-hop", "GET"): 10000})
        summary = counter.format_summary()
        self.assertEqual(
            summary,
            "api_calls total=10000 github=0 uploads=0 other=10000 graphql=0 "
            "by_role=source:10000 by_operation=asset_hop_get:10000",
        )

    def test_app_token_provider_mint_respects_source_role(self):
        provider = AppTokenProvider("12345", "synthetic-key", "srcowner", "personal", role="source")
        install_resp = _StubResponse(json.dumps({"id": 7, "account": {"login": "srcowner"}}).encode())
        token_resp = _StubResponse(
            json.dumps({"token": "ghs_src", "expires_at": "2099-01-01T00:00:00Z"}).encode()
        )
        fake_opener = MagicMock()
        fake_opener.open.side_effect = [install_resp, token_resp]

        with (
            patch.object(github_api, "build_opener", return_value=fake_opener),
            patch.object(
                github_api.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout=b"sig"),
            ),
        ):
            token = provider()

        self.assertEqual(token, "ghs_src")
        self.assertEqual(
            api_call_metrics_snapshot(),
            {
                ("github", "source", "API", "GET"): 1,
                ("github", "source", "API", "POST"): 1,
            },
        )

    def test_unauthenticated_download_counts_eai_again_dns_failure(self):
        src = API()
        ok_resp = MagicMock(status=200)
        ok_resp.getheader.side_effect = lambda k, d=None: "2" if k == "Content-Length" else d
        ok_resp.read.side_effect = [b"ok", b""]
        conn = MagicMock()
        conn.getresponse.return_value = ok_resp

        output = io.BytesIO()
        with (
            patch.object(
                github_api.socket,
                "getaddrinfo",
                side_effect=[
                    socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"),
                    [(2, 1, 6, "", ("8.8.8.8", 443))],
                ],
            ),
            patch.object(github_api.socket, "socket"),
            patch.object(github_api.ssl, "create_default_context"),
            patch.object(github_api.http.client, "HTTPSConnection", return_value=conn),
            patch.object(github_api.time, "sleep"),
        ):
            src.download("https://forge.example.test/alice/demo/releases/download/v1/file.bin", output)

        self.assertEqual(output.getvalue(), b"ok")
        self.assertEqual(
            api_call_metrics_snapshot(),
            {("other", "source", "asset-hop", "GET"): 2},
        )

    def test_graphql_bucket_overlaps_github(self):
        counter = APICallCounter()
        counter.record("api.github.com", "source", "graphql", "POST")
        self.assertEqual(
            counter.format_summary(),
            "api_calls total=1 github=1 uploads=0 other=0 graphql=1 "
            "by_role=source:1 by_operation=graphql_post:1",
        )

    def test_cli_emits_api_calls_once_on_early_failure(self):
        output = io.StringIO()
        config = {
            "destinations": {"main": {"account_type": "organization"}},
            "repositories": [
                {"source": {"url": f"https://github.com/{SOURCE['owner']}/demo"}, "mirrors": {"main": {}}}
            ],
        }

        def fail_early(*args, **kwargs):
            github_api.record_api_call("api.github.com", "destination", "API", "GET")
            github_api.record_api_call("gitlab.example.test", "source", "API", "GET")
            raise ValueError("early failure")

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
                patch_everywhere("API", return_value=Destination()),
                patch("repo_mirror.cli.validate_source", side_effect=fail_early),
                contextlib.redirect_stderr(output),
            ):
                with self.assertRaisesRegex(ValueError, "early failure"):
                    mirror.main(["--config", f.name])

        text = output.getvalue()
        summary_lines = [line for line in text.splitlines() if line.startswith("api_calls ")]
        self.assertEqual(len(summary_lines), 1)
        self.assertEqual(
            summary_lines[0],
            "api_calls total=2 github=1 uploads=0 other=1 graphql=0 "
            "by_role=destination:1,source:1 by_operation=api_get:2",
        )
        self.assertIn("runtime total elapsed=", text)

    def test_cli_emits_api_calls_once_on_worker_base_exception(self):
        output = io.StringIO()
        dst = Destination()
        dst.fail = False
        config = {
            "destinations": {"main": {"account_type": "organization"}},
            "repositories": [
                {"source": {"url": f"https://github.com/{SOURCE['owner']}/demo"}, "mirrors": {"main": {}}}
            ],
        }

        def crash_worker(*args, **kwargs):
            github_api.record_api_call("api.github.com", "source", "API", "GET")
            raise KeyboardInterrupt("aborted")

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
                patch("repo_mirror.cli.mirror_repository", side_effect=crash_worker),
                contextlib.redirect_stderr(output),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    mirror.main(["--config", f.name])

        text = output.getvalue()
        summary_lines = [line for line in text.splitlines() if line.startswith("api_calls ")]
        self.assertEqual(len(summary_lines), 1)
        self.assertEqual(
            summary_lines[0],
            "api_calls total=1 github=1 uploads=0 other=0 graphql=0 "
            "by_role=source:1 by_operation=api_get:1",
        )
        self.assertIn("runtime total elapsed=", text)

    def test_cli_emits_api_calls_once_on_caught_repo_failure(self):
        output = io.StringIO()
        dst = Destination()
        dst.fail = False
        config = {
            "destinations": {"main": {"account_type": "organization"}},
            "repositories": [
                {"source": {"url": f"https://github.com/{SOURCE['owner']}/demo"}, "mirrors": {"main": {}}}
            ],
        }

        def fail_repo(*args, **kwargs):
            github_api.record_api_call("api.github.com", "source", "API", "GET")
            raise RuntimeError("boom")

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
                patch("repo_mirror.cli.mirror_repository", side_effect=fail_repo),
                contextlib.redirect_stderr(output),
            ):
                with self.assertRaisesRegex(RuntimeError, "1 repositories failed"):
                    mirror.main(["--config", f.name])

        text = output.getvalue()
        summary_lines = [line for line in text.splitlines() if line.startswith("api_calls ")]
        self.assertEqual(len(summary_lines), 1)
        self.assertIn("===== Summary =====", text)
        self.assertEqual(
            summary_lines[0],
            "api_calls total=1 github=1 uploads=0 other=0 graphql=0 "
            "by_role=source:1 by_operation=api_get:1",
        )


if __name__ == "__main__":
    unittest.main()
