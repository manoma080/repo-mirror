"""Tests for PR 1: git-phase error taxonomy, allowlist, and retry accounting."""

import io
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
from repo_mirror.github_api import API, AppTokenProvider, SourceAPI, _cooldowns
from repo_mirror.logging_utils import error_detail
from repo_mirror.repository import _IdentityGuardedDestination, mirror_releases, upload_with_reconciliation


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

    def read(self):
        return self._body


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
        raw_mock.upload.side_effect = [direct_429, None]
        check_calls = [0]

        def check_pins():
            check_calls[0] += 1
            if check_calls[0] >= 3:
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
        raw_mock.upload.side_effect = [direct_429, None]

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
                self.calls += 1
                if self.calls == 1:
                    err = _http_error(429, {"Retry-After": "0"}, url=url)
                    err.direct_upload_rejection = True
                    raise err
                return b"{}"

        capi = CustomReqAPI()
        self.assertFalse(capi.emits_attempt_callback)
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


if __name__ == "__main__":
    unittest.main()
