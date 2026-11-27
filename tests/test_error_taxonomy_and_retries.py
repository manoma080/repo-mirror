"""Tests for PR 1: git-phase error taxonomy, allowlist, and retry accounting."""

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from email.message import Message
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.response import addinfourl

from mirror_test_support import patch_everywhere

import mirror
from repo_mirror import snapshot_push
from repo_mirror.forgejo_destination_adapter import ForgejoDestinationAdapter
from repo_mirror.forgejo_releases import ForgejoReleases
from repo_mirror.forgejo_source_adapter import ForgejoSourceAdapter
from repo_mirror.github_api import API, AppTokenProvider, SourceAPI, _UPLOAD_ATTEMPT_CALLBACK, _cooldowns
from repo_mirror.logging_utils import error_detail
from repo_mirror.repository import _IdentityGuardedDestination, mirror_releases, upload_with_reconciliation


def _fire_upload_attempt():
    callback = _UPLOAD_ATTEMPT_CALLBACK.get()
    if callback is not None:
        callback()


def _http_error(code, headers=None, body=b"", url="https://api.github.com/repos/dest/demo"):
    return HTTPError(url, code, "secret-reason", headers or {}, io.BytesIO(body))


class _JsonResponse:
    def __init__(self, body=b"{}", headers=None):
        self._body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self._body if size is None or size < 0 else self._body[:size]


class GitPhaseErrorTaxonomyTests(unittest.TestCase):
    def test_http_403_rate_limit_in_default_switch_preserves_operation_and_suffix(self):
        err = _http_error(
            403,
            headers={"X-RateLimit-Remaining": "0"},
            url="https://user:secret@api.github.com/repos/dest/demo?token=secret",
        )
        err.mirror_operation = ("destination", "API", "GET")
        with self.assertRaises(HTTPError) as caught, snapshot_push.git_phase("default switch"):
            raise err
        detail = error_detail(caught.exception)
        self.assertEqual(
            detail,
            "git default switch: HTTP 403 (destination API GET; rate limit remaining=0)",
        )
        self.assertNotIn("secret", detail)
        self.assertNotIn("https://", detail)
        err.close()

    def test_http_error_in_default_switch_without_rate_limit_or_operation(self):
        err_with_op = _http_error(403, headers={"X-RateLimit-Remaining": "10"})
        err_with_op.mirror_operation = ("destination", "API", "PATCH")
        with self.assertRaises(HTTPError) as caught, snapshot_push.git_phase("default switch"):
            raise err_with_op
        self.assertEqual(error_detail(caught.exception), "git default switch: HTTP 403 (destination API PATCH)")
        err_with_op.close()

        err_without_op = _http_error(403, headers={"X-RateLimit-Remaining": "0"})
        with self.assertRaises(HTTPError) as caught, snapshot_push.git_phase("default switch"):
            raise err_without_op
        self.assertEqual(error_detail(caught.exception), "git default switch: HTTP 403")
        err_without_op.close()

        err_bad_op = _http_error(403, headers={"X-RateLimit-Remaining": "0"})
        err_bad_op.mirror_operation = ("untrusted", "API", "GET")
        with self.assertRaises(HTTPError) as caught, snapshot_push.git_phase("default switch"):
            raise err_bad_op
        self.assertEqual(error_detail(caught.exception), "git default switch: HTTP 403")
        err_bad_op.close()

    def test_cooldown_runtime_error_in_default_switch_preserves_safe_message(self):
        message = "already rate limited by the source or destination host; the 60 s wait limit is exhausted"
        with self.assertRaises(RuntimeError) as caught, snapshot_push.git_phase("default switch"):
            raise RuntimeError(message)
        self.assertEqual(error_detail(caught.exception), f"git default switch: {message}")

    def test_retry_wait_limit_exceeded_survives_error_detail_and_git_phase(self):
        self.assertEqual(
            error_detail(RuntimeError("retry wait limit exceeded")),
            "retry wait limit exceeded",
        )
        self.assertEqual(
            error_detail(RuntimeError("retry wait limit exceeded extra")),
            "RuntimeError",
        )
        with self.assertRaises(RuntimeError) as caught, snapshot_push.git_phase("default switch"):
            raise RuntimeError("retry wait limit exceeded")
        self.assertEqual(
            error_detail(caught.exception),
            "git default switch: retry wait limit exceeded",
        )

    def test_unrecognised_exception_in_git_phase_keeps_generic_fallback(self):
        for exc in (
            RuntimeError("https://private.example/secret-token"),
            ValueError("already rate limited by the source or destination host; the 60 s wait limit is exhausted"),
            OSError("secret-os-error"),
            HTTPError("https://private.example", "bad-code", "secret", {}, None),
        ):
            with self.subTest(exc=type(exc).__name__):
                with self.assertRaises(type(exc)) as caught, snapshot_push.git_phase("default switch"):
                    raise exc
                detail = error_detail(caught.exception)
                self.assertEqual(
                    detail,
                    "git default switch: operation failed; inspect safely on a trusted system",
                )
                self.assertNotIn("secret", detail)
                self.assertNotIn("private.example", detail)
                if isinstance(exc, HTTPError):
                    exc.close()


class RetryAccountingTests(unittest.TestCase):
    def setUp(self):
        _cooldowns.clear()
        self.addCleanup(_cooldowns.clear)
        secret = patch.dict("os.environ", {"MIRROR_TIMESTAMP_SECRET": "synthetic-test-only-secret"})
        secret.start()
        self.addCleanup(secret.stop)

    def test_api_request_retry_cancelled_by_wait_budget_logs_nothing_for_cancelled_attempt(self):
        api = API()
        clock = [100.0]
        stderr = io.StringIO()
        with (
            patch.object(
                api.opener,
                "open",
                side_effect=[_http_error(429, {"Retry-After": "59"}), _http_error(500)],
            ) as opening,
            patch("repo_mirror.github_api.time.monotonic", side_effect=lambda: clock[0]),
            patch(
                "repo_mirror.github_api.time.sleep",
                side_effect=lambda n: clock.__setitem__(0, clock[0] + n),
            ),
            redirect_stderr(stderr),
            self.assertRaises(RuntimeError) as caught,
        ):
            api.request("GET", "https://api.github.com/test")

        self.assertEqual(error_detail(caught.exception), "retry wait limit exceeded")
        self.assertEqual(opening.call_count, 2)
        output = stderr.getvalue()
        self.assertEqual(output.count("request retry 1/3"), 1)
        self.assertNotIn("request retry 2/3", output)

    def test_api_request_retry_cancelled_by_cooldown_during_sleep_logs_no_retry(self):
        api = API()
        clock = [100.0]
        stderr = io.StringIO()

        def sleep_and_trip_cooldown(seconds):
            clock[0] += seconds
            _cooldowns["api.github.com"] = clock[0] + 120

        with (
            patch.object(api.opener, "open", side_effect=[_http_error(500), _JsonResponse()]) as opening,
            patch("repo_mirror.github_api.time.monotonic", side_effect=lambda: clock[0]),
            patch("repo_mirror.github_api.time.sleep", side_effect=sleep_and_trip_cooldown),
            redirect_stderr(stderr),
            self.assertRaises(RuntimeError) as caught,
        ):
            api.request("GET", "https://api.github.com/test")

        self.assertEqual(
            error_detail(caught.exception),
            "already rate limited by the source or destination host; the 60 s wait limit is exhausted",
        )
        self.assertEqual(opening.call_count, 1)
        self.assertNotIn("request retry", stderr.getvalue())

    def test_api_request_retry_cancelled_by_cumulative_cooldown_logs_only_attempted_retry(self):
        api = API()
        clock = [100.0]
        stderr = io.StringIO()
        with (
            patch.object(
                api.opener,
                "open",
                side_effect=[
                    _http_error(429, {"Retry-After": "40"}),
                    _http_error(429, {"Retry-After": "40"}),
                    _JsonResponse(),
                ],
            ) as opening,
            patch("repo_mirror.github_api.time.monotonic", side_effect=lambda: clock[0]),
            patch(
                "repo_mirror.github_api.time.sleep",
                side_effect=lambda n: clock.__setitem__(0, clock[0] + n),
            ),
            redirect_stderr(stderr),
            self.assertRaises(RuntimeError),
        ):
            api.request("GET", "https://api.github.com/test")

        self.assertEqual(opening.call_count, 2)
        output = stderr.getvalue()
        self.assertEqual(output.count("request retry 1/3"), 1)
        self.assertNotIn("request retry 2/3", output)

    def test_api_request_single_attempted_retry_logs_once(self):
        api = API()
        stderr = io.StringIO()
        with (
            patch.object(api.opener, "open", side_effect=[_http_error(500), _JsonResponse()]) as opening,
            patch("repo_mirror.github_api.time.sleep"),
            redirect_stderr(stderr),
        ):
            self.assertEqual(api.request("GET", "https://api.github.com/test"), {})
        self.assertEqual(opening.call_count, 2)
        output = stderr.getvalue()
        self.assertEqual(output.count("request retry 1/3"), 1)
        self.assertNotIn("request retry 2/3", output)

    def test_app_token_mint_retry_cancelled_by_wait_budget_logs_nothing_for_cancelled_attempt(self):
        provider = AppTokenProvider("1", "key", "alice")
        clock = [100.0]
        stderr = io.StringIO()
        with (
            patch("repo_mirror.github_api.subprocess.run", return_value=SimpleNamespace(stdout=b"sig")),
            patch("repo_mirror.github_api.build_opener") as build_opener,
            patch("repo_mirror.github_api.time.monotonic", side_effect=lambda: clock[0]),
            patch(
                "repo_mirror.github_api.time.sleep",
                side_effect=lambda n: clock.__setitem__(0, clock[0] + n),
            ),
            redirect_stderr(stderr),
            self.assertRaises(RuntimeError) as caught,
        ):
            build_opener.return_value.open.side_effect = [
                _http_error(429, {"Retry-After": "59"}),
                _http_error(500),
            ]
            provider._mint()

        self.assertEqual(error_detail(caught.exception), "retry wait limit exceeded")
        self.assertEqual(build_opener.return_value.open.call_count, 2)
        output = stderr.getvalue()
        self.assertEqual(output.count("app authentication retry 1/3"), 1)
        self.assertNotIn("app authentication retry 2/3", output)

    def test_app_token_mint_single_attempted_retry_logs_once(self):
        provider = AppTokenProvider("1", "key", "alice")
        stderr = io.StringIO()
        installation = _JsonResponse(b'{"id": 42, "account": {"login": "alice"}}')
        token = _JsonResponse(b'{"token": "tok", "expires_at": "2099-01-01T00:00:00Z"}')
        with (
            patch("repo_mirror.github_api.subprocess.run", return_value=SimpleNamespace(stdout=b"sig")),
            patch("repo_mirror.github_api.build_opener") as build_opener,
            patch("repo_mirror.github_api.time.sleep"),
            redirect_stderr(stderr),
        ):
            build_opener.return_value.open.side_effect = [_http_error(500), installation, token]
            minted, _ = provider._mint()
        self.assertEqual(minted, "tok")
        output = stderr.getvalue()
        self.assertEqual(output.count("app authentication retry 1/3"), 1)
        self.assertNotIn("app authentication retry 2/3", output)

    def test_upload_with_reconciliation_retry_cancelled_by_cooldown_logs_no_retry(self):
        api = API("token")
        calls = []
        stderr = io.StringIO()

        def respond(handler, request):
            calls.append(request.data.read())
            headers = Message()
            headers["Retry-After"] = "0"
            response = addinfourl(io.BytesIO(b""), headers, request.full_url, 429)
            response.msg = "Too Many Requests"
            return response

        def sleep_and_trip_cooldown(seconds):
            _cooldowns["uploads.github.com"] = time.monotonic() + 120

        with (
            patch("urllib.request.HTTPSHandler.https_open", respond),
            patch.object(api, "pages", return_value=[]),
            patch("repo_mirror.repository.time.sleep", side_effect=sleep_and_trip_cooldown),
            redirect_stderr(stderr),
            self.assertRaises(RuntimeError) as caught,
        ):
            upload_with_reconciliation(
                api,
                "https://uploads.github.com/asset",
                io.BytesIO(b"data"),
                "application/octet-stream",
                "https://api.github.com/repos/dest/demo/releases/1/assets",
                "demo.bin",
            )

        self.assertEqual(
            error_detail(caught.exception),
            "already rate limited by the source or destination host; the 60 s wait limit is exhausted",
        )
        self.assertEqual(calls, [b"data"])
        self.assertNotIn("asset upload retry", stderr.getvalue())

    def test_upload_with_reconciliation_attempted_retry_logs_once(self):
        api = API("token")
        calls = []
        stderr = io.StringIO()

        def respond(handler, request):
            calls.append(request.data.read())
            headers = Message()
            headers["Retry-After"] = "0"
            code = 429 if len(calls) == 1 else 200
            response = addinfourl(io.BytesIO(b""), headers, request.full_url, code)
            response.msg = "response"
            return response

        with (
            patch("urllib.request.HTTPSHandler.https_open", respond),
            patch.object(api, "pages", return_value=[]),
            patch("repo_mirror.repository.time.sleep"),
            redirect_stderr(stderr),
        ):
            upload_with_reconciliation(
                api,
                "https://uploads.github.com/asset",
                io.BytesIO(b"data"),
                "application/octet-stream",
                "https://api.github.com/repos/dest/demo/releases/1/assets",
                "demo.bin",
            )

        self.assertEqual(calls, [b"data", b"data"])
        output = stderr.getvalue()
        self.assertEqual(output.count("asset upload retry 1/3"), 1)
        self.assertNotIn("asset upload retry 2/3", output)

    def test_journaled_asset_refused_by_cooldown_does_not_increment_retried(self):
        source = {"type": "github", "base_url": "https://github.com", "owner": "alice"}

        class SingleAssetSource:
            def request(self, *args):
                if args[1].endswith("/releases/latest"):
                    raise _http_error(404, url=args[1])
                if "/releases/" in args[1] and args[1].rsplit("/", 1)[-1].isdigit():
                    return {"id": 1, "tag_name": "v1", "name": "v1", "body": "notes"}
                return {"clone_url": "https://github.com/alice/demo.git"}

            def pages(self, url):
                if url.endswith("/releases"):
                    return [{"id": 1, "tag_name": "v1", "name": "v1", "body": "notes"}]
                return [{"name": "new", "size": 1, "browser_download_url": "https://github.com/new"}]

            def download(self, url, out):
                out.write(b"n")

        class RealUploadDestination(API):
            def __init__(self):
                super().__init__("token")
                self.release = {
                    "id": 2,
                    "name": "v1",
                    "body": "notes",
                    "upload_url": "https://uploads.github.com/x{?name}",
                }

            def request(self, method, url, data=None, **kwargs):
                if url.endswith("/repos/dest/mirror-alice-demo"):
                    return {"description": mirror.source_marker(source, "demo")}
                if "/releases/tags/" in url:
                    return self.release.copy()
                if method == "PATCH" and url.endswith("/releases/2"):
                    self.release.update(data)
                    return self.release.copy()
                return {}

            def pages(self, url):
                return []

        dst = RealUploadDestination()
        _cooldowns["uploads.github.com"] = time.monotonic() + 120
        stderr = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch_everywhere("mirror_git"),
            patch.object(dst.opener, "open") as opening,
            redirect_stderr(stderr),
        ):
            journal = mirror.FailureJournal(os.path.join(directory, "journal"))
            journal.set(journal.key(source, "demo", "dest", "v1", "new"), True)
            with self.assertRaises(RuntimeError):
                mirror.mirror_repository(
                    source, "demo", "dest", SingleAssetSource(), dst, "token", journal=journal
                )

        opening.assert_not_called()
        output = stderr.getvalue()
        self.assertIn("assets uploaded=0 updated=0 skipped=0 retried=0 upload_failed=1", output)

    def test_journaled_asset_attempted_then_failed_increments_retried_once(self):
        source = {"type": "github", "base_url": "https://github.com", "owner": "alice"}

        class SingleAssetSource:
            def request(self, *args):
                if args[1].endswith("/releases/latest"):
                    raise _http_error(404, url=args[1])
                if "/releases/" in args[1] and args[1].rsplit("/", 1)[-1].isdigit():
                    return {"id": 1, "tag_name": "v1", "name": "v1", "body": "notes"}
                return {"clone_url": "https://github.com/alice/demo.git"}

            def pages(self, url):
                if url.endswith("/releases"):
                    return [{"id": 1, "tag_name": "v1", "name": "v1", "body": "notes"}]
                return [{"name": "new", "size": 1, "browser_download_url": "https://github.com/new"}]

            def download(self, url, out):
                out.write(b"n")

        class RealUploadDestination(API):
            def __init__(self):
                super().__init__("token")
                self.release = {
                    "id": 2,
                    "name": "v1",
                    "body": "notes",
                    "upload_url": "https://uploads.github.com/x{?name}",
                }

            def request(self, method, url, data=None, **kwargs):
                if url.endswith("/repos/dest/mirror-alice-demo"):
                    return {"description": mirror.source_marker(source, "demo")}
                if "/releases/tags/" in url:
                    return self.release.copy()
                if method == "PATCH" and url.endswith("/releases/2"):
                    self.release.update(data)
                    return self.release.copy()
                return {}

            def pages(self, url):
                return []

        dst = RealUploadDestination()
        stderr = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch_everywhere("mirror_git"),
            patch.object(
                dst.opener,
                "open",
                side_effect=_http_error(502, url="https://uploads.github.com/x?name=new"),
            ) as opening,
            redirect_stderr(stderr),
        ):
            journal = mirror.FailureJournal(os.path.join(directory, "journal"))
            journal.set(journal.key(source, "demo", "dest", "v1", "new"), True)
            with self.assertRaises(RuntimeError):
                mirror.mirror_repository(
                    source, "demo", "dest", SingleAssetSource(), dst, "token", journal=journal
                )

        self.assertEqual(opening.call_count, 1)
        output = stderr.getvalue()
        self.assertIn("assets uploaded=0 updated=0 skipped=0 retried=1 upload_failed=1", output)

    def test_forgejo_adapter_source_cooldown_before_upload_does_not_count_or_log_retry(self):
        source = {"type": "github", "base_url": "https://github.com", "owner": "alice"}
        upstream = "https://api.github.com/repos/alice/demo"
        src = SourceAPI("src-token")
        assets_fetch_count = [0]

        def open_source(request, timeout=60):
            url = request.full_url
            if url.endswith("/releases/latest"):
                raise _http_error(404, url=url)
            if "/releases/1/assets" in url:
                if not url.endswith("&page=1"):
                    headers = {}
                    if assets_fetch_count[0] >= 2:
                        headers = {
                            "X-RateLimit-Remaining": "0",
                            "X-RateLimit-Reset": str(int(time.time()) + 1800),
                        }
                    return _JsonResponse(b"[]", headers=headers)
                assets_fetch_count[0] += 1
                return _JsonResponse(
                    b'[{"id": 11, "name": "demo.bin", "size": 4, "browser_download_url": "https://github.com/alice/demo/releases/download/v1/demo.bin"}]'
                )
            if "/releases?" in url or url.endswith("/releases"):
                if url.endswith("&page=1"):
                    return _JsonResponse(b'[{"id": 1, "tag_name": "v1", "name": "v1", "body": "notes"}]')
                return _JsonResponse(b"[]")
            if url.endswith("/releases/1"):
                return _JsonResponse(b'{"id": 1, "tag_name": "v1", "name": "v1", "body": "notes"}')
            if url == upstream:
                return _JsonResponse(b'{"id": 123, "clone_url": "https://github.com/alice/demo.git"}')
            return _JsonResponse(b"{}")

        release_state = {"id": 2, "tag_name": "v1", "name": "v1", "body": "notes", "draft": False}

        def client_json(method, path, body=None):
            if method == "PATCH" and isinstance(body, dict):
                release_state.update(body)
            return dict(release_state)

        fclient = MagicMock()
        fclient.origin = "http://127.0.0.1:3000"
        fclient.base = "/api/v1/repos/dest/demo"
        fclient.list_releases.side_effect = lambda: [dict(release_state)]
        fclient.list_assets.return_value = []
        fclient._json.side_effect = client_json

        def guard():
            src.request("GET", upstream)

        adapter = ForgejoDestinationAdapter("dest/demo", "local-token", client=fclient, guard=guard)
        stderr = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(src, "restrict_origin"),
            patch.object(src.opener, "open", side_effect=open_source),
            patch("repo_mirror.forgejo_source_adapter.ForgejoSourceAdapter.download", side_effect=lambda url, out: out.write(b"data")),
            redirect_stderr(stderr),
        ):
            journal = mirror.FailureJournal(os.path.join(directory, "journal"))
            journal.set(journal.key(source, "demo", "dest", "v1", "demo.bin"), True)
            with self.assertRaises(RuntimeError):
                mirror_releases(
                    source,
                    "demo",
                    "dest",
                    src,
                    adapter,
                    upstream,
                    adapter.target,
                    mirror.source_marker(source, "demo"),
                    initializing=False,
                    journal=journal,
                )

        self.assertEqual(fclient.upload.call_count, 0)
        output = stderr.getvalue()
        self.assertIn("assets uploaded=0 updated=0 skipped=0 retried=0 upload_failed=1", output)
        self.assertNotIn("asset upload retry", output)

    def test_forgejo_adapter_attempted_upload_retry_counts_and_logs_once(self):
        source = {"type": "github", "base_url": "https://github.com", "owner": "alice"}
        upstream = "https://api.github.com/repos/alice/demo"
        src = SourceAPI("src-token")

        def open_source(request, timeout=60):
            url = request.full_url
            if url.endswith("/releases/latest"):
                raise _http_error(404, url=url)
            if "/releases/1/assets" in url:
                if not url.endswith("&page=1"):
                    return _JsonResponse(b"[]")
                return _JsonResponse(
                    b'[{"id": 11, "name": "demo.bin", "size": 4, "browser_download_url": "https://github.com/alice/demo/releases/download/v1/demo.bin"}]'
                )
            if "/releases?" in url or url.endswith("/releases"):
                if url.endswith("&page=1"):
                    return _JsonResponse(b'[{"id": 1, "tag_name": "v1", "name": "v1", "body": "notes"}]')
                return _JsonResponse(b"[]")
            if url.endswith("/releases/1"):
                return _JsonResponse(b'{"id": 1, "tag_name": "v1", "name": "v1", "body": "notes"}')
            if url == upstream:
                return _JsonResponse(b'{"id": 123, "clone_url": "https://github.com/alice/demo.git"}')
            return _JsonResponse(b"{}")

        release_state = {"id": 2, "tag_name": "v1", "name": "v1", "body": "notes", "draft": False}

        def client_json(method, path, body=None):
            if method == "PATCH" and isinstance(body, dict):
                release_state.update(body)
            return dict(release_state)

        uploaded_assets = []
        fclient = MagicMock()
        fclient.origin = "http://127.0.0.1:3000"
        fclient.base = "/api/v1/repos/dest/demo"
        fclient.list_releases.side_effect = lambda: [dict(release_state)]
        fclient.list_assets.side_effect = lambda rel: [dict(item) for item in uploaded_assets]
        fclient.download.side_effect = lambda asset, out: out.write(b"data")
        fclient._json.side_effect = client_json
        direct_429 = _http_error(429, {"Retry-After": "0"}, url="http://127.0.0.1:3000/upload")
        direct_429.direct_upload_rejection = True
        upload_calls = [0]

        def upload_side_effect(release, name, data, size, content_type):
            upload_calls[0] += 1
            if upload_calls[0] == 1:
                raise direct_429
            asset = {"id": 10, "name": name, "size": size}
            uploaded_assets.append(asset)
            return dict(asset)

        fclient.upload.side_effect = upload_side_effect

        adapter = ForgejoDestinationAdapter("dest/demo", "local-token", client=fclient, guard=lambda: None)
        stderr = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(src, "restrict_origin"),
            patch.object(src.opener, "open", side_effect=open_source),
            patch("repo_mirror.forgejo_source_adapter.ForgejoSourceAdapter.download", side_effect=lambda url, out: out.write(b"data")),
            patch("repo_mirror.repository.time.sleep"),
            redirect_stderr(stderr),
        ):
            journal = mirror.FailureJournal(os.path.join(directory, "journal"))
            journal.set(journal.key(source, "demo", "dest", "v1", "demo.bin"), True)
            mirror_releases(
                source,
                "demo",
                "dest",
                src,
                adapter,
                upstream,
                adapter.target,
                mirror.source_marker(source, "demo"),
                initializing=False,
                journal=journal,
            )

        self.assertEqual(fclient.upload.call_count, 2)
        output = stderr.getvalue()
        self.assertEqual(output.count("asset upload retry 1/3"), 1)
        self.assertNotIn("asset upload retry 2/3", output)
        self.assertIn("assets uploaded=1 updated=0 skipped=0 retried=1 upload_failed=0", output)

    def test_identity_guarded_mock_destination_refused_before_retry_logs_no_retry(self):
        direct_429 = _http_error(429, {"Retry-After": "0"}, url="https://uploads.github.com/asset")
        direct_429.direct_upload_rejection = True
        raw_mock = MagicMock()
        raw_mock.pages.return_value = []
        upload_calls = [0]

        def mock_upload(url, data, content_type):
            _fire_upload_attempt()
            upload_calls[0] += 1
            if upload_calls[0] == 1:
                raise direct_429
            return None

        raw_mock.upload.side_effect = mock_upload
        check_calls = [0]

        def check_pins():
            check_calls[0] += 1
            if check_calls[0] >= 2:
                raise RuntimeError(
                    "already rate limited by the source or destination host; the 60 s wait limit is exhausted"
                )

        dst = _IdentityGuardedDestination(raw_mock, check_pins)
        attempts = [0]
        stderr = io.StringIO()
        with (
            patch("repo_mirror.repository.time.sleep"),
            redirect_stderr(stderr),
            self.assertRaises(RuntimeError) as caught,
        ):
            upload_with_reconciliation(
                dst,
                "https://uploads.github.com/asset",
                io.BytesIO(b"data"),
                "application/octet-stream",
                "https://api.github.com/repos/dest/demo/releases/1/assets",
                "demo.bin",
                on_attempt=lambda: attempts.__setitem__(0, attempts[0] + 1),
            )

        self.assertEqual(
            error_detail(caught.exception),
            "already rate limited by the source or destination host; the 60 s wait limit is exhausted",
        )
        self.assertEqual(raw_mock.upload.call_count, 1)
        self.assertEqual(attempts[0], 1)
        self.assertNotIn("asset upload retry", stderr.getvalue())

    def test_identity_guarded_mock_destination_attempted_retry_counts_and_logs_once(self):
        direct_429 = _http_error(429, {"Retry-After": "0"}, url="https://uploads.github.com/asset")
        direct_429.direct_upload_rejection = True
        raw_mock = MagicMock()
        raw_mock.pages.return_value = []
        upload_calls = [0]

        def mock_upload(url, data, content_type):
            _fire_upload_attempt()
            upload_calls[0] += 1
            if upload_calls[0] == 1:
                raise direct_429
            return None

        raw_mock.upload.side_effect = mock_upload

        dst = _IdentityGuardedDestination(raw_mock, lambda: None)
        attempts = [0]
        stderr = io.StringIO()
        with (
            patch("repo_mirror.repository.time.sleep"),
            redirect_stderr(stderr),
        ):
            upload_with_reconciliation(
                dst,
                "https://uploads.github.com/asset",
                io.BytesIO(b"data"),
                "application/octet-stream",
                "https://api.github.com/repos/dest/demo/releases/1/assets",
                "demo.bin",
                on_attempt=lambda: attempts.__setitem__(0, attempts[0] + 1),
            )

        self.assertEqual(raw_mock.upload.call_count, 2)
        self.assertEqual(attempts[0], 2)
        output = stderr.getvalue()
        self.assertEqual(output.count("asset upload retry 1/3"), 1)
        self.assertNotIn("asset upload retry 2/3", output)

    def test_github_api_subclass_overriding_request_counts_and_logs_retry(self):
        class CustomReqAPI(API):
            def __init__(self):
                super().__init__("token")
                self.calls = 0

            def _request(self, method, url, body=None, headers=None, *, upload=False):
                if upload:
                    _fire_upload_attempt()
                self.calls += 1
                if self.calls == 1:
                    err = _http_error(429, {"Retry-After": "0"}, url=url)
                    err.direct_upload_rejection = True
                    raise err
                return b"{}"

        capi = CustomReqAPI()
        attempts = [0]
        stderr = io.StringIO()
        with (
            patch.object(capi, "pages", return_value=[]),
            patch("repo_mirror.repository.time.sleep"),
            redirect_stderr(stderr),
        ):
            upload_with_reconciliation(
                capi,
                "https://uploads.github.com/asset",
                io.BytesIO(b"data"),
                "application/octet-stream",
                "https://api.github.com/repos/dest/demo/releases/1/assets",
                "demo.bin",
                on_attempt=lambda: attempts.__setitem__(0, attempts[0] + 1),
            )

        self.assertEqual(capi.calls, 2)
        self.assertEqual(attempts[0], 2)
        output = stderr.getvalue()
        self.assertEqual(output.count("asset upload retry 1/3"), 1)
        self.assertNotIn("asset upload retry 2/3", output)

    def test_api_download_cooldown_refusal_on_retry_logs_no_retry(self):
        token_reads = [0]

        def expiring_token():
            token_reads[0] += 1
            if token_reads[0] <= 2:
                return "tok1"
            raise RuntimeError(
                "already rate limited by the source or destination host; the 60 s wait limit is exhausted"
            )

        dapi = API(expiring_token)
        stderr = io.StringIO()
        with (
            patch.object(
                dapi.opener,
                "open",
                side_effect=[_http_error(502, url="https://api.github.com/a")],
            ) as opening,
            patch("repo_mirror.github_api.time.sleep"),
            redirect_stderr(stderr),
            self.assertRaises(RuntimeError) as caught,
        ):
            dapi.download("https://api.github.com/a", io.BytesIO())

        self.assertEqual(
            error_detail(caught.exception),
            "already rate limited by the source or destination host; the 60 s wait limit is exhausted",
        )
        self.assertEqual(opening.call_count, 1)
        self.assertNotIn("asset download retry", stderr.getvalue())

    def test_api_download_attempted_retry_logs_once(self):
        dapi = API("tok1")
        out = io.BytesIO()
        stderr = io.StringIO()
        with (
            patch.object(
                dapi.opener,
                "open",
                side_effect=[
                    _http_error(502, url="https://api.github.com/a"),
                    addinfourl(io.BytesIO(b"ok"), Message(), "https://api.github.com/a", 200),
                ],
            ) as opening,
            patch("repo_mirror.github_api.time.sleep"),
            redirect_stderr(stderr),
        ):
            dapi.download("https://api.github.com/a", out)

        self.assertEqual(out.getvalue(), b"ok")
        self.assertEqual(opening.call_count, 2)
        output = stderr.getvalue()
        self.assertEqual(output.count("asset download retry 1/3 target=destination"), 1)
        self.assertNotIn("asset download retry 2/3", output)

    def test_forgejo_adapter_real_client_invalid_content_type_does_not_count_or_log_retry(self):
        client = ForgejoReleases("dest/demo", "local-token", port=3000)
        adapter = ForgejoDestinationAdapter("dest/demo", "local-token", client=client, guard=lambda: None)
        attempts = [0]
        stderr = io.StringIO()
        with (
            patch.object(client.opener, "open") as opening,
            redirect_stderr(stderr),
            self.assertRaises(ValueError),
        ):
            upload_with_reconciliation(
                adapter,
                adapter.target + "/releases/2/assets?name=demo.bin",
                io.BytesIO(b"data"),
                "text/plain; charset=utf-8",
                adapter.target + "/releases/2/assets",
                "demo.bin",
                on_attempt=lambda: attempts.__setitem__(0, attempts[0] + 1),
            )

        self.assertEqual(opening.call_count, 0)
        self.assertEqual(attempts[0], 0)
        self.assertNotIn("asset upload retry", stderr.getvalue())

        source = {"type": "github", "base_url": "https://github.com", "owner": "alice"}
        upstream = "https://api.github.com/repos/alice/demo"
        src = SourceAPI("src-token")

        def open_source(request, timeout=60):
            url = request.full_url
            if url.endswith("/releases/latest"):
                raise _http_error(404, url=url)
            if "/releases/1/assets" in url:
                if not url.endswith("&page=1"):
                    return _JsonResponse(b"[]")
                return _JsonResponse(
                    b'[{"id": 11, "name": "demo.bin", "size": 4, "content_type": "text/plain; charset=utf-8", '
                    b'"browser_download_url": "https://github.com/alice/demo/releases/download/v1/demo.bin"}]'
                )
            if "/releases?" in url or url.endswith("/releases"):
                if url.endswith("&page=1"):
                    return _JsonResponse(b'[{"id": 1, "tag_name": "v1", "name": "v1", "body": "notes"}]')
                return _JsonResponse(b"[]")
            if url.endswith("/releases/1"):
                return _JsonResponse(b'{"id": 1, "tag_name": "v1", "name": "v1", "body": "notes"}')
            if url == upstream:
                return _JsonResponse(b'{"id": 123, "clone_url": "https://github.com/alice/demo.git"}')
            return _JsonResponse(b"{}")

        release_state = {"id": 2, "tag_name": "v1", "name": "v1", "body": "notes", "draft": False}
        upload_open_calls = [0]

        def open_forgejo(request, timeout=60):
            url = request.full_url
            method = request.get_method()
            if method == "POST" and "/releases/2/assets" in url:
                upload_open_calls[0] += 1
                return _JsonResponse(b'{"id": 10, "name": "demo.bin", "size": 4}')
            if "/releases?limit=100&page=1" in url:
                return _JsonResponse(json.dumps([release_state]).encode())
            if "/releases?limit=100&page=" in url:
                return _JsonResponse(b"[]")
            if url.endswith("/releases/2/assets"):
                return _JsonResponse(b"[]")
            if method == "PATCH" and url.endswith("/releases/2"):
                release_state.update(json.loads(request.data.decode()))
                return _JsonResponse(json.dumps(release_state).encode())
            return _JsonResponse(json.dumps(release_state).encode())

        real_client = ForgejoReleases("dest/demo", "local-token", port=3000)
        real_adapter = ForgejoDestinationAdapter("dest/demo", "local-token", client=real_client, guard=lambda: None)
        mirror_stderr = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(src, "restrict_origin"),
            patch.object(src.opener, "open", side_effect=open_source),
            patch.object(real_client.opener, "open", side_effect=open_forgejo),
            patch("repo_mirror.forgejo_source_adapter.ForgejoSourceAdapter.download", side_effect=lambda url, out: out.write(b"data")),
            redirect_stderr(mirror_stderr),
        ):
            journal = mirror.FailureJournal(os.path.join(directory, "journal"))
            journal.set(journal.key(source, "demo", "dest", "v1", "demo.bin"), True)
            with self.assertRaises(RuntimeError):
                mirror_releases(
                    source,
                    "demo",
                    "dest",
                    src,
                    real_adapter,
                    upstream,
                    real_adapter.target,
                    mirror.source_marker(source, "demo"),
                    initializing=False,
                    journal=journal,
                )

        self.assertEqual(upload_open_calls[0], 0)
        output = mirror_stderr.getvalue()
        self.assertIn("assets uploaded=0 updated=0 skipped=0 retried=0 upload_failed=1", output)
        self.assertNotIn("asset upload retry", output)

    def test_forgejo_adapter_real_client_attempted_retry_counts_and_logs_once(self):
        client = ForgejoReleases("dest/demo", "local-token", port=3000)
        adapter = ForgejoDestinationAdapter("dest/demo", "local-token", client=client, guard=lambda: None)
        self.assertFalse(hasattr(adapter, "emits_attempt_callback"))
        direct_429 = _http_error(429, {"Retry-After": "0"}, url=adapter.target + "/releases/2/assets?name=demo.bin")
        direct_429.direct_upload_rejection = True
        upload_calls = [0]

        def open_forgejo(request, timeout=60):
            if request.get_method() == "POST":
                upload_calls[0] += 1
                if upload_calls[0] == 1:
                    raise direct_429
                return _JsonResponse(b'{"id": 10, "name": "demo.bin", "size": 4}')
            return _JsonResponse(b"[]")

        attempts = [0]
        stderr = io.StringIO()
        with (
            patch.object(client.opener, "open", side_effect=open_forgejo),
            patch("repo_mirror.repository.time.sleep"),
            redirect_stderr(stderr),
        ):
            upload_with_reconciliation(
                adapter,
                adapter.target + "/releases/2/assets?name=demo.bin",
                io.BytesIO(b"data"),
                "application/octet-stream",
                adapter.target + "/releases/2/assets",
                "demo.bin",
                on_attempt=lambda: attempts.__setitem__(0, attempts[0] + 1),
            )

        self.assertEqual(upload_calls[0], 2)
        self.assertEqual(attempts[0], 2)
        output = stderr.getvalue()
        self.assertEqual(output.count("asset upload retry 1/3"), 1)
        self.assertNotIn("asset upload retry 2/3", output)

    def test_forgejo_adapter_mock_client_invalid_content_type_does_not_count_attempt(self):
        fclient = MagicMock()
        fclient.origin = "http://127.0.0.1:3000"
        fclient.base = "/api/v1/repos/dest/demo"
        fclient.list_assets.return_value = []
        fclient.upload.return_value = {"id": 10, "name": "demo.bin", "size": 4}
        adapter = ForgejoDestinationAdapter("dest/demo", "local-token", client=fclient, guard=lambda: None)

        attempts = [0]
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(ValueError):
            upload_with_reconciliation(
                adapter,
                adapter.target + "/releases/2/assets?name=demo.bin",
                io.BytesIO(b"data"),
                "text/plain; charset=utf-8",
                adapter.target + "/releases/2/assets",
                "demo.bin",
                on_attempt=lambda: attempts.__setitem__(0, attempts[0] + 1),
            )

        self.assertEqual(fclient.upload.call_count, 0)
        self.assertEqual(attempts[0], 0)
        self.assertNotIn("asset upload retry", stderr.getvalue())

        # Positive control: valid content type issues upload and counts once.
        upload_with_reconciliation(
            adapter,
            adapter.target + "/releases/2/assets?name=demo.bin",
            io.BytesIO(b"data"),
            "application/octet-stream",
            adapter.target + "/releases/2/assets",
            "demo.bin",
            on_attempt=lambda: attempts.__setitem__(0, attempts[0] + 1),
        )
        self.assertEqual(fclient.upload.call_count, 1)
        self.assertEqual(attempts[0], 1)

    def test_forgejo_source_adapter_download_cooldown_refusal_on_retry_logs_no_retry(self):
        now = [1000.0]
        provider = AppTokenProvider("1", "key", "alice", role="source")
        provider.token, provider.expires = "initial-token", now[0] + 301.0
        fsa = ForgejoSourceAdapter("alice/demo", provider)
        url = "https://github.com/alice/demo/releases/download/v1/demo.bin"
        fsa._assets[url] = {"id": 11, "name": "demo.bin", "size": 4}
        stderr = io.StringIO()

        def fake_sleep(_):
            now[0] += 5.0
            _cooldowns["api.github.com"] = time.monotonic() + 120

        err502 = _http_error(502, url="https://api.github.com/repos/alice/demo/releases/assets/11")
        with (
            patch.object(fsa.client.api.opener, "open", side_effect=[err502]) as opening,
            patch("repo_mirror.github_api.time.time", side_effect=lambda: now[0]),
            patch("repo_mirror.github_api.subprocess.run", return_value=SimpleNamespace(stdout=b"sig")),
            patch("repo_mirror.forgejo_source_adapter.time.sleep", side_effect=fake_sleep),
            redirect_stderr(stderr),
            self.assertRaises(RuntimeError) as caught,
        ):
            fsa.download(url, io.BytesIO())

        self.assertEqual(
            error_detail(caught.exception),
            "already rate limited by the source or destination host; the 60 s wait limit is exhausted",
        )
        self.assertEqual(opening.call_count, 1)
        self.assertNotIn("asset download retry", stderr.getvalue())

    def test_forgejo_source_adapter_download_attempted_retry_logs_once_after_gates(self):
        fsa = ForgejoSourceAdapter("alice/demo", "source-token")
        url = "https://github.com/alice/demo/releases/download/v1/demo.bin"
        fsa._assets[url] = {"id": 11, "name": "demo.bin", "size": 4}
        stderr = io.StringIO()
        sleep_stderr = []
        open_stderr = []

        class _AssetResponse(io.BytesIO):
            status = 200
            headers = {"Content-Length": "4"}

        def fake_sleep(_):
            sleep_stderr.append(stderr.getvalue())

        err502 = _http_error(502, url="https://api.github.com/repos/alice/demo/releases/assets/11")

        def open_asset(request, timeout=60):
            open_stderr.append(stderr.getvalue())
            if len(open_stderr) == 1:
                raise err502
            return _AssetResponse(b"data")

        out = io.BytesIO()
        with (
            patch.object(fsa.client.api.opener, "open", side_effect=open_asset) as opening,
            patch("repo_mirror.forgejo_source_adapter.time.sleep", side_effect=fake_sleep),
            redirect_stderr(stderr),
        ):
            fsa.download(url, out)

        self.assertEqual(out.getvalue(), b"data")
        self.assertEqual(opening.call_count, 2)
        self.assertEqual(sleep_stderr, [""])
        self.assertEqual(open_stderr[0], "")
        self.assertIn("asset download retry 1/3 target=source", open_stderr[1])
        output = stderr.getvalue()
        self.assertEqual(output.count("asset download retry 1/3 target=source"), 1)
        self.assertNotIn("asset download retry 2/3", output)

    def test_proxy_wrapper_and_subclass_overriding_upload_count_only_issued_requests(self):
        class UploadWrapper:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def upload(self, url, data, content_type):
                return self._inner.upload(url, data, content_type)

        class SubclassUploadAPI(API):
            def upload(self, url, data, content_type):
                return super().upload(url, data, content_type)

        for make_dst in (lambda: UploadWrapper(API("token")), lambda: SubclassUploadAPI("token")):
            with self.subTest(dst=make_dst.__name__):
                _cooldowns.clear()
                dst = make_dst()
                self.assertFalse(hasattr(dst, "emits_attempt_callback"))
                # Gated on attempt 0 -> zero counts, no retry line.
                _cooldowns["uploads.github.com"] = time.monotonic() + 120
                attempts = [0]
                stderr = io.StringIO()
                with (
                    patch("urllib.request.HTTPSHandler.https_open") as opening,
                    redirect_stderr(stderr),
                    self.assertRaises(RuntimeError),
                ):
                    upload_with_reconciliation(
                        dst,
                        "https://uploads.github.com/asset",
                        io.BytesIO(b"data"),
                        "application/octet-stream",
                        "https://api.github.com/repos/dest/demo/releases/1/assets",
                        "demo.bin",
                        on_attempt=lambda: attempts.__setitem__(0, attempts[0] + 1),
                    )
                self.assertEqual(opening.call_count, 0)
                self.assertEqual(attempts[0], 0)
                self.assertNotIn("asset upload retry", stderr.getvalue())

                # Issued attempt 0 (429) then issued attempt 1 (200) -> 2 counts, 1 retry line.
                _cooldowns.clear()
                calls = []

                def respond(handler, request):
                    calls.append(request.data.read())
                    headers = Message()
                    headers["Retry-After"] = "0"
                    code = 429 if len(calls) == 1 else 200
                    response = addinfourl(io.BytesIO(b""), headers, request.full_url, code)
                    response.msg = "response"
                    return response

                attempts = [0]
                stderr = io.StringIO()
                with (
                    patch("urllib.request.HTTPSHandler.https_open", respond),
                    patch.object(dst._inner if isinstance(dst, UploadWrapper) else dst, "pages", return_value=[]),
                    patch("repo_mirror.repository.time.sleep"),
                    redirect_stderr(stderr),
                ):
                    upload_with_reconciliation(
                        dst,
                        "https://uploads.github.com/asset",
                        io.BytesIO(b"data"),
                        "application/octet-stream",
                        "https://api.github.com/repos/dest/demo/releases/1/assets",
                        "demo.bin",
                        on_attempt=lambda: attempts.__setitem__(0, attempts[0] + 1),
                    )
                self.assertEqual(len(calls), 2)
                self.assertEqual(attempts[0], 2)
                self.assertEqual(stderr.getvalue().count("asset upload retry 1/3"), 1)

    def test_github_api_subclass_overriding_request_to_super_counts_only_issued_requests(self):
        class TracingAPI(API):
            def _request(self, method, url, body=None, headers=None, *, upload=False):
                return super()._request(method, url, body, headers, upload=upload)

        # Gate refuses attempt 0 -> 0 counts, no retry line.
        api = TracingAPI("token")
        _cooldowns["uploads.github.com"] = time.monotonic() + 120
        attempts = [0]
        stderr = io.StringIO()
        with (
            patch("urllib.request.HTTPSHandler.https_open") as opening,
            redirect_stderr(stderr),
            self.assertRaises(RuntimeError),
        ):
            upload_with_reconciliation(
                api,
                "https://uploads.github.com/asset",
                io.BytesIO(b"data"),
                "application/octet-stream",
                "https://api.github.com/repos/dest/demo/releases/1/assets",
                "demo.bin",
                on_attempt=lambda: attempts.__setitem__(0, attempts[0] + 1),
            )
        self.assertEqual(opening.call_count, 0)
        self.assertEqual(attempts[0], 0)
        self.assertNotIn("asset upload retry", stderr.getvalue())

        # Attempt 0 gets 429, cooldown refuses attempt 1 -> 1 count, no retry line.
        _cooldowns.clear()
        calls = []

        def respond_429(handler, request):
            calls.append(request.data.read())
            headers = Message()
            headers["Retry-After"] = "0"
            response = addinfourl(io.BytesIO(b""), headers, request.full_url, 429)
            response.msg = "Too Many Requests"
            return response

        def trip_cooldown(_):
            _cooldowns["uploads.github.com"] = time.monotonic() + 120

        attempts = [0]
        stderr = io.StringIO()
        with (
            patch("urllib.request.HTTPSHandler.https_open", respond_429),
            patch.object(api, "pages", return_value=[]),
            patch("repo_mirror.repository.time.sleep", side_effect=trip_cooldown),
            redirect_stderr(stderr),
            self.assertRaises(RuntimeError),
        ):
            upload_with_reconciliation(
                api,
                "https://uploads.github.com/asset",
                io.BytesIO(b"data"),
                "application/octet-stream",
                "https://api.github.com/repos/dest/demo/releases/1/assets",
                "demo.bin",
                on_attempt=lambda: attempts.__setitem__(0, attempts[0] + 1),
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(attempts[0], 1)
        self.assertNotIn("asset upload retry", stderr.getvalue())

        # Positive control: attempt 0 gets 429, attempt 1 succeeds -> 2 counts, 1 retry line.
        _cooldowns.clear()
        calls = []

        def respond_retry(handler, request):
            calls.append(request.data.read())
            headers = Message()
            headers["Retry-After"] = "0"
            code = 429 if len(calls) == 1 else 200
            response = addinfourl(io.BytesIO(b""), headers, request.full_url, code)
            response.msg = "response"
            return response

        attempts = [0]
        stderr = io.StringIO()
        with (
            patch("urllib.request.HTTPSHandler.https_open", respond_retry),
            patch.object(api, "pages", return_value=[]),
            patch("repo_mirror.repository.time.sleep"),
            redirect_stderr(stderr),
        ):
            upload_with_reconciliation(
                api,
                "https://uploads.github.com/asset",
                io.BytesIO(b"data"),
                "application/octet-stream",
                "https://api.github.com/repos/dest/demo/releases/1/assets",
                "demo.bin",
                on_attempt=lambda: attempts.__setitem__(0, attempts[0] + 1),
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual(attempts[0], 2)
        self.assertEqual(stderr.getvalue().count("asset upload retry 1/3"), 1)
        self.assertNotIn("asset upload retry 2/3", stderr.getvalue())

    def test_all_destination_adapters_honour_single_upload_attempt_contract(self):
        import importlib
        import inspect
        import pkgutil
        import repo_mirror

        discovered = {}
        for modinfo in pkgutil.walk_packages(repo_mirror.__path__, repo_mirror.__name__ + "."):
            mod = importlib.import_module(modinfo.name)
            for name, cls in inspect.getmembers(mod, inspect.isclass):
                if cls.__module__ != mod.__name__ or not hasattr(cls, "upload"):
                    continue
                if cls is SourceAPI:
                    continue
                params = list(inspect.signature(cls.upload).parameters.values())
                # Destination adapters implement upload(self, url, data, content_type)
                # or forward (*args, **kwargs).
                accepts_adapter_sig = (
                    len(params) == 4
                    or any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
                )
                if accepts_adapter_sig:
                    discovered[f"{mod.__name__}.{name}"] = cls

        expected_adapters = {
            "repo_mirror.github_api.API": API,
            "repo_mirror.forgejo_destination_adapter.ForgejoDestinationAdapter": ForgejoDestinationAdapter,
            "repo_mirror.repository._IdentityGuardedDestination": _IdentityGuardedDestination,
        }
        self.assertEqual(discovered, expected_adapters)

        for qualname, cls in discovered.items():
            with self.subTest(adapter=qualname):
                self.assertFalse(hasattr(cls, "emits_attempt_callback"))

        # 1. API: notifies once on issued upload, zero when cooldown refuses.
        api = API("token")
        count = [0]
        tok = _UPLOAD_ATTEMPT_CALLBACK.set(lambda: count.__setitem__(0, count[0] + 1))
        try:
            with patch.object(api.opener, "open", return_value=_JsonResponse(b"{}")):
                api.upload("https://uploads.github.com/asset", io.BytesIO(b"x"), "application/octet-stream")
            self.assertEqual(count[0], 1)
            _cooldowns["uploads.github.com"] = time.monotonic() + 120
            with patch.object(api.opener, "open") as opening, self.assertRaises(RuntimeError):
                api.upload("https://uploads.github.com/asset", io.BytesIO(b"x"), "application/octet-stream")
            self.assertEqual(opening.call_count, 0)
            self.assertEqual(count[0], 1)
        finally:
            _UPLOAD_ATTEMPT_CALLBACK.reset(tok)
            _cooldowns.clear()

        # 2. ForgejoDestinationAdapter: notifies once on issued upload, zero when content_type or guard refuses.
        fclient = ForgejoReleases("dest/demo", "local-token", port=3000)
        fadapter = ForgejoDestinationAdapter("dest/demo", "local-token", client=fclient, guard=lambda: None)
        count = [0]
        tok = _UPLOAD_ATTEMPT_CALLBACK.set(lambda: count.__setitem__(0, count[0] + 1))
        try:
            with patch.object(fclient.opener, "open", return_value=_JsonResponse(b'{"id": 1, "name": "a", "size": 1}')):
                fadapter.upload(fadapter.target + "/releases/2/assets?name=a", io.BytesIO(b"x"), "application/octet-stream")
            self.assertEqual(count[0], 1)
            with patch.object(fclient.opener, "open") as opening, self.assertRaises(ValueError):
                fadapter.upload(fadapter.target + "/releases/2/assets?name=a", io.BytesIO(b"x"), "text/plain; charset=utf-8")
            self.assertEqual(opening.call_count, 0)
            self.assertEqual(count[0], 1)
        finally:
            _UPLOAD_ATTEMPT_CALLBACK.reset(tok)

        # 3. _IdentityGuardedDestination: forwards only, never notifies itself.
        class SilentInner:
            def upload(self, url, data, content_type):
                return None

        guarded_silent = _IdentityGuardedDestination(SilentInner(), lambda: None)
        count = [0]
        tok = _UPLOAD_ATTEMPT_CALLBACK.set(lambda: count.__setitem__(0, count[0] + 1))
        try:
            guarded_silent.upload("https://uploads.github.com/asset", io.BytesIO(b"x"), "application/octet-stream")
            self.assertEqual(count[0], 0)
            guarded_api = _IdentityGuardedDestination(API("token"), lambda: (_ for _ in ()).throw(RuntimeError("pin failed")))
            with self.assertRaises(RuntimeError):
                guarded_api.upload("https://uploads.github.com/asset", io.BytesIO(b"x"), "application/octet-stream")
            self.assertEqual(count[0], 0)
        finally:
            _UPLOAD_ATTEMPT_CALLBACK.reset(tok)


if __name__ == "__main__":
    unittest.main()
