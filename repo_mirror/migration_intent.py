"""Authenticated, crash-durable migration intent storage.

An intent records *what was authorized*, not evidence that a remote operation
succeeded. Callers must re-read and verify remote identities at every phase.
"""
import fcntl
import hashlib
import hmac
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from repo_mirror.config_json import unique_pairs

_PHASES = ('prepared', 'synced', 'renamed', 'configured')
_DOMAIN = b'repo-mirror:migration-intent:v1\0'


class IntentStore:
    def __init__(self, path, secret):
        self.path = Path(path)
        if not isinstance(secret, str) or len(secret.encode()) < 32:
            raise ValueError('invalid intent secret')
        self.secret = secret.encode()

    @staticmethod
    def _encode(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')

    @contextmanager
    def _lock(self):
        fd = os.open(str(self.path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('untrusted intent lock')
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _load(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, encoding='utf-8') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError('untrusted intent file')
                envelope = json.load(stream, object_pairs_hook=unique_pairs)
            if not isinstance(envelope, dict) or set(envelope) != {'intent', 'signature'}:
                raise ValueError('invalid intent')
            intent, signature = envelope['intent'], envelope['signature']
            expected = hmac.new(self.secret, _DOMAIN + self._encode(intent), hashlib.sha256).hexdigest()
            if (not isinstance(signature, str) or not hmac.compare_digest(signature, expected)
                    or not isinstance(intent, dict) or set(intent) != {'binding', 'phase'}
                    or not isinstance(intent['binding'], dict) or intent['phase'] not in _PHASES):
                raise ValueError('invalid intent')
            return intent
        except FileNotFoundError:
            return None
        except (TypeError, KeyError, json.JSONDecodeError):
            raise ValueError('invalid intent') from None

    def load(self):
        with self._lock():
            return self._load()

    def _write(self, intent):
        signature = hmac.new(self.secret, _DOMAIN + self._encode(intent), hashlib.sha256).hexdigest()
        data = self._encode({'intent': intent, 'signature': signature}) + b'\n'
        fd, name = tempfile.mkstemp(prefix='.migration-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            dirfd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def begin(self, binding):
        if not isinstance(binding, dict) or not binding:
            raise ValueError('invalid intent binding')
        # Freeze nested input and reject values not representable in JSON.
        binding = json.loads(self._encode(binding))
        with self._lock():
            current = self._load()
            if current is not None:
                if current['binding'] != binding:
                    raise ValueError('intent binding mismatch')
                return current
            current = {'binding': binding, 'phase': 'prepared'}
            self._write(current)
            return current

    def advance(self, expected, target):
        if expected not in _PHASES or target not in _PHASES or _PHASES.index(target) != _PHASES.index(expected) + 1:
            raise ValueError('invalid intent phase transition')
        with self._lock():
            current = self._load()
            if current is None or current['phase'] != expected:
                raise ValueError('intent phase mismatch')
            current['phase'] = target
            self._write(current)
            return current
