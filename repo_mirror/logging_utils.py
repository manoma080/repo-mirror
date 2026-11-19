"""logging_utils: see README for an overview."""

import http.client
import math
import os
import re
import socket
import ssl
import subprocess
import sys
import time
from collections import Counter
from contextlib import contextmanager
from functools import wraps
from urllib.error import HTTPError, URLError

from repo_mirror.constants import LOG_SECRETS, REPOSITORY_TIME
from repo_mirror.journal import ReleaseRecoveryError


def safe_label(value):
    # Logs are public: conservative allowlist, never emit URLs or suspicious values.
    """Turn a value into a log-safe label (secrets and odd characters removed)."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.@+/-]{1,100}", value):
        return "[redacted]"
    lowered = value.lower()
    if (
        "secret" in lowered
        or "token" in lowered
        or "password" in lowered
        or "credential" in lowered
        or re.search(
            r"(?i)(?:gh[pousr]_|github_pat_|bearer|awsaccesskey|signature|akia[0-9a-z]{16}|xox[baprs]-|sk[-_])",
            value,
        )
        or re.search(r"[A-Za-z0-9_=]{32,}", value)
        or re.search(
            r"(?<![A-Za-z0-9])(?:(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{8}-){3}(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{8}(?![A-Za-z0-9])",
            value,
        )
        or re.search(r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value)
        or re.search(r"(?i)[0-9a-f]{24,}", value)
    ):
        return "[redacted]"
    # Catch long, opaque hyphenated runs without treating every long project
    # name as a credential. This bounded heuristic cannot recognize all secrets.
    for run in re.findall(r"[A-Za-z0-9_-]{32,}", value):
        chars = run.replace("-", "").replace("_", "")
        if len(chars) >= 32:
            counts = Counter(chars)
            entropy = -sum((n / len(chars)) * math.log2(n / len(chars)) for n in counts.values())
            if entropy >= 4.2:
                return "[redacted]"
    for secret in (
        *LOG_SECRETS.get(),
        os.environ.get("GH_TOKEN", ""),
        os.environ.get("MIRROR_TIMESTAMP_SECRET", ""),
    ):
        if callable(secret):
            secret = getattr(secret, "token", None)
        if secret and (secret in value or (value in secret and len(value) >= 8)):
            return "[redacted]"
    return value


@contextmanager
def timed(label):
    """Context manager that logs how long a block takes."""
    start = time.monotonic()
    try:
        yield
    finally:
        print(f"{label} elapsed={time.monotonic() - start:.3f}s", file=sys.stderr, flush=True)


def timed_call(label):
    """Decorator that logs how long the wrapped function call takes."""

    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with timed(label(*args, **kwargs)):
                return fn(*args, **kwargs)

        return wrapped

    return decorate


def timed_repository(fn):
    """Decorator that times one repository mirror and records its log context."""

    @wraps(fn)
    def wrapped(source, name, owner, src, dst, token, account_type="organization", journal=None):
        context = LOG_SECRETS.set((token,))
        started = time.monotonic()
        try:
            with timed(f"repository source_repo={safe_label(source.get('owner'))}/{safe_label(name)}"):
                return fn(source, name, owner, src, dst, token, account_type, journal)
        finally:
            elapsed = time.monotonic() - started
            counter = REPOSITORY_TIME.get()
            if counter is not None:
                counter[0] += elapsed
            LOG_SECRETS.reset(context)

    return wrapped


SAFE_ERROR_MESSAGES = frozenset(
    {
        "source type must be github or forgejo",
        "source base_url must be credential-free HTTPS",
        "invalid owner or prefix",
        "repos must be all or a list of repository names",
        "invalid discovered repository name",
        "invalid destination repository name",
        "destination collision: repository has no matching source marker",
        "unsafe redirect",
        "API URL must be credential-free HTTPS",
        "refusing to send destination credentials to non-GitHub host",
        "request retries exhausted",
        "already rate limited by the source or destination host; the 60 s wait limit is exhausted",
        "pagination response is not an array",
        "pagination exceeded safety limit",
        "refusing unsafe destination asset URL",
        "unsafe asset URL",
        "asset address is not public",
        "asset redirect missing location",
        "asset download failed",
        "too many asset redirects",
        "asset exceeds GitHub size limit",
        "failure journal must not be a symlink",
        "invalid failure journal",
        "created repository owner does not match configured destination",
        "source clone URL does not match source host",
        "pinned source relocation detected; migration not applied",
        "source relocation requires durable migration state and authoritative config publication",
        "source default branch missing; refusing to prune",
        "multiple destination releases for tag",
        "ambiguous untagged destination draft; manual reconciliation required",
        "invalid source latest release",
        "release assets response is not an array",
        "source release name uses reserved marker",
        "source release changed during sync",
        "release name too long for pending marker",
        "source asset size mismatch",
        "source asset digest mismatch",
        "destination asset digest mismatch",
        "release asset uploads failed",
        "source starter asset skipped; release remains incomplete",
        "Forgejo empty source body cannot be safely mirrored; inspect any pending release and reconcile manually before retrying",
        "GH_TOKEN and valid DESTINATION_OWNER are required",
        "config must be an object",
        "destination_account_type must be personal or organization",
        "sources must be a list",
        "duplicate destination repository",
        # Migration transaction failures: fixed reasons only, never free text.
        "migration configuration invalid",
        "migration configuration changed by another writer",
        "migration configuration publication failed",
        "migration configuration rewrite failed",
        "migration destination identity mismatch",
        "migration destination missing",
        "migration destination name collision",
        "migration destination name mismatch",
        "migration destination pin is ambiguous",
        "migration destination pin replaced",
        "migration original binding changed",
        "migration phase invalid",
        "migration record missing",
        "migration requires named repository configuration",
        "migration signing secret required",
        "migration source identity mismatch",
        *ReleaseRecoveryError.MESSAGES.values(),
        "starter release asset detected; manually remove the starter asset from the pending GitHub release, then rerun",
    }
)


def _safe_operation(exc):
    """Return the sanitized ``(target, operation, method)`` label, or None."""
    operation = getattr(exc, "mirror_operation", None)
    if (
        type(operation) is tuple
        and len(operation) == 3
        and all(type(part) is str for part in operation)
        and (
            operation == ("destination", "release", "create")
            or (
                operation[0] == "destination"
                and operation[1] == "app authentication"
                and operation[2] in ("GET", "POST")
            )
            or (
                operation[0] in ("source", "destination")
                and operation[1] in ("API", "asset")
                and operation[2] in ("GET", "POST", "PUT", "PATCH", "DELETE", "request")
            )
        )
    ):
        return operation
    return None


_GIT_HTTP_STATUS = re.compile(
    r"\bhttp(?:\s*error)?\s*[;:(]?\s*(\d{3})\b|returned error:\s*(\d{3})\b"
)


def _git_http_status(text):
    """Return the HTTP status Git reported, if any, without echoing its text."""
    match = _GIT_HTTP_STATUS.search(text)
    if match is None:
        return None
    value = match.group(1) or match.group(2)
    return int(value) if 100 <= int(value) <= 599 else None


def _git_http_reason(status):
    """Describe an HTTP status Git reported as a fixed, public-safe cause."""
    if status == 429:
        return "HTTP 429 (the source host rate limited the request; the next scheduled run retries)"
    if status in (401, 403):
        return f"HTTP {status} (access denied)"
    if 500 <= status <= 599:
        return f"HTTP {status} (source host server error)"
    return f"HTTP {status}"


def _network_reason(exc):
    """Map a transport failure to a fixed, public-safe cause label.

    Only the exception *type* selects the label; the exception message is never
    emitted because it can carry host names, ports or credentials.
    """
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(reason, socket.gaierror):
        return "dns failure"
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused"
    if isinstance(reason, ConnectionResetError):
        return "connection reset"
    if isinstance(reason, (ssl.SSLError, ssl.CertificateError)):
        return "tls failure"
    if isinstance(reason, (http.client.HTTPException, OSError)):
        return "connection failure"
    return "transport failure"


def error_detail(exc):
    # Never emit arbitrary exception text, URLs, response bodies or class names.
    """Build a short, redacted description of an exception for public logs."""
    phase = getattr(exc, "mirror_git_phase", None)
    if phase in ("snapshot fetch", "snapshot push", "remote verification", "default switch"):
        if isinstance(exc, subprocess.CalledProcessError):
            raw = exc.stderr
            text = raw.decode("utf-8", "replace").lower() if isinstance(raw, bytes) else raw.lower() if isinstance(raw, str) else ""
            if "gh001" in text or "exceeds github's file size limit" in text:
                reason = "large file rejected (100 MB limit); inspect repository history and consider Git LFS"
            elif "pack exceeds maximum allowed size" in text or "push cannot exceed 2gb" in text or "push cannot exceed 2 gb" in text:
                reason = "push exceeds 2 GB limit; split the history into smaller pushes"
            elif any(s in text for s in ("permission denied", "403", "authentication failed", "access denied", "write access to repository not granted")):
                reason = "permission or authentication failure; check repository access and token permissions"
            elif any(s in text for s in ("could not resolve host", "connection timed out", "connection reset", "failed to connect", "remote end hung up unexpectedly")):
                reason = "network failure; check connectivity and retry after a fresh safety check"
            elif (status := _git_http_status(text)) is not None:
                # Git only reports "error: 429" / "HTTP 500" in its stderr, so a
                # rate limit was previously indistinguishable from any other Git
                # failure. Only the numeric code is emitted, never the message.
                reason = _git_http_reason(status)
            else:
                reason = "unclassified Git failure; inspect safely on a trusted system"
        elif isinstance(exc, HTTPError) and type(exc.code) is int and 100 <= exc.code <= 599:
            reason = f"HTTP {exc.code}"
        else:
            reason = "operation failed; inspect safely on a trusted system"
        return f"git {phase}: {reason}"
    if isinstance(exc, HTTPError):
        if type(exc.code) is int and 100 <= exc.code <= 599:
            detail = f"HTTP {exc.code}"
            operation = _safe_operation(exc)
            if operation is not None:
                rate = None
                if exc.code == 403:
                    try:
                        rate = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
                    except Exception:
                        pass
                suffix = "; rate limit remaining=0" if type(rate) is str and rate == "0" else ""
                detail += f" ({operation[0]} {operation[1]} {operation[2]}{suffix})"
            return detail
        return "HTTPError"
    if isinstance(exc, URLError):
        # Transport failures previously reported only the class name, which made
        # a source-host outage indistinguishable from any other error.
        detail = f"URLError ({_network_reason(exc)})"
        operation = _safe_operation(exc)
        if operation is not None:
            detail += f" ({operation[0]} {operation[1]} {operation[2]})"
        return detail

    if type(exc) is RuntimeError and exc.args == ("release asset uploads failed",):
        status = getattr(exc, "mirror_upload_status", None)
        if type(status) is int and 100 <= status <= 599:
            return f"release asset uploads failed (first HTTP {status})"
    if type(exc) is RuntimeError and exc.args == ("asset download failed",):
        status = getattr(exc, "mirror_asset_status", None)
        if type(status) is int and 100 <= status <= 599:
            return f"HTTP {status} (source asset GET)"
    release_errors = getattr(exc, "release_errors", None)
    if isinstance(exc, RuntimeError) and type(release_errors) in (list, tuple) and release_errors:
        return f"{len(release_errors)} releases failed; first: {error_detail(release_errors[0])}"
    if len(exc.args) == 1 and type(exc.args[0]) is str and exc.args[0] in SAFE_ERROR_MESSAGES:
        return exc.args[0]
    for kind in (ValueError, RuntimeError, URLError, OSError, KeyError, TypeError):
        if isinstance(exc, kind):
            return kind.__name__
    return "Error"


def timed_main(fn):
    """Decorator that times the whole run and records the total time."""

    @wraps(fn)
    def wrapped(argv=None):
        started = time.monotonic()
        counter = [0.0]
        context = REPOSITORY_TIME.set(counter)
        try:
            return fn(argv)
        finally:
            total = time.monotonic() - started
            REPOSITORY_TIME.reset(context)
            print(
                f"runtime total elapsed={total:.3f}s overhead={max(0.0, total - counter[0]):.3f}s (outside repository processing)",
                file=sys.stderr,
                flush=True,
            )

    return wrapped
