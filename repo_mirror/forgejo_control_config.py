"""Blob-SHA conditional updates to seeded files in a Forgejo control repo.

The configured repository ID and readback detect replacement; a name-based
Contents API write cannot atomically enforce the repository ID. A blob SHA
protects a file update, not branch history or journal freshness against replay.
"""
import base64
import http.client
import json
import re

from repo_mirror.forgejo_git import APIError, LocalForgejoAPI, _name
from repo_mirror.migrations import blob_sha


_PATHS = ('config.json', 'failures.json', 'migrations.json')
_BRANCH = 'main'
_REF_QUERY = '?ref=refs/heads/main'
_CONTENTS = re.compile(r'/api/v1/repos/[A-Za-z0-9_][A-Za-z0-9_.-]*/'
                       r'[A-Za-z0-9_][A-Za-z0-9_.-]*/contents/'
                       r'(?:config|failures|migrations)\.json')


class ForgejoControlAPI(LocalForgejoAPI):
    """Permit only the extra branch-pinned Contents GET and conditional PUT."""

    def request(self, method, path, payload=None, *, token=None):
        if method == 'GET' and path.endswith(_REF_QUERY) and _CONTENTS.fullmatch(path[:-len(_REF_QUERY)]):
            if payload is not None:
                raise ValueError('invalid Forgejo Contents request')
        elif method == 'PUT' and _CONTENTS.fullmatch(path):
            if (not isinstance(payload, dict) or payload.get('branch') != _BRANCH
                    or not isinstance(payload.get('sha'), str)
                    or not re.fullmatch(r'[0-9a-f]{40}', payload['sha'])):
                raise ValueError('invalid Forgejo Contents request')
        else:
            return super().request(method, path, payload, token=token)
        # The superclass validates a loopback-only origin. HTTPConnection makes
        # one direct request: no proxy lookup and no redirects, even on 307/308.
        conn = http.client.HTTPConnection(self.host, self.port, timeout=30)
        try:
            conn.request(method, path, body=json.dumps(payload).encode() if payload is not None else None,
                         headers={'Authorization': 'token ' + (self.token() if token is None else token),
                                  'Content-Type': 'application/json'})
            response = conn.getresponse()
            if not 200 <= response.status < 300:
                raise APIError(response.status)
            data = response.read()
            return json.loads(data) if data else None
        finally:
            conn.close()


class ForgejoFile:
    """Read and conditionally replace one pre-existing control file on main."""

    def __init__(self, api, owner, repo, path, *, expected_id=None):
        self.identity = f'{_name(owner)}/{_name(repo)}'
        if path not in _PATHS:
            raise ValueError('unsupported Forgejo control file')
        if expected_id is not None and (type(expected_id) is not int or expected_id <= 0):
            raise ValueError('invalid Forgejo control repository ID')
        self.api = api
        self.expected_id = expected_id
        self.repo_path = f'/api/v1/repos/{self.identity}'
        self.file_path = f'{self.repo_path}/contents/{path}'

    def _verify_repository(self):
        repo = self.api.request('GET', self.repo_path)
        if (not isinstance(repo, dict) or type(repo.get('id')) is not int or repo['id'] <= 0
                or (self.expected_id is not None and repo['id'] != self.expected_id)
                or not isinstance(repo.get('full_name'), str)
                or repo['full_name'].lower() != self.identity.lower()
                or repo.get('private') is not True or repo.get('has_actions') is not False):
            raise ValueError('Forgejo control repository identity, privacy or Actions not verified')
        self.expected_id = repo['id']

    def _read(self):
        self._verify_repository()
        response = self.api.request('GET', self.file_path + _REF_QUERY)
        if (not isinstance(response, dict) or response.get('type') != 'file'
                or response.get('encoding') != 'base64'
                or not isinstance(response.get('content'), str)):
            raise ValueError('invalid Forgejo control file response')
        try:
            data = base64.b64decode(response['content'].replace('\n', ''), validate=True)
        except ValueError:
            raise ValueError('invalid Forgejo control file encoding') from None
        sha = blob_sha(data)
        if sha != response.get('sha'):
            raise ValueError('Forgejo control file blob mismatch')
        self._verify_repository()
        return data, sha

    def read(self):
        return self._read()[0]

    def replace(self, before, after):
        if not isinstance(before, bytes) or not isinstance(after, bytes):
            raise ValueError('invalid Forgejo control file replacement')
        current, sha = self._read()
        if current != before:
            raise ValueError('Forgejo control file changed')
        if before == after:
            return
        try:
            self.api.request('PUT', self.file_path, {
                'message': 'Update mirror control file', 'branch': _BRANCH, 'sha': sha,
                'content': base64.b64encode(after).decode('ascii'),
            })
        except APIError as exc:
            if exc.status == 409:
                raise ValueError('Forgejo control file changed') from None
            raise
        if self.read() != after:
            raise ValueError('Forgejo control file publication readback mismatch')


class ForgejoMigrations(ForgejoFile):
    """Store the signed journal bytes produced and verified by MigrationsStore."""

    def __init__(self, api, owner, repo, *, expected_id=None):
        super().__init__(api, owner, repo, 'migrations.json', expected_id=expected_id)

    def load_bytes(self):
        # This file must be seeded. A 404 does not authorize treating left-over
        # transactions as absent or recreating an empty journal.
        return self.read()

    def publish(self, expected, replacement):
        self.replace(expected, replacement)
