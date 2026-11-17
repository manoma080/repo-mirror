"""Publication backends for the durable migration journal.

The GitHub backend writes `migrations.json` on the management default branch
with blob-SHA conditional updates and a mandatory readback. It is the only
durable store available to the ephemeral GitHub runner, and the Forgejo
scheduler can publish the same file so both runners agree on what is pending.

A conditional write is not a transaction: it only detects that the file changed
between read and write. Callers must still recheck repository identities before
mutating anything remote.
"""
import base64
import binascii
import hashlib
import json
import os
import re
import tempfile
from urllib.error import HTTPError
from urllib.parse import quote

from repo_mirror.migrations_journal import MigrationsJournal, journal_key, parse


class FileMigrations:
    """Owner-controlled local file, used by the Forgejo runner."""

    def __init__(self, path):
        self.path = os.fspath(path)

    def load_bytes(self):
        try:
            with open(self.path, 'rb') as stream:
                return stream.read()
        except FileNotFoundError:
            return None

    def publish(self, expected, replacement):
        current = self.load_bytes()
        if current != expected:
            raise ValueError('migrations journal changed')
        directory = os.path.dirname(os.path.abspath(self.path)) or '.'
        fd, name = tempfile.mkstemp(prefix='.migrations-', dir=directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(replacement)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dirfd)
            finally:
                os.close(dirfd)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        if self.load_bytes() != replacement:
            raise ValueError('migrations journal readback mismatch')


class GitHubMigrations:
    """Contents API backend for the authoritative management repository."""

    def __init__(self, api, owner, repo, branch='main'):
        if (not all(isinstance(x, str) and re.fullmatch(r'[A-Za-z0-9_.-]+', x)
                    and x not in ('.', '..') for x in (owner, repo)) or branch != 'main'):
            raise ValueError('invalid migrations location')
        self.api = api
        self.branch = branch
        self.url = (f'https://api.github.com/repos/{quote(owner, safe="")}/'
                    f'{quote(repo, safe="")}/contents/migrations.json')

    def _read(self):
        try:
            response = self.api.request(
                'GET', self.url + '?ref=' + quote(self.branch, safe=''))
        except HTTPError as exc:
            if exc.code == 404:
                exc.close()
                return None, None
            raise
        if (not isinstance(response, dict) or response.get('type') != 'file'
                or response.get('encoding') != 'base64'
                or not isinstance(response.get('content'), str)
                or not isinstance(response.get('sha'), str)
                or not re.fullmatch(r'[0-9a-f]{40}', response['sha'])):
            raise ValueError('invalid migrations response')
        try:
            raw = base64.b64decode(response['content'].replace('\n', ''), validate=True)
        except (ValueError, binascii.Error):
            raise ValueError('invalid migrations encoding') from None
        if hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() != response['sha']:
            raise ValueError('migrations blob mismatch')
        return raw, response['sha']

    def load_bytes(self):
        return self._read()[0]

    def publish(self, expected, replacement):
        for _ in range(5):
            current, sha = self._read()
            if current != expected:
                raise ValueError('migrations journal changed')
            payload = {'message': 'Update mirror migration journal', 'branch': self.branch,
                       'content': base64.b64encode(replacement).decode('ascii')}
            if sha is not None:
                payload['sha'] = sha
            try:
                self.api.request('PUT', self.url, payload)
            except HTTPError as exc:
                # A conflict means another writer replaced the file; reload and
                # retry only if the caller's expectation is still current.
                if exc.code != 409:
                    raise
                exc.close()
                continue
            if self.load_bytes() != replacement:
                raise ValueError('migrations journal publication readback mismatch')
            return
        raise ValueError('migrations journal conflict retry limit exceeded')


class MigrationsStore:
    """Read, record and advance migration transactions in a backend."""

    def __init__(self, backend, secret):
        self.backend = backend
        self.secret = secret

    def load(self):
        raw = self.backend.load_bytes()
        if raw is None:
            return MigrationsJournal(None, self.secret), None
        return parse(raw, self.secret), raw

    def record(self, entry):
        journal, raw = self.load()
        updated = journal.put(entry)
        self._publish(raw, updated)
        return updated

    def advance(self, source_origin, source_id, entry):
        journal, raw = self.load()
        updated = journal.advance(source_origin, source_id, entry)
        self._publish(raw, updated)
        return updated

    def complete(self, source_origin, source_id):
        journal, raw = self.load()
        updated = journal.remove(source_origin, source_id)
        self._publish(raw, updated)
        return updated

    def pending(self):
        return self.load()[0].pending()

    def _publish(self, raw, journal):
        self.backend.publish(raw, journal.encode())

    def get(self, source_origin, source_id):
        return self.load()[0].get(source_origin, source_id)


def build_entry(record):
    """Assemble a validated journal entry from a caller's migration plan.

    `record` must supply the pinned source and destination identities, the
    original recovery binding and the configuration base blob SHA.
    """
    entry = {
        'source': record['source'],
        'destination': record['destination'],
        'recovery': record['recovery'],
        'config': record['config'],
        'phase': record.get('phase', 'prepared'),
    }
    # Validate eagerly so an incomplete plan never reaches the journal.
    MigrationsJournal(None, 'x' * 32).put(entry)
    return entry
