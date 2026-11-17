"""journal: see README for an overview."""

import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
import threading
from contextlib import contextmanager

from repo_mirror.config_json import unique_pairs
from repo_mirror.migration_intent import IntentStore, _DOMAIN, _PHASES


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
        self._lock = threading.RLock()
        with self._locked():
            self._last_failures = set(self._read()['failures'])

    @contextmanager
    def _locked(self):
        with self._lock:
            fd = os.open(self.path + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError('untrusted journal lock')
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def _read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, encoding='utf-8') as stream:
                data = json.load(stream, object_pairs_hook=unique_pairs)
        except FileNotFoundError:
            return {'version': 1, 'failures': []}
        except (json.JSONDecodeError, TypeError):
            raise ValueError('invalid failure journal') from None
        if (not isinstance(data, dict) or data.get('version') not in (1, 2)
                or set(data) != ({'version', 'failures'} if data.get('version') == 1 else {'version', 'failures', 'intents'})
                or not isinstance(data.get('failures'), list)
                or any(not isinstance(x, str) or not re.fullmatch(r'[0-9a-f]{64}:[0-9a-f]{64}', x) for x in data['failures'])
                or (data['version'] == 2 and (not isinstance(data['intents'], dict)
                    or any(not re.fullmatch(r'[0-9a-f]{64}', k) for k in data['intents'])))):
            raise ValueError('invalid failure journal')
        return data

    def _write(self, data):
        directory = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(prefix='.failures-', dir=directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(data, stream, indent=2, sort_keys=True)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
            dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def intent_store(self, secret, identifier):
        return JournalIntentStore(self, secret, identifier)

    @property
    def failures(self):
        try:
            with self._locked():
                self._last_failures = set(self._read()['failures'])
                return self._last_failures.copy()
        except FileNotFoundError:
            if os.path.isdir(os.path.dirname(os.path.abspath(self.path))):
                raise
            return self._last_failures.copy()

    @staticmethod
    def key(source, name, owner, tag, asset):
        identity = [source["type"], source["base_url"].rstrip("/").lower(), source["owner"], name, owner, tag]
        digest = lambda value: hashlib.sha256(
            json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
        ).hexdigest()
        return digest(identity) + ":" + digest(asset)

    @staticmethod
    def id_key(source_origin, source_id, destination_origin, destination_id, tag, asset):
        if (not all(isinstance(x, str) and x for x in (source_origin, destination_origin, tag))
                or not isinstance(asset, str)
                or any(isinstance(x, bool) or not isinstance(x, int) or x <= 0 for x in (source_id, destination_id))):
            raise ValueError('invalid stable failure identity')
        def digest(value):
            return hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
        return digest(['id-v1', source_origin.rstrip('/').lower(), source_id,
                       destination_origin.rstrip('/').lower(), destination_id, tag]) + ':' + digest(asset)

    def migrate_failure(self, legacy_key, stable_key):
        if any(not isinstance(k, str) or not re.fullmatch(r'[0-9a-f]{64}:[0-9a-f]{64}', k)
               for k in (legacy_key, stable_key)):
            raise ValueError('invalid failure key')
        with self._locked():
            data = self._read()
            failures = set(data['failures'])
            if legacy_key not in failures:
                return False
            failures.remove(legacy_key)
            failures.add(stable_key)
            data['failures'] = sorted(failures)
            self._write(data)
            self._last_failures = failures
            return True

    def retry_order(self, source, name, owner, tag, assets):
        failures = self.failures
        return sorted(
            assets, key=lambda asset: self.key(source, name, owner, tag, asset["name"]) not in failures
        )

    def set(self, key, failed):
        if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{64}:[0-9a-f]{64}', key):
            raise ValueError('invalid failure key')
        with self._locked():
            data = self._read()
            failures = set(data['failures'])
            if failed:
                failures.add(key)
            else:
                failures.discard(key)
            data['failures'] = sorted(failures)
            self._write(data)
            self._last_failures = failures


class JournalIntentStore(IntentStore):
    """IntentStore API backed by a shared failure journal transaction."""

    def __init__(self, journal, secret, identifier):
        super().__init__(journal.path, secret)
        if not isinstance(identifier, str) or not identifier:
            raise ValueError('invalid intent identifier')
        self.journal = journal
        self.identifier = hashlib.sha256(identifier.encode()).hexdigest()

    @contextmanager
    def _lock(self):
        with self.journal._locked():
            yield

    def _load(self):
        data = self.journal._read()
        intents = data.get('intents', {})
        if self.identifier not in intents:
            return None
        envelope = intents[self.identifier]
        if not isinstance(envelope, dict) or set(envelope) != {'intent', 'signature'}:
            raise ValueError('invalid intent')
        intent, signature = envelope['intent'], envelope['signature']
        try:
            expected = hmac.new(self.secret, _DOMAIN + self.identifier.encode() + self._encode(intent), hashlib.sha256).hexdigest()
            if (not isinstance(signature, str) or not hmac.compare_digest(signature, expected)
                    or not isinstance(intent, dict) or set(intent) != {'binding', 'phase'}
                    or not isinstance(intent['binding'], dict) or not intent['binding'] or intent['phase'] not in _PHASES):
                raise ValueError('invalid intent')
        except (TypeError, KeyError):
            raise ValueError('invalid intent') from None
        return intent

    def _write(self, intent):
        data = self.journal._read()
        data['version'] = 2
        intents = data.setdefault('intents', {})
        signature = hmac.new(self.secret, _DOMAIN + self.identifier.encode() + self._encode(intent), hashlib.sha256).hexdigest()
        intents[self.identifier] = {'intent': intent, 'signature': signature}
        self.journal._write(data)


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
