"""Replicate Forgejo worker journals to one authoritative failures.json.

The caller must hold its batch lock and drain its workers before leaving the
session. The remote must implement conditional replace against the exact bytes
returned by read() (None means absent). Keep the journal directory persistent.
"""

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager

from repo_mirror.config_json import unique_pairs


MAX_BYTES = 4 * 1024 * 1024
_FENCE = '.failure-remote-state.json'
_LOCK = '.failure-remote.lock'
_DIGEST = r'[0-9a-f]{64}'
_JOURNAL = re.compile(rf'(?:dispatch-)?{_DIGEST}\.json')
_KEY = re.compile(rf'{_DIGEST}:{_DIGEST}')


def _bad_constant(_):
    raise ValueError('invalid JSON constant')


def _decode(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ValueError('invalid or oversized failure snapshot')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique_pairs,
                          parse_constant=_bad_constant)
    except (ValueError, RecursionError):
        raise ValueError('invalid failure snapshot JSON') from None


def _encode(data):
    try:
        raw = (json.dumps(data, sort_keys=True, indent=2, ensure_ascii=True,
                          allow_nan=False) + '\n').encode('ascii')
    except (ValueError, RecursionError):
        raise ValueError('invalid failure snapshot JSON') from None
    if len(raw) > MAX_BYTES:
        raise ValueError('oversized failure snapshot')
    return raw


def _journal(data):
    version = data.get('version') if isinstance(data, dict) else None
    if (type(version) is not int or version not in (1, 2)
            or set(data) != ({'version', 'failures'} if version == 1
                             else {'version', 'failures', 'intents'})
            or not isinstance(data['failures'], list)
            or any(not isinstance(item, str) or not _KEY.fullmatch(item)
                   for item in data['failures'])):
        raise ValueError('invalid worker failure journal')
    if version == 2:
        # Match the existing FailureJournal envelope. Its signed contents are
        # interpreted and authenticated by the worker, not by the synchronizer.
        if not isinstance(data['intents'], dict) or any(
                not re.fullmatch(_DIGEST, key) for key in data['intents']):
            raise ValueError('invalid worker failure journal intents')
    return data


def _remote_documents(raw):
    if raw is None:
        return {}
    value = _decode(raw)
    if (not isinstance(value, dict) or set(value) != {'version', 'journals'}
            or type(value['version']) is not int or value['version'] != 1
            or not isinstance(value['journals'], dict)
            or any(not _JOURNAL.fullmatch(name) for name in value['journals'])):
        raise ValueError('invalid remote failure snapshot')
    documents = {name: _journal(data) for name, data in value['journals'].items()}
    # Ensure the canonical representation will fit before restoring anything.
    _snapshot(documents)
    return documents


@contextmanager
def _directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('failure journal directory must be owner-only')
        yield fd
    finally:
        os.close(fd)


def _regular(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o022):
        raise ValueError('untrusted failure journal file')
    return info


def _read(directory, name, limit=MAX_BYTES):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        if _regular(fd).st_size > limit:
            raise ValueError('oversized failure journal file')
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('oversized failure journal file')
    return raw


def _local_documents(directory):
    documents = {}
    remaining = MAX_BYTES
    listing = os.open('.', os.O_RDONLY | os.O_DIRECTORY, dir_fd=directory)
    try:
        names = sorted(os.listdir(listing))
    finally:
        os.close(listing)
    for name in names:
        if not _JOURNAL.fullmatch(name):
            continue
        raw = _read(directory, name, remaining)
        if raw is None:
            raise ValueError('failure journal changed during snapshot')
        documents[name] = _journal(_decode(raw))
        remaining -= len(raw)
    return documents


def _snapshot(documents):
    return _encode({'version': 1, 'journals': documents})


def snapshot(directory):
    """Return canonical bytes without including locks or migration metadata files."""
    with _directory(directory) as fd:
        return _snapshot(_local_documents(fd))


def _atomic_write(directory, name, raw, *, create=False):
    temp = '.failure-remote-tmp-' + secrets.token_hex(16)
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            # The caller's batch lock excludes journal writers during restore.
            # Use a rename, not link/unlink: a crash between those operations
            # would leave the restored file with an untrusted link count.
            try:
                os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise _diverged()
        os.replace(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(temp, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.fsync(directory)


@contextmanager
def _exclusive(directory):
    fd = os.open(_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                 0o600, dir_fd=directory)
    try:
        _regular(fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _digest(raw):
    return None if raw is None else hashlib.sha256(raw).hexdigest()


def _state(directory):
    raw = _read(directory, _FENCE, 1024)
    if raw is None:
        return None
    state = _decode(raw)
    if (not isinstance(state, dict) or set(state) != {'phase', 'base', 'target'}
            or state['phase'] not in ('restore', 'active', 'publish')
            or state['phase'] != 'publish' and state['target'] is not None
            or state['phase'] == 'publish' and state['target'] is None
            or any(item is not None and (not isinstance(item, str) or not re.fullmatch(_DIGEST, item))
                   for item in (state['base'], state['target']))):
        raise ValueError('invalid local failure sync fence')
    return state


def _save(directory, phase, base, target=None):
    _atomic_write(directory, _FENCE, _encode({'phase': phase, 'base': base, 'target': target}))


def _diverged():
    return ValueError('failure journals diverged; preserve local files and reconcile failures.json before retrying')


def _publish(directory, remote, before, after):
    if before != after:
        _save(directory, 'publish', _digest(before), _digest(after))
        remote.replace(before, after)
    if remote.read() != after:
        raise _diverged()


def _start(directory, remote):
    state = _state(directory)
    before = remote.read()
    authoritative = _remote_documents(before)
    local = _local_documents(directory)
    if state is None or state['phase'] == 'restore':
        if state is not None and state['base'] != _digest(before):
            raise _diverged()
        if any(name not in authoritative or _encode(data) != _encode(authoritative[name])
               for name, data in local.items()):
            raise _diverged()
        missing = authoritative.keys() - local.keys()
        if missing:
            # A partial restore is trusted only when this exact remote base was
            # fenced before the first journal was written.
            if local and state is None:
                raise _diverged()
            _save(directory, 'restore', _digest(before))
            for name in sorted(missing):
                _atomic_write(directory, name, _encode(authoritative[name]), create=True)
    else:
        after = _snapshot(local)
        if state['phase'] == 'publish' and state['target'] != _digest(after):
            raise _diverged()
        if state['base'] == _digest(before):
            if not authoritative.keys() <= local.keys():
                raise _diverged()
            _publish(directory, remote, before, after)
            before = after
        elif state['phase'] != 'publish' or before != after:
            raise _diverged()
    if remote.read() != before:
        raise _diverged()
    _save(directory, 'active', _digest(before))
    return before


@contextmanager
def session(directory, remote):
    """Restore/recover before yielding; conditionally publish on normal exit.

    Exceptional exits preserve exact local files and the durable fence. A
    subsequent invocation must reconcile those files before starting a worker.
    A failed batch that returns normally (including nonzero) still publishes.
    """
    with _directory(directory) as fd, _exclusive(fd):
        before = _start(fd, remote)
        yield
        local = _local_documents(fd)
        if not _remote_documents(before).keys() <= local.keys():
            raise _diverged()
        _publish(fd, remote, before, _snapshot(local))
        os.unlink(_FENCE, dir_fd=fd)
        os.fsync(fd)
