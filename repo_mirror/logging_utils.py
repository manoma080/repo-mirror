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


_ARCHIVE_SUFFIX = re.compile(r"(?:-[0-9a-f]{16})?-wipe-\d{8}T\d{6}Z\Z")


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
    entropy_target = _ARCHIVE_SUFFIX.sub("", value)
    if not entropy_target:
        return "[redacted]"
    # Catch long, opaque hyphenated runs without treating every long project
    # name as a credential. This bounded heuristic cannot recognize all secrets.
    for run in re.findall(r"[A-Za-z0-9_-]{32,}", entropy_target):
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
    def wrapped(source, name, owner, src, dst, token, account_type="organization", journal=None, **kwargs):
        context = LOG_SECRETS.set((token,))
        started = time.monotonic()
        try:
            with timed(f"repository source_repo={safe_label(source.get('owner'))}/{safe_label(name)}"):
                return fn(source, name, owner, src, dst, token, account_type, journal, **kwargs)
        finally:
            elapsed = time.monotonic() - started
            counter = REPOSITORY_TIME.get()
            if counter is not None:
                counter[0] += elapsed
            LOG_SECRETS.reset(context)

    return wrapped


SAFE_ERROR_MESSAGES = frozenset(
    {
        "legacy configuration format is no longer supported",
        "source type must be github, forgejo or gitlab",
        "source base_url must be credential-free HTTPS",
        "invalid owner",
        "invalid repository name",
        "invalid discovered repository name",
        "invalid destination repository name",
        "destination collision: repository has no matching source marker",
        "unsafe redirect",
        "API URL must be credential-free HTTPS",
        "refusing to send destination credentials to non-GitHub host",
        "request retries exhausted",
        "retry wait limit exceeded",
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
        "repositories must be a list",
        "duplicate destination repository",
        "auto-discovery returned zero repositories for a rule with existing entries",
        "source owner identity mismatch",
        "invalid source owner metadata",
        "invalid discovered repository metadata",
        "duplicate discovered repository",
        "source repository id mismatch or missing",
        "existing source mapping conflicts",
        "unpinned existing destination requires manual reconciliation",
        "created destination repository identity not verified",
        "destination Actions not verified disabled",
        "invalid created destination repository id",
        "authenticated GitHub user does not match configured destination",
        "GitHub App installation owner does not match configured destination",
        "source GitHub App owner is required",
        "GitHub App installation id is required",
        "GitHub App helper path is required",
        "GITHUB_TOKEN required; run under the GitHub App helper",
        "destination creation may be partial; reconcile manually",
        "auto-discovery onboarding publication failed; reconcile manually",
        "auto-discovery onboarding requires authoritative management configuration",
        "authoritative configuration differs from checkout",
        "configuration readback mismatch",
        "pending migration requires reconciliation",
        "Forgejo repository identity, provenance, privacy or Actions not verified",
        "authenticated Forgejo user does not match configured destination",
        "destination changed during creation",
        "Forgejo control file changed",
        "Forgejo control file publication readback mismatch",
        "Forgejo control repository identity, privacy or Actions not verified",
        "ambiguous owner or repository URL",
        "ambiguous source URL matches both an auto-discovery rule and a repository entry",
        "destination name is derived per repository for a discovery rule",
        "overlapping auto-discovery rule and child repository URL",
        "one-time mirroring is not supported for owner URLs; enable add_to_config",
        "duplicate auto-discovery rule",
        "duplicate group declaration in groups and auto_discoveries",
        "destination_name_prefix is not allowed on a rule with existing generated gitlab destinations",
        "unknown or unsupported source repository URL",
        "cannot adopt a repository with a configured destination name",
        "could not confirm upstream repository deletion",
        "cannot adopt a repository without a pinned destination id",
        "failed to archive existing destination",
        "adoption destination identity mismatch",
        "adoption archive name already exists",
        "adoption destination rename verification failed",
        "adoption publication failed; archived destinations left in place; reconcile manually",
        "invalid archive identity",
        # Migration transaction failures: fixed reasons only, never free text.
        "invalid migration attestation",
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
        # Scheduler and sync fixed configuration errors.
        "invalid local configuration authority",
        "invalid manual dispatch",
        "Forgejo asset byte limit must be configured",
        "destination account type mismatch",
        "invalid installation or port",
        "invalid repository timeout",
        "incomplete app credentials",
        "invalid configuration repository",
        "invalid recovery secret file",
        "recovery secret too short",
        "recovery secret must be separate",
        "invalid journal directory",
        "lock must be an owner-only regular file",
        "configuration changed since fetch",
        "invalid port",
        "invalid source URL",
        "duplicate configured source",
        "duplicate requested source",
        "conflicting destination",
        "requested source not exactly configured",
        "invalid GitHub App configuration",
        "GraphQL request rate limited",
        "GraphQL request forbidden",
        "GraphQL resource not found",
        "GraphQL query node limit exceeded",
        "GraphQL request returned errors",
        "invalid GraphQL response",
        "GraphQL query fan-out exceeds configured limit",
        "invalid GraphQL query parameters",
        "GraphQL pagination exceeded safety limit",
        "GraphQL pagination cycle detected",
        "invalid GraphQL pagination cursor",
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
                and operation[1] in ("API", "asset", "graphql")
                and operation[2] in ("GET", "POST", "PUT", "PATCH", "DELETE", "request")
            )
        )
    ):
        return operation
    return None


_GIT_HTTP_STATUS = re.compile(
    r"\bhttp(?:\s*error)?\s*[;:(]?\s*(\d{3})\b|returned error:\s*(\d{3})\b"
)

# GitHub rejects any single push whose pack exceeds 2 GiB. Depending on where
# the server aborts, Git reports this as "pack exceeds maximum allowed size",
# "push cannot exceed 2GB", or only as a closed connection. The last form is
# indistinguishable from a real network fault, so it is worded as both.
_PUSH_LIMIT_SIGNATURES = (
    "pack exceeds maximum allowed size",
    "push cannot exceed 2gb",
    "push cannot exceed 2 gb",
)
_PUSH_TRANSPORT_SIGNATURE = "remote end hung up unexpectedly"


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


def _http_error_detail(exc):
    """Build a redacted HTTP status description with optional safe operation and rate-limit suffix."""
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
            elif any(s in text for s in _PUSH_LIMIT_SIGNATURES):
                reason = "push exceeds 2 GB limit; split the history into smaller pushes"
            elif phase == "snapshot push" and _PUSH_TRANSPORT_SIGNATURE in text:
                reason = (
                    "push connection closed by the destination (a history larger than GitHub's 2 GiB "
                    "single-push limit, or connectivity); retry after a fresh safety check"
                )
            elif any(s in text for s in ("permission denied", "403", "authentication failed", "access denied", "write access to repository not granted")):
                reason = "permission or authentication failure; check repository access and token permissions"
            elif "non-fast-forward" in text or "fetch first" in text:
                reason = "push rejected as non-fast-forward; the destination advanced during the push; retry after a fresh safety check"
            elif any(s in text for s in ("could not resolve host", "connection timed out", "connection reset", "failed to connect")) or (
                phase != "snapshot push" and _PUSH_TRANSPORT_SIGNATURE in text
            ):
                reason = "network failure; check connectivity and retry after a fresh safety check"
            elif (status := _git_http_status(text)) is not None:
                # Git only reports "error: 429" / "HTTP 500" in its stderr, so a
                # rate limit was previously indistinguishable from any other Git
                # failure. Only the numeric code is emitted, never the message.
                reason = _git_http_reason(status)
            else:
                reason = "unclassified Git failure; inspect safely on a trusted system"
        elif isinstance(exc, HTTPError) and type(exc.code) is int and 100 <= exc.code <= 599:
            reason = _http_error_detail(exc)
        elif (
            isinstance(exc, RuntimeError)
            and len(exc.args) == 1
            and type(exc.args[0]) is str
            and exc.args[0] in SAFE_ERROR_MESSAGES
        ):
            reason = exc.args[0]
        else:
            reason = "operation failed; inspect safely on a trusted system"
        return f"git {phase}: {reason}"
    if isinstance(exc, HTTPError):
        if type(exc.code) is int and 100 <= exc.code <= 599:
            return _http_error_detail(exc)
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
        from repo_mirror.github_api import emit_api_call_summary, reset_api_call_metrics

        reset_api_call_metrics()
        started = time.monotonic()
        counter = [0.0]
        context = REPOSITORY_TIME.set(counter)
        try:
            return fn(argv)
        finally:
            emit_api_call_summary()
            total = time.monotonic() - started
            REPOSITORY_TIME.reset(context)
            print(
                f"runtime total elapsed={total:.3f}s overhead={max(0.0, total - counter[0]):.3f}s (outside repository processing)",
                file=sys.stderr,
                flush=True,
            )

    return wrapped
