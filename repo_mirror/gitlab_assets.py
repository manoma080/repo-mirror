"""Safe handling of public external release assets."""


def render_preserved_link(link):
    """Render the original safe URL, not a potentially expiring redirect target."""
    import html
    from urllib.parse import quote

    url = link['url']
    validate_url(url)
    name = link['name']
    kind = link['link_type']
    if (not isinstance(name, str) or not name or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or kind not in ('other', 'runbook', 'image', 'package')):
        raise UnsafeAsset('invalid asset metadata')
    name = html.escape(name).replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]')
    target = quote(url, safe=':/%.-_~')
    return f'- [{name}]({target}) ({kind})'


import ipaddress
import math
import os
import re
from urllib.parse import unquote, urlsplit


class UnsafeAsset(ValueError):
    """The asset URL or target is not safe to access or publish."""


# Allow only ordinary global unicast IPv6, excluding protocol assignments and
# 6to4. This intentionally rejects all NAT64, mapped/compatible and other
# transition addresses, including those embedding an otherwise public IPv4.
_IPV6_UNICAST = ipaddress.ip_network('2000::/3')
_IPV6_TRANSITION = (ipaddress.ip_network('2001::/23'), ipaddress.ip_network('2002::/16'))


def _public_address(ip):
    if not ip.is_global or ip.is_multicast:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        return (ip.scope_id is None and ip in _IPV6_UNICAST
                and not any(ip in network for network in _IPV6_TRANSITION))
    return True


def validate_url(url, *, allow_query=False):
    if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in url):
        raise UnsafeAsset("invalid asset URL")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        default_port = {'http': 80, 'https': 443, 'ftp': 21}.get(parts.scheme)
        if (default_port is None or not host or parts.username is not None
                or parts.password is not None or parts.port not in (None, default_port)
                or (not allow_query and "?" in url) or "#" in url or not host.isascii()):
            raise UnsafeAsset("unsafe asset URL")
        try:
            if not _public_address(ipaddress.ip_address(host)):
                raise UnsafeAsset("non-public address")
        except ValueError as exc:
            if isinstance(exc, UnsafeAsset):
                raise
            if not re.fullmatch(r"[a-zA-Z0-9.-]+", host) or "." not in host:
                raise UnsafeAsset("invalid hostname") from None
        path = unquote(parts.path).lower()
        if '%' in path or any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in path):
            raise UnsafeAsset("invalid path")
        if re.search(r"/(?:-/archive(?:/|$)|repository/archive(?:\.[^/]+)?(?:/|$))", path):
            raise UnsafeAsset("generated archives are excluded")
        return parts
    except ValueError as exc:
        if isinstance(exc, UnsafeAsset):
            raise
        raise UnsafeAsset("invalid asset URL") from None


import contextlib
import ftplib
import http.client
import socket
import ssl
import tempfile
import threading
import time
from dataclasses import dataclass
from urllib.parse import urljoin

from repo_mirror.github_api import record_api_call


class AssetError(Exception):
    """Download failed; messages deliberately contain no remote URL."""


@dataclass
class Download:
    oversize: bool
    file: object = None
    size: int | None = None


def _same_origin(a, b):
    """True when both URLs share scheme, host and effective port."""
    left, right = urlsplit(a), urlsplit(b)
    return (left.scheme.lower(), left.hostname, left.port or 443) == (
        right.scheme.lower(), right.hostname, right.port or 443)


def _open(url, timeout, headers=None):
    started = time.monotonic()
    parts = validate_url(url, allow_query=True)
    host = parts.hostname
    port = {'https': 443, 'http': 80, 'ftp': 21}[parts.scheme]
    if parts.scheme in ('http', 'https'):
        record_api_call(host, "source", "asset", "GET")
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise AssetError("DNS returned no addresses")
    for family, socktype, proto, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        if not _public_address(ip):
            raise UnsafeAsset("non-public DNS answer")
    family, socktype, proto, _, address = addresses[0]
    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:
        raise AssetError("asset download deadline exceeded")
    sock = socket.socket(family, socktype, proto)
    active_sockets = [sock]
    def interrupt():
        for active in list(active_sockets):
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    timer = threading.Timer(remaining, interrupt)
    timer.daemon = True
    timer.start()
    try:
        sock.settimeout(remaining)
        sock.connect(address)  # Connect to the vetted numeric address, never resolve twice.
        if parts.scheme == 'https':
            transport = ssl.create_default_context().wrap_socket(
                sock, server_hostname=host, do_handshake_on_connect=False)
            active_sockets[0] = transport
            transport.do_handshake()
        else:
            transport = sock
    except BaseException:
        timer.cancel()
        active_sockets[0].close()
        raise
    if parts.scheme == 'ftp':
        ftp = ftplib.FTP()
        data = None
        try:
            ftp.sock = transport
            ftp.file = transport.makefile('r', encoding=ftp.encoding)
            ftp.getresp()
            ftp.login()  # Anonymous only; never forward source credentials.
            ftp.voidcmd('TYPE I')
            passive = ftp.sendcmd('EPSV')
            match = re.fullmatch(r'229[^\r\n]*\(\|\|\|([0-9]{1,5})\|\)[^\r\n]*', passive)
            if not match or not 1024 <= int(match.group(1)) <= 65535:
                raise UnsafeAsset('invalid FTP passive port')
            data = socket.socket(family, socktype, proto)
            active_sockets.append(data)
            data.settimeout(max(0.001, timeout - (time.monotonic() - started)))
            data_address = (address[0], int(match.group(1)), *address[2:])
            data.connect(data_address)  # EPSV cannot redirect to another host.
            reply = ftp.sendcmd('RETR ' + unquote(parts.path or '/'))
            if not reply.startswith('1'):
                raise AssetError('invalid FTP transfer response')
            class FTPResponse:
                status = 200
                def getheader(self, key, default=None):
                    return default
                def read(self, size):
                    chunk = data.recv(size)
                    if not chunk:
                        ftp.voidresp()  # Do not accept a truncated or failed transfer.
                    return chunk
                def close(self):
                    data.close()
            def close_ftp():
                timer.cancel()
                data.close()
                ftp.close()
            return FTPResponse(), close_ftp
        except BaseException:
            timer.cancel()
            if data is not None:
                data.close()
            ftp.close()
            raise
    connection_class = http.client.HTTPSConnection if parts.scheme == 'https' else http.client.HTTPConnection
    conn = connection_class(host, timeout=remaining)
    conn.sock = transport
    def close():
        timer.cancel()
        conn.close()
    try:
        target = parts.path or "/"
        if "?" in url:
            target += "?" + parts.query
        request_headers = {"Accept-Encoding": "identity", "User-Agent": "repo-mirror"}
        request_headers.update(headers or {})
        conn.request("GET", target, headers=request_headers)
        return conn.getresponse(), close
    except BaseException:
        close()
        raise


def _open_before_deadline(url, deadline, headers=None):
    """Bound DNS and response headers even when the system resolver stalls."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AssetError("asset download deadline exceeded")
    done = threading.Event()
    lock = threading.Lock()
    state = {"abandoned": False}
    def run():
        try:
            result = _open(url, remaining, headers) if headers else _open(url, remaining)
            with lock:
                if state["abandoned"]:
                    result[0].close()
                    result[1]()
                else:
                    state["result"] = result
        except BaseException as exc:
            with lock:
                state["error"] = exc
        finally:
            done.set()
    threading.Thread(target=run, daemon=True).start()
    if not done.wait(max(0, deadline - time.monotonic())):
        with lock:
            state["abandoned"] = True
            result = state.pop("result", None)
        if result is not None:
            result[0].close()
            result[1]()
        raise AssetError("asset download deadline exceeded")
    if "error" in state:
        raise state["error"]
    return state["result"]


@contextlib.contextmanager
def download_asset(url, max_bytes, *, directory=None, timeout=None, max_redirects=3, headers=None):
    """Yield Download with a rewinded temporary file, or oversize=True.

    File is deleted at context exit; the caller must upload before exiting.
    Unknown lengths are streamed and checked, not rejected. No proxy, cookies,
    or credentials are used. The deadline excludes caller work after yield;
    an OS resolver that hangs may leave a daemon worker until it returns.
    HTTP framing prevents detecting bytes beyond a smaller Content-Length.
    The total deadline defaults to one hour; REPO_MIRROR_ASSET_TIMEOUT_SECONDS
    or the explicit timeout argument may override it.
    HTTP and anonymous FTP are plaintext; HTTPS redirects cannot downgrade.
    FTP uses EPSV only, pins the data socket to the validated control address,
    and refuses privileged passive ports. Original queries and credentials are
    rejected because publishing or forwarding them could expose secrets; only
    HTTPS redirect targets may carry private signed queries. Raises UnsafeAsset
    or sanitized AssetError.
    """
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    if timeout is None:
        try:
            timeout = float(os.environ.get("REPO_MIRROR_ASSET_TIMEOUT_SECONDS", "3600"))
        except ValueError:
            raise ValueError("invalid download limits") from None
    if (not isinstance(max_redirects, int) or max_redirects < 0
            or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("invalid download limits")
    yielding = False
    deadline = time.monotonic() + timeout
    try:
        current = url
        for hop in range(max_redirects + 1):
            validate_url(current, allow_query=hop > 0)
            response, close = _open_before_deadline(current, deadline, headers if _same_origin(current, url) else None)
            try:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location or hop == max_redirects:
                        raise AssetError("invalid or excessive redirect")
                    target = urljoin(current, location)
                    next_parts = validate_url(target, allow_query=True)
                    if (urlsplit(current).scheme == 'https' and next_parts.scheme != 'https') or (next_parts.scheme != 'https' and '?' in target):
                        raise UnsafeAsset('unsafe redirect')
                    current = target
                    continue
                if response.status == 429:
                    # GitLab throttles upload/download paths per IP even with a
                    # token. Honor a short Retry-After; otherwise fail visibly.
                    raw = response.getheader("Retry-After", "")
                    wait = int(raw) if raw.isdigit() else None
                    if wait is None or wait > 120 or hop > 0:
                        raise AssetError("asset server rate limited")
                    time.sleep(wait)
                    continue
                if response.status != 200:
                    raise AssetError("asset server returned an error")
                encoding = response.getheader("Content-Encoding", "identity").lower()
                if encoding != "identity":
                    raise AssetError("encoded response not supported")
                raw_length = response.getheader("Content-Length")
                length = None
                if raw_length is not None:
                    if not re.fullmatch(r"[0-9]+", raw_length):
                        raise AssetError("invalid content length")
                    length = int(raw_length)
                with tempfile.TemporaryFile(dir=directory) as file:
                    total = 0
                    while True:
                        if time.monotonic() >= deadline:
                            raise AssetError("asset download deadline exceeded")
                        read = getattr(response, "read1", response.read)
                        chunk = read(min(65536, max_bytes - total + 1))
                        if time.monotonic() >= deadline:
                            raise AssetError("asset download deadline exceeded")
                        if not chunk:
                            break
                        if length is not None and total + len(chunk) > length:
                            raise AssetError("incomplete response")
                        if len(chunk) > max_bytes - total:
                            yielding = True
                            yield Download(True)
                            return
                        file.write(chunk)
                        total += len(chunk)
                    if length is not None and total != length:
                        raise AssetError("incomplete response")
                    file.seek(0)
                    yielding = True
                    yield Download(False, file, total)
                    return
            finally:
                response.close()
                close()
    except (UnsafeAsset, AssetError):
        raise
    except Exception:
        if yielding:
            raise
        raise AssetError("asset download failed") from None
