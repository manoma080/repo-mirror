"""journal: see README for an overview."""

import hashlib
import json
import os
import re
import tempfile
import threading


class ReleaseRecoveryError(RuntimeError):
    """Error raised when a pending release needs manual review before the mirror can continue."""

    MESSAGES = {
        "marker": "invalid pending release marker; manually inspect the pending GitHub release and reconcile it before retrying",
        "secret": "MIRROR_TIMESTAMP_SECRET is required for v3 release recovery",
        "changed": "source release assets changed during pending sync; manually review the pending GitHub release and source, reconcile or recreate the release, then retry",
        "mismatch": "release asset mismatch; refusing to delete or replace; manually compare and reconcile the pending GitHub release assets before retrying",
    }

    def __init__(self, reason):
        super().__init__(self.MESSAGES[reason])


class StarterAssetError(ReleaseRecoveryError):
    """Error raised when a release only has starter assets that need manual handling."""

    def __init__(self):
        RuntimeError.__init__(
            self,
            "starter release asset detected; manually remove the starter asset from the pending GitHub release, then rerun",
        )


class FailureJournal:
    """Persist only hashed identifiers, never URLs, error messages or credentials."""

    def __init__(self, path):
        self.path = os.fspath(path)
        if os.path.islink(self.path):
            raise ValueError("failure journal must not be a symlink")
        try:
            with open(self.path, encoding="utf-8") as stream:
                data = json.load(stream)
        except FileNotFoundError:
            data = {"version": 1, "failures": []}
        if (
            not isinstance(data, dict)
            or set(data) != {"version", "failures"}
            or data["version"] != 1
            or not isinstance(data["failures"], list)
            or any(
                not isinstance(x, str) or not re.fullmatch(r"[0-9a-f]{64}:[0-9a-f]{64}", x)
                for x in data["failures"]
            )
        ):
            raise ValueError("invalid failure journal")
        self._lock = threading.RLock()
        self._failures = set(data["failures"])

    @property
    def failures(self):
        with self._lock:
            return self._failures.copy()

    @staticmethod
    def key(source, name, owner, tag, asset):
        identity = [source["type"], source["base_url"].rstrip("/").lower(), source["owner"], name, owner, tag]
        digest = lambda value: hashlib.sha256(
            json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
        ).hexdigest()
        return digest(identity) + ":" + digest(asset)

    def retry_order(self, source, name, owner, tag, assets):
        failures = self.failures
        return sorted(
            assets, key=lambda asset: self.key(source, name, owner, tag, asset["name"]) not in failures
        )

    def set(self, key, failed):
        with self._lock:
            self._set_locked(key, failed)

    def _set_locked(self, key, failed):
        if failed:
            self._failures.add(key)
        else:
            self._failures.discard(key)
        directory = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(prefix=".failures-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "failures": sorted(self.failures)}, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def compare_reason(previous, source_hash, target_hash, ident, entry):
    """Explain why two release states differ, for the failure journal."""
    reasons = []
    if not previous:
        reasons.append("no-destination-asset")
    if not isinstance(source_hash, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", source_hash):
        reasons.append("no-source-digest")
    if not isinstance(target_hash, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", target_hash):
        reasons.append("no-destination-digest")
    if ident and not isinstance(entry, dict):
        reasons.append("no-manifest-entry")
    if ident and isinstance(entry, dict) and entry.get("timestamp") != ident.get("timestamp"):
        reasons.append("timestamp-mismatch")
    if ident is None:
        reasons.append("manifest-unavailable")
    return ",".join(reasons) or "digest-size-or-identity-mismatch"
