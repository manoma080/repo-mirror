"""github_api: see README for an overview."""

import base64
import http.client
import ipaddress
import json
import math
import os
import random
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from repo_mirror.constants import CHUNK_SIZE, GITHUB, MAX_ASSET_SIZE, NAME
from repo_mirror.logging_utils import timed, timed_call


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


class SafeRedirect(HTTPRedirectHandler):
    """HTTP redirect handler that re-checks each redirect target before following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlparse(newurl)
        if target.scheme != "https" or target.username or target.password:
            raise ValueError("unsafe redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urlparse(req.full_url).netloc.lower() != target.netloc.lower():
            redirected.remove_header("Authorization")
        return redirected


class AppTokenProvider:
    """Creates and caches short-lived GitHub App installation tokens."""

    def __init__(self, app_id, private_key, owner, account_type="personal"):
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
        self.token, self.expires = None, 0

    def __call__(self):
        if not self.token or time.time() >= self.expires - 300:
            token, expires = self._mint()
            if (
                not isinstance(token, str)
                or not token.strip()
                or not isinstance(expires, (int, float))
                or not math.isfinite(expires)
                or expires <= time.time() + 300
            ):
                raise ValueError("invalid GitHub App token response")
            self.token, self.expires = token, expires
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

        def request(path, method="GET"):
            # Token mint POSTs create only short-lived credentials. An ambiguous
            # failure can leave an unused token, but never retries other writes.
            for attempt in range(4):
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
                    with opener.open(req, timeout=60) as response:
                        return json.load(response)
                except HTTPError as exc:
                    setattr(exc, "mirror_operation", ("destination", "app authentication", method))
                    if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                        raise
                    exc.close()
                except URLError, TimeoutError, ConnectionError:
                    if attempt == 3:
                        raise
                print(f"app authentication retry {attempt + 1}/3", file=sys.stderr, flush=True)
                time.sleep(2**attempt)

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


class API:
    """Small HTTP client for one GitHub or Forgejo API (JSON requests, retries, safe logging)."""

    def __init__(self, token=None):
        self._token = token
        self.role = "destination" if token else "source"
        self.opener = build_opener(SafeRedirect())

    @property
    def token(self):
        return self._token() if callable(self._token) else self._token

    def _request_token(self, method, parsed):
        return self.token if self._token else None

    def _request(self, method, url, body=None, headers=None):
        parsed = urlparse(url)
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
        for attempt in range(4):
            attempt_headers = dict(headers)
            current = self._request_token(method, parsed)
            if current:
                attempt_headers["Authorization"] = "Bearer " + current
            with timed(
                f"network target={self.role} operation=API method={method if method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE') else 'request'}"
            ):
                try:
                    with self.opener.open(
                        Request(url, data=body, headers=attempt_headers, method=method), timeout=60
                    ) as response:
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
                    if (
                        method not in ("GET", "PUT", "PATCH", "DELETE")
                        or e.code not in (429, 500, 502, 503, 504)
                        or attempt == 3
                    ):
                        raise
                except URLError:
                    if method not in ("GET", "PUT", "PATCH", "DELETE") or attempt == 3:
                        raise
            print(f"request retry {attempt + 1}/3", file=sys.stderr, flush=True)
            time.sleep(2**attempt)
        raise RuntimeError("request retries exhausted")

    def request(self, method, url, data=None, **kwargs):
        body = None if data is None else json.dumps(data).encode()
        raw = self._request(
            method, url, body, {"Accept": "application/vnd.github+json", "Content-Type": "application/json"}
        )
        return json.loads(raw) if raw else None

    def pages(self, url):
        result = []
        for page in range(1, 10001):
            separator = "&" if "?" in url else "?"
            items = self.request("GET", f"{url}{separator}per_page=100&limit=100&page={page}")
            if not isinstance(items, list):
                raise ValueError("pagination response is not an array")
            result.extend(items)
            if not items:
                return result
        raise RuntimeError("pagination exceeded safety limit")

    @timed_call(lambda self, url, output: f"network target={self.role} operation=asset method=GET")
    def download(self, url, output):
        position = output.tell()
        for attempt in range(4):
            output.seek(position)
            output.truncate()
            try:
                return _asset_download_once(self, url, output)
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
                print(f"asset download retry {attempt + 1}/3 target={self.role}", file=sys.stderr, flush=True)
                time.sleep(delay)

    def _download_once(self, url, output):
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
            try:
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
                addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
                ips = [ipaddress.ip_address(a[4][0]) for a in addresses]
                if not ips or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in ips):
                    raise ValueError("asset address is not public")
                family, socktype, proto, _, address = addresses[0]
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
        self._request("POST", url, data, {"Content-Type": content_type, "Content-Length": str(size)})


_asset_download_once = API._download_once


class SourceAPI(API):
    """HTTP client for a source forge (GitHub or Forgejo), with read-only requests."""

    def __init__(self, token):
        super().__init__()
        self.source_token = token

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
