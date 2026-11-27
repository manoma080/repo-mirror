"""github_api: see README for an overview."""

import base64
import contextvars
import http.client
import ipaddress
import io
import json
import math
import os
import random
import socket
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse, urlsplit
from urllib.request import HTTPErrorProcessor, HTTPRedirectHandler, Request, build_opener

from repo_mirror.constants import CHUNK_SIZE, GITHUB, MAX_ASSET_SIZE, NAME
from repo_mirror.logging_utils import timed, timed_call

_cooldown_lock = threading.Lock()
_cooldowns = {}
_MAX_RATE_WAIT = 60
_UPLOAD_ATTEMPT_CALLBACK = contextvars.ContextVar("_UPLOAD_ATTEMPT_CALLBACK", default=None)


def _notify_upload_attempt():
    callback = _UPLOAD_ATTEMPT_CALLBACK.get()
    if callback is not None:
        callback()

_KNOWN_METHODS = frozenset(("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"))


def _is_loopback_host(host):
    # Deliberately exclude loopback traffic (127.0.0.0/8, ::1, localhost,
    # *.localhost per RFC 6761) regardless of whether the caller passes a bare
    # host, host:port, userinfo@host:port, or full URL: the local Forgejo
    # runner is not rate-limited and would pollute the metric. Pure and cheap
    # (string/IP parsing only — no DNS or network lookups).
    if not isinstance(host, str):
        return False
    raw = host.strip()
    if not raw:
        return False

    candidate = raw
    if "://" in raw:
        try:
            extracted = urlsplit(raw).hostname
        except ValueError:
            return False
        if not extracted:
            return False
        candidate = extracted
    else:
        # Handle bare IPv6 literals (e.g. "::1") before authority parsing, since
        # unbracketed colons are ambiguous with a host:port separator.
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            try:
                extracted = urlsplit("//" + raw).hostname
            except ValueError:
                return False
            if not extracted:
                return False
            candidate = extracted
        else:
            return ip.is_loopback or (
                isinstance(ip, ipaddress.IPv6Address)
                and ip.ipv4_mapped is not None
                and ip.ipv4_mapped.is_loopback
            )

    candidate = candidate.strip().lower().rstrip(".")
    if not candidate:
        return False
    if candidate == "localhost" or candidate.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return ip.is_loopback or (
        isinstance(ip, ipaddress.IPv6Address)
        and ip.ipv4_mapped is not None
        and ip.ipv4_mapped.is_loopback
    )


def _host_bucket(host):
    if isinstance(host, str):
        lowered = host.strip().lower()
        if lowered == "api.github.com":
            return "github"
        if lowered == "uploads.github.com":
            return "uploads"
    return "other"


class APICallCounter:
    """Thread-safe counter for outbound HTTP request attempts.

    Counts are keyed by ``(bucket, role, operation, method)`` where ``bucket``
    is one of ``{"github", "uploads", "other"}`` (computed at record time so
    the key space is bounded by a small constant regardless of distinct
    hostnames). Loopback hosts (``127.0.0.1``, ``::1``, ``localhost``) are
    excluded because the local Forgejo runner is not rate-limited.

    In ``format_summary()``, ``total == github + uploads + other``. The
    ``graphql`` field is an overlapping subset counting calls whose operation
    is ``"graphql"`` (currently no callers; reserved for future GraphQL
    queries), so callers must not sum ``github + uploads + other + graphql``.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._counts = {}
        self._emitted = False

    def record(self, host, role, operation, method):
        if _is_loopback_host(host):
            return
        bucket = _host_bucket(host)
        normalized_role = role if role in ("source", "destination") else "unknown"
        normalized_operation = operation if operation in ("API", "asset", "asset-hop", "graphql") else "API"
        upper_method = method.upper() if isinstance(method, str) else ""
        normalized_method = upper_method if upper_method in _KNOWN_METHODS else "REQUEST"
        key = (bucket, normalized_role, normalized_operation, normalized_method)
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1

    def reset(self):
        with self._lock:
            self._counts.clear()
            self._emitted = False

    def mark_emitted(self):
        with self._lock:
            if self._emitted:
                return False
            self._emitted = True
            return True

    def snapshot(self):
        with self._lock:
            return dict(self._counts)

    def format_summary(self):
        counts = self.snapshot()
        total = sum(counts.values())
        github = 0
        uploads = 0
        other = 0
        graphql = 0
        by_role = {}
        by_operation = {}
        for (bucket, role, operation, method), count in counts.items():
            if bucket == "github":
                github += count
            elif bucket == "uploads":
                uploads += count
            else:
                other += count
            if operation == "graphql":
                graphql += count
            by_role[role] = by_role.get(role, 0) + count
            op_key = f"{operation.lower().replace('-', '_')}_{method.lower()}"
            by_operation[op_key] = by_operation.get(op_key, 0) + count
        role_part = ",".join(f"{k}:{v}" for k, v in sorted(by_role.items())) if by_role else "none"
        op_part = ",".join(f"{k}:{v}" for k, v in sorted(by_operation.items())) if by_operation else "none"
        return (
            f"api_calls total={total} github={github} uploads={uploads} "
            f"other={other} graphql={graphql} by_role={role_part} by_operation={op_part}"
        )


_api_call_counter = APICallCounter()


def record_api_call(host, role, operation, method):
    _api_call_counter.record(host, role, operation, method)


def reset_api_call_metrics():
    _api_call_counter.reset()


def api_call_metrics_snapshot():
    return _api_call_counter.snapshot()


def format_api_call_summary():
    return _api_call_counter.format_summary()


def emit_api_call_summary(stream=None):
    """Print the api_calls summary line at most once since the last reset."""
    if not _api_call_counter.mark_emitted():
        return False
    print(format_api_call_summary(), file=sys.stderr if stream is None else stream, flush=True)
    return True


def _wait_for_cooldown(host, budget=None):
    if budget is None:
        budget = [_MAX_RATE_WAIT]
    while True:
        with _cooldown_lock:
            remaining = _cooldowns.get(host, 0) - time.monotonic()
        if remaining <= 0:
            return
        if remaining > budget[0]:
            raise RuntimeError("already rate limited by the source or destination host; the 60 s wait limit is exhausted")
        budget[0] -= remaining
        time.sleep(remaining)


def _rate_delay(exc, attempt):
    headers = exc.headers or {}
    delays = []
    value = headers.get("Retry-After")
    if value:
        try:
            delay = float(value)
        except (ValueError, TypeError):
            try:
                delay = parsedate_to_datetime(value).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                delay = 60 * 2**attempt
        delays.append(delay)
    if headers.get("X-RateLimit-Remaining") == "0":
        try:
            delays.append(float(headers.get("X-RateLimit-Reset")) - time.time())
        except (ValueError, TypeError, OverflowError):
            delays.append(60 * 2**attempt)
    if not delays:
        delays.append(60 * 2**attempt)
    if any(math.isnan(delay) for delay in delays):
        return float("inf")
    return max(0, *delays)


def _is_rate_limited(exc):
    if exc.code == 429:
        return True
    if exc.code != 403:
        return False
    headers = exc.headers or {}
    if headers.get("X-RateLimit-Remaining") == "0" or headers.get("Retry-After"):
        return True
    if exc.fp is None:
        return False
    original = exc.fp
    body = exc.read(65536)
    original.close()
    exc.fp = io.BytesIO(body)
    return b"secondary rate limit" in body.lower() or b"abuse detection" in body.lower()


def _publish_limit(host, exc, attempt):
    delay = _rate_delay(exc, attempt)
    with _cooldown_lock:
        _cooldowns[host] = max(_cooldowns.get(host, 0), time.monotonic() + delay)
    return delay


def _publish_exhausted_response(host, response):
    headers = getattr(response, "headers", None)
    if headers and headers.get("X-RateLimit-Remaining") == "0":
        _publish_limit(host, response, 0)


def transient_transfer_error(exc):
    if isinstance(exc, socket.gaierror):
        return exc.errno == socket.EAI_AGAIN
    if isinstance(exc, HTTPError):
        return exc.code == 429 or 500 <= exc.code <= 599
    if isinstance(exc, URLError) and isinstance(exc.reason, socket.gaierror):
        return exc.reason.errno == socket.EAI_AGAIN
    status = getattr(exc, "mirror_asset_status", None)
    if status is not None:
        return status == 429 or 500 <= status <= 599
    return isinstance(exc, (URLError, TimeoutError, ConnectionError, ssl.SSLEOFError, http.client.IncompleteRead, http.client.RemoteDisconnected))


def transfer_retry_delay(exc, attempt):
    headers = getattr(exc, "headers", None)
    retry_after = headers.get("Retry-After") if headers else getattr(exc, "mirror_retry_after", None)
    delay = 0
    if retry_after:
        try:
            delay = float(retry_after)
        except (ValueError, TypeError):
            try:
                delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                pass
    if delay > 60:
        return None
    if not math.isfinite(delay):
        delay = 0
    return max(0, delay, 2 ** attempt + random.uniform(0, 1))


class UploadErrorProcessor(HTTPErrorProcessor):
    """Identify a rejection of the original upload, never a redirected hop."""

    def http_response(self, request, response):
        direct_rejection = getattr(request, "_original_upload", False) and response.code == 429
        try:
            return super().http_response(request, response)
        except HTTPError as exc:
            if direct_rejection and exc.code == 429 and exc.fp is response:
                exc.direct_upload_rejection = True
            raise

    https_response = http_response


class SafeRedirect(HTTPRedirectHandler):
    """HTTP redirect handler that re-checks each redirect target before following it."""

    def https_request(self, request):
        if not getattr(request, "_mirror_metrics_counted", False):
            role, operation = getattr(request, "_mirror_metrics", ("source", "asset"))
            setattr(request, "_mirror_metrics", (role, operation))
            setattr(request, "_mirror_metrics_counted", True)
            record_api_call(urlparse(request.full_url).hostname, role, operation, request.get_method())
        return request

    http_request = https_request

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlparse(newurl)
        if target.scheme != "https" or target.username or target.password:
            raise ValueError("unsafe redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            role, operation = getattr(req, "_mirror_metrics", ("source", "asset"))
            next_operation = "asset-hop" if operation == "asset" else operation
            setattr(redirected, "_mirror_metrics", (role, next_operation))
            if urlparse(req.full_url).netloc.lower() != target.netloc.lower():
                redirected.remove_header("Authorization")
        return redirected


def https_origin(url):
    """Return the normalized HTTPS origin, rejecting credentials and malformed URLs."""
    parsed = urlparse(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment):
        raise ValueError("unsafe HTTPS origin")
    port = parsed.port
    if port == 0:
        raise ValueError("unsafe HTTPS origin")
    return (parsed.scheme, parsed.hostname, 443 if port is None else port)


class OriginRedirect(SafeRedirect):
    def __init__(self, origin):
        self.origin = origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if https_origin(newurl) != self.origin:
            raise ValueError("redirect outside configured origin")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class AppTokenProvider:
    """Creates and caches short-lived GitHub App installation tokens."""

    def __init__(self, app_id, private_key, owner, account_type="personal", role="destination"):
        if (
            not app_id.isdigit()
            or not private_key
            or not NAME.fullmatch(owner)
            or owner in (".", "..")
            or account_type not in ("personal", "organization")
        ):
            raise ValueError("invalid GitHub App configuration")
        self.app_id, self.private_key, self.owner, self.account_type = (
            app_id,
            private_key,
            owner,
            account_type,
        )
        self.role = role if role in ("source", "destination") else "destination"
        self.token, self.expires = None, 0
        self._lock = threading.RLock()
        self._verified = None

    def verified_snapshot(self):
        with self._lock:
            token = self()
            if self._verified is None or self._verified[0] != token:
                raise RuntimeError("GitHub App token owner is unverifiable")
            return self._verified

    def __call__(self):
        with self._lock:
            if not self.token or time.time() >= self.expires - 300:
                verified_owner, verified_type = self.owner, self.account_type
                token, expires = self._mint()
                if (self.owner, self.account_type) != (verified_owner, verified_type):
                    raise ValueError("GitHub App configuration changed during token mint")
                if (
                    not isinstance(token, str)
                    or not token.strip()
                    or not isinstance(expires, (int, float))
                    or not math.isfinite(expires)
                    or expires <= time.time() + 300
                ):
                    raise ValueError("invalid GitHub App token response")
                self.token, self.expires = token, expires
                self._verified = (token, verified_owner, verified_type)
            return self.token

    def _mint(self):
        def encode(data):
            return base64.urlsafe_b64encode(data).rstrip(b"=")

        now = int(time.time())
        message = b".".join(
            (
                encode(b'{"alg":"RS256","typ":"JWT"}'),
                encode(
                    json.dumps(
                        {"iat": now - 60, "exp": now + 540, "iss": self.app_id}, separators=(",", ":")
                    ).encode()
                ),
            )
        )
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, message)
            os.close(write_fd)
            write_fd = None
            result = subprocess.run(
                ["openssl", "dgst", "-sha256", "-sign", "/dev/stdin", f"/dev/fd/{read_fd}"],
                input=self.private_key.encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
                pass_fds=(read_fd,),
            )
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
        jwt = (message + b"." + encode(result.stdout)).decode()
        opener = build_opener(SafeRedirect())

        budget = [_MAX_RATE_WAIT]
        host = urlparse(GITHUB).hostname

        def request(path, method="GET"):
            # Token mint POSTs create only short-lived credentials. An ambiguous
            # failure can leave an unused token, but never retries other writes.
            for attempt in range(4):
                _wait_for_cooldown(host, budget)
                limited = False
                req = Request(
                    GITHUB + path,
                    method=method,
                    data=b"{}" if method == "POST" else None,
                    headers={
                        "Authorization": "Bearer " + jwt,
                        "Accept": "application/vnd.github+json",
                        "Content-Type": "application/json",
                        "User-Agent": "repo-mirror",
                    },
                )
                try:
                    if attempt > 0:
                        print(f"app authentication retry {attempt}/3", file=sys.stderr, flush=True)
                    setattr(req, "_mirror_metrics", (self.role, "API"))
                    setattr(req, "_mirror_metrics_counted", True)
                    record_api_call(host, self.role, "API", method)
                    with opener.open(req, timeout=60) as response:
                        _publish_exhausted_response(host, response)
                        return json.load(response)
                except HTTPError as exc:
                    setattr(exc, "mirror_operation", ("destination", "app authentication", method))
                    limited = _is_rate_limited(exc)
                    delay = _publish_limit(host, exc, attempt) if limited else None
                    if (exc.code not in (429, 500, 502, 503, 504) and not limited) or attempt == 3 or (limited and delay > _MAX_RATE_WAIT):
                        raise
                    exc.close()
                except URLError, TimeoutError, ConnectionError:
                    if attempt == 3:
                        raise
                if not limited:
                    delay = 2**attempt
                    if delay > budget[0]:
                        raise RuntimeError("retry wait limit exceeded")
                    budget[0] -= delay
                    time.sleep(delay)

        kind = "users" if self.account_type == "personal" else "orgs"
        installation = request(f"/{kind}/{quote(self.owner, safe='')}/installation")
        installation_id = installation["id"]
        if (
            not isinstance(installation_id, int)
            or isinstance(installation_id, bool)
            or installation_id <= 0
            or installation.get("account", {}).get("login", "").lower() != self.owner.lower()
        ):
            raise ValueError("GitHub App installation owner mismatch")
        result = request(f"/app/installations/{installation_id}/access_tokens", "POST")
        expiry = datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            raise ValueError("invalid GitHub App token response")
        return result["token"], expiry.timestamp()


def _extract_link_header(response):
    """Extract and combine any HTTP ``Link`` headers from a response object."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        values = get_all("Link")
        if isinstance(values, (list, tuple)):
            joined = ", ".join(v for v in values if isinstance(v, str) and v)
            return joined or None
        return values if isinstance(values, str) and values else None
    if isinstance(headers, dict):
        values = [v for k, v in headers.items() if isinstance(k, str) and k.lower() == "link" and isinstance(v, str) and v]
        return ", ".join(values) if values else None
    get = getattr(headers, "get", None)
    if callable(get):
        value = get("Link")
        return value if isinstance(value, str) and value else None
    return None


def _parse_link_header(header_value):
    """Parse an RFC 8288 ``Link`` header, returning ``(next_url, all_rels)``."""
    if not isinstance(header_value, str) or not header_value.strip():
        return None, frozenset()
    n = len(header_value)
    i = 0
    next_url = None
    all_rels = set()

    def skip_to_next_entry(pos):
        in_quotes = False
        while pos < n:
            ch = header_value[pos]
            if in_quotes:
                if ch == "\\" and pos + 1 < n:
                    pos += 2
                    continue
                if ch == '"':
                    in_quotes = False
            else:
                if ch == '"':
                    in_quotes = True
                elif ch == ",":
                    return pos + 1
            pos += 1
        return pos

    while i < n:
        while i < n and (header_value[i].isspace() or header_value[i] == ","):
            i += 1
        if i >= n:
            break
        if header_value[i] != "<":
            i = skip_to_next_entry(i)
            continue
        gt = header_value.find(">", i + 1)
        if gt == -1:
            break
        target = header_value[i + 1 : gt].strip()
        i = gt + 1
        entry_rels = set()
        while i < n:
            while i < n and header_value[i].isspace():
                i += 1
            if i >= n:
                break
            if header_value[i] == ",":
                i += 1
                break
            if header_value[i] != ";":
                i = skip_to_next_entry(i)
                break
            i += 1
            param_start = i
            while i < n and header_value[i] not in "=;,":
                i += 1
            if i >= n or header_value[i] != "=":
                continue
            name = header_value[param_start:i].strip().lower()
            i += 1
            while i < n and header_value[i].isspace():
                i += 1
            if i < n and header_value[i] == '"':
                i += 1
                chars = []
                while i < n:
                    ch = header_value[i]
                    if ch == "\\" and i + 1 < n:
                        chars.append(header_value[i + 1])
                        i += 2
                    elif ch == '"':
                        i += 1
                        break
                    else:
                        chars.append(ch)
                        i += 1
                value = "".join(chars)
            else:
                val_start = i
                while i < n and header_value[i] not in ";,":
                    i += 1
                value = header_value[val_start:i].strip()
            if name == "rel":
                for token in value.split():
                    if token:
                        entry_rels.add(token.lower())
        if target and entry_rels:
            all_rels.update(entry_rels)
            if "next" in entry_rels and next_url is None:
                next_url = target
    return next_url, frozenset(all_rels)


class API:
    """Small HTTP client for one GitHub or Forgejo API (JSON requests, retries, safe logging)."""

    def __init__(self, token=None):
        self._token = token
        self.role = "destination" if token else "source"
        self.opener = build_opener(SafeRedirect(), UploadErrorProcessor())
        self._response_state = threading.local()

    @property
    def token(self):
        return self._token() if callable(self._token) else self._token

    def _request_token(self, method, parsed):
        return self.token if self._token else None

    def _request(self, method, url, body=None, headers=None, *, upload=False):
        parsed = urlparse(url)
        origin = getattr(self, "trusted_api_origin", None)
        if origin is not None and https_origin(url) != origin:
            raise ValueError("API request outside configured origin")
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise ValueError("API URL must be credential-free HTTPS")
        headers = dict(headers or {})
        headers["User-Agent"] = "repo-mirror"
        if self._token:
            if parsed.hostname not in ("api.github.com", "uploads.github.com") or parsed.port not in (
                None,
                443,
            ):
                raise ValueError("refusing to send destination credentials to non-GitHub host")
        budget = [_MAX_RATE_WAIT]
        for attempt in range(4):
            _wait_for_cooldown(parsed.hostname, budget)
            rate_limited = False
            attempt_headers = dict(headers)
            current = self._request_token(method, parsed)
            if current:
                attempt_headers["Authorization"] = "Bearer " + current
            with timed(
                f"network target={self.role} operation=API method={method if method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE') else 'request'}"
            ):
                try:
                    request = Request(url, data=body, headers=attempt_headers, method=method)
                    if upload:
                        setattr(request, "_original_upload", True)
                    _wait_for_cooldown(parsed.hostname, budget)
                    if attempt > 0:
                        print(f"request retry {attempt}/3", file=sys.stderr, flush=True)
                    if upload:
                        _notify_upload_attempt()
                    setattr(request, "_mirror_metrics", (self.role, "API"))
                    setattr(request, "_mirror_metrics_counted", True)
                    record_api_call(parsed.hostname, self.role, "API", method)
                    with self.opener.open(request, timeout=60) as response:
                        _publish_exhausted_response(parsed.hostname, response)
                        state = getattr(self, "_response_state", None)
                        if state is not None:
                            state.link = _extract_link_header(response)
                        return response.read()
                except HTTPError as e:
                    setattr(
                        e,
                        "mirror_operation",
                        (
                            self.role,
                            "API",
                            method if method in ("GET", "POST", "PUT", "PATCH", "DELETE") else "request",
                        ),
                    )
                    limited = _is_rate_limited(e)
                    delay = _publish_limit(parsed.hostname, e, attempt) if limited else None
                    if (
                        method not in ("GET", "PUT", "PATCH", "DELETE")
                        or (e.code not in (429, 500, 502, 503, 504) and not (method == "GET" and limited))
                        or attempt == 3
                        or (limited and delay > _MAX_RATE_WAIT)
                    ):
                        raise
                    if limited:
                        rate_limited = True
                    e.close()
                except URLError as e:
                    # Keep the operation on the escaping error so public
                    # diagnostics can name it without the raw exception text.
                    setattr(
                        e,
                        "mirror_operation",
                        (
                            self.role,
                            "API",
                            method if method in ("GET", "POST", "PUT", "PATCH", "DELETE") else "request",
                        ),
                    )
                    if method not in ("GET", "PUT", "PATCH", "DELETE") or attempt == 3:
                        raise
            if not rate_limited:
                delay = 2**attempt
                if delay > budget[0]:
                    raise RuntimeError("retry wait limit exceeded")
                budget[0] -= delay
                time.sleep(delay)
        raise RuntimeError("request retries exhausted")

    def request(self, method, url, data=None, **kwargs):
        body = None if data is None else json.dumps(data).encode()
        raw = self._request(
            method, url, body, {"Accept": "application/vnd.github+json", "Content-Type": "application/json"}
        )
        return json.loads(raw) if raw else None

    def create_personal_repository(self, owner, data):
        # Pin the actual credential across identity verification and creation;
        # dynamic providers may otherwise refresh between the two requests.
        if type(self._token) is AppTokenProvider:
            token, verified_owner, verified_type = self._token.verified_snapshot()
            if verified_type != "personal" or verified_owner.lower() != owner.lower():
                raise RuntimeError("GitHub App installation owner does not match configured destination")
        else:
            token = self.token
        pinned = API(token)
        pinned.opener = self.opener
        if type(self._token) is not AppTokenProvider:
            identity = pinned.request("GET", GITHUB + "/user")
            login = identity.get("login") if isinstance(identity, dict) else None
            if not isinstance(login, str) or not login or login.lower() != owner.lower():
                raise RuntimeError("authenticated GitHub user does not match configured destination")
        return pinned.request("POST", GITHUB + "/user/repos", data)

    def pages(self, url):
        result = []
        separator = "&" if "?" in url else "?"
        next_url = None
        following_links = False
        seen_urls = set()
        state = getattr(self, "_response_state", None)
        for page in range(1, 10001):
            request_url = next_url if next_url is not None else f"{url}{separator}per_page=100&limit=100&page={page}"
            if request_url in seen_urls:
                raise RuntimeError("pagination cycle detected")
            seen_urls.add(request_url)
            if state is not None:
                state.link = None
            items = self.request("GET", request_url)
            if not isinstance(items, list):
                raise ValueError("pagination response is not an array")
            result.extend(items)
            if not items:
                return result
            link_header = getattr(state, "link", None) if state is not None else None
            next_link, rels = _parse_link_header(link_header)
            if next_link is not None:
                resolved = urljoin(request_url, next_link)
                if https_origin(resolved) != https_origin(url):
                    raise ValueError("pagination link outside configured origin")
                next_url = resolved
                following_links = True
            else:
                if following_links or "last" in rels:
                    return result
                next_url = None
        raise RuntimeError("pagination exceeded safety limit")

    @timed_call(lambda self, url, output: f"network target={self.role} operation=asset method=GET")
    def download(self, url, output):
        position = output.tell()
        for attempt in range(4):
            output.seek(position)
            output.truncate()
            notified = False

            def notify_attempt(current_attempt=attempt):
                nonlocal notified
                if not notified:
                    notified = True
                    if current_attempt > 0:
                        print(f"asset download retry {current_attempt}/3 target={self.role}", file=sys.stderr, flush=True)

            try:
                return _asset_download_once(self, url, output, on_attempt=notify_attempt)
            except Exception as exc:
                output.seek(position)
                output.truncate()
                if not transient_transfer_error(exc) or attempt == 3:
                    raise
                delay = transfer_retry_delay(exc, attempt)
                if delay is None:
                    raise
                if isinstance(exc, HTTPError):
                    exc.close()
                time.sleep(delay)

    def _download_once(self, url, output, *, on_attempt=None):
        if self.token:
            parsed = urlparse(url)
            if (
                parsed.scheme != "https"
                or parsed.username
                or parsed.password
                or parsed.hostname not in ("api.github.com", "uploads.github.com")
                or parsed.port not in (None, 443)
            ):
                raise ValueError("refusing unsafe destination asset URL")
            request = Request(
                url,
                headers={
                    "Accept": "application/octet-stream",
                    "User-Agent": "repo-mirror",
                    "Authorization": "Bearer " + self.token,
                },
            )
            if on_attempt is not None:
                on_attempt()
            try:
                setattr(request, "_mirror_metrics", (self.role, "asset"))
                setattr(request, "_mirror_metrics_counted", True)
                record_api_call(parsed.hostname, self.role, "asset", "GET")
                with self.opener.open(request, timeout=60) as response:
                    copy_asset(response, output)
            except HTTPError as exc:
                setattr(exc, "mirror_operation", ("destination", "asset", "GET"))
                raise
            return
        # Connect to a validated address directly; do not use a proxy or resolve twice.
        for _ in range(10):
            with timed("network target=source operation=asset-hop method=GET"):
                parsed = urlparse(url)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.fragment
                ):
                    raise ValueError("unsafe asset URL")
                port = parsed.port or 443
                trusted_origin = getattr(self, 'trusted_asset_origin', None)
                if trusted_origin is not None and (parsed.hostname, port) != trusted_origin:
                    raise ValueError("asset redirect outside configured origin")
                record_api_call(parsed.hostname, self.role, "asset-hop", "GET")
                addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
                ips = [ipaddress.ip_address(a[4][0]) for a in addresses]
                if not ips or (trusted_origin is None and any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in ips)):
                    raise ValueError("asset address is not public")
                family, socktype, proto, _, address = addresses[0]
                if on_attempt is not None:
                    on_attempt()
                raw = socket.socket(family, socktype, proto)
                raw.settimeout(60)
                try:
                    raw.connect(address)
                    tls = ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname)
                    conn = http.client.HTTPSConnection(parsed.hostname, port, timeout=60)
                    conn.sock = tls
                    try:
                        path = parsed.path or "/"
                        if parsed.query:
                            path += "?" + parsed.query
                        conn.request(
                            "GET",
                            path,
                            headers={"Accept": "application/octet-stream", "User-Agent": "repo-mirror"},
                        )
                        response = conn.getresponse()
                        if response.status in (301, 302, 303, 307, 308):
                            location = response.getheader("Location")
                            if not location:
                                raise ValueError("asset redirect missing location")
                            url = urljoin(url, location)
                            continue
                        if response.status != 200:
                            if response.status == 403:
                                exc = HTTPError(url, 403, "", response.headers, None)
                                setattr(exc, "mirror_operation", ("source", "asset", "GET"))
                                raise exc
                            exc = RuntimeError("asset download failed")
                            setattr(exc, "mirror_asset_status", response.status)
                            setattr(exc, "mirror_retry_after", response.getheader("Retry-After"))
                            raise exc
                        copy_asset(response, output)
                        return
                    finally:
                        conn.close()
                finally:
                    raw.close()
        raise ValueError("too many asset redirects")

    def upload(self, url, data, content_type):
        position = data.tell()
        data.seek(0, os.SEEK_END)
        size = data.tell() - position
        data.seek(position)
        if size > MAX_ASSET_SIZE:
            raise ValueError("asset exceeds GitHub size limit")
        self._request("POST", url, data, {"Content-Type": content_type, "Content-Length": str(size)}, upload=True)


_asset_download_once = API._download_once


class SourceAPI(API):
    """HTTP client for a source forge (GitHub or Forgejo), with read-only requests."""

    def __init__(self, token):
        super().__init__()
        self.source_token = token

    def restrict_origin(self, url):
        self.trusted_api_origin = https_origin(url)
        self.opener = build_opener(OriginRedirect(self.trusted_api_origin), UploadErrorProcessor())

    def _request_token(self, method, parsed):
        if (
            method == "GET"
            and parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and parsed.hostname == "api.github.com"
            and parsed.port in (None, 443)
        ):
            return self.source_token() if callable(self.source_token) else self.source_token
        return None


def copy_asset(response, output):
    """Copy a response body to a file in chunks, stopping if it exceeds the size limit."""
    size = 0
    getheader = getattr(response, "getheader", None)
    length = getheader("Content-Length") if callable(getheader) else None
    encoding = getheader("Transfer-Encoding") if callable(getheader) else None
    expected = int(length) if isinstance(length, str) and length.isascii() and length.isdigit() and not encoding else None
    while True:
        chunk = response.read(CHUNK_SIZE)
        if not chunk:
            if expected is not None and size < expected:
                raise http.client.IncompleteRead(b"", expected - size)
            return size
        size += len(chunk)
        if size > MAX_ASSET_SIZE:
            raise ValueError("asset exceeds GitHub size limit")
        output.write(chunk)


def same_file(left, right):
    """Return True when two release-asset records describe the same file (name, size and digest)."""
    left.seek(0)
    right.seek(0)
    while True:
        a, b = left.read(CHUNK_SIZE), right.read(CHUNK_SIZE)
        if a != b:
            return False
        if not a:
            return True


def not_found(exc):
    """Return True when an exception is an HTTP 404 Not Found error."""
    return isinstance(exc, HTTPError) and exc.code == 404
