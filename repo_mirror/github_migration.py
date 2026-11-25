"""GitHub wiring for the post-batch migration transaction.

The GitHub runner has no durable local disk, so both the migration journal and
the mirror configuration live in the management repository and are read and
written through the Contents API with blob-SHA conditional writes. A conflict
is never retried blindly: it means another writer changed the document, and the
transaction stops for review.

Environment contract (the workflow supplies it):

* ``MIRROR_MANAGEMENT_REPOSITORY`` (fallback ``GITHUB_REPOSITORY``) - the
  ``owner/name`` of the repository holding ``config.json`` and
  ``migrations.json``.
* ``MIRROR_MANAGEMENT_TOKEN`` (fallback ``MANAGEMENT_TOKEN``, then
  ``GH_TOKEN``) - a token with contents write access.
* ``MIRROR_TIMESTAMP_SECRET`` - the recovery signing secret, at least 32 bytes.

When any of them is missing, ``transaction_from_environment`` returns ``None``
and the batch reports the relocation without renaming anything.
"""
import base64
import binascii
import os
import re
from urllib.error import HTTPError
from urllib.parse import quote

from repo_mirror.constants import GITHUB, NAME
from repo_mirror.github_api import API
from repo_mirror.migrations import (
    CONFIG_PATHS,
    MigrationTransaction,
    blob_sha,
)
from repo_mirror.migrations_store import GitHubMigrations, MigrationsStore


def _location(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', value)
            or value in ('.', '..')):
        raise ValueError('invalid management repository location')
    return value


class GitHubConfigFile:
    """Byte-exact contents-API CAS over one JSON document."""

    def __init__(self, api, owner, repo, path='config.json', branch='main'):
        _location(owner)
        _location(repo)
        if path not in CONFIG_PATHS:
            raise ValueError('unsupported configuration path')
        if branch != 'main':
            raise ValueError('unsupported management branch')
        self.api = api
        self.branch = branch
        self.url = (f'{GITHUB}/repos/{quote(owner, safe="")}/{quote(repo, safe="")}'
                    f'/contents/{quote(path, safe="")}')

    def _read(self):
        try:
            response = self.api.request('GET', self.url + '?ref=' + quote(self.branch, safe=''))
        except HTTPError as exc:
            if exc.code == 404:
                exc.close()
                raise ValueError('migration configuration invalid') from None
            raise
        if (not isinstance(response, dict) or response.get('type') != 'file'
                or response.get('encoding') != 'base64'
                or not isinstance(response.get('content'), str)
                or not isinstance(response.get('sha'), str)
                or not re.fullmatch(r'[0-9a-f]{40}', response['sha'])):
            raise ValueError('migration configuration invalid')
        try:
            raw = base64.b64decode(response['content'].replace('\n', ''), validate=True)
        except (ValueError, binascii.Error):
            raise ValueError('migration configuration invalid') from None
        if blob_sha(raw) != response['sha']:
            raise ValueError('migration configuration invalid')
        return raw, response['sha']

    def read(self):
        return self._read()

    def publish(self, expected_sha, replacement):
        if not isinstance(expected_sha, str) or not isinstance(replacement, bytes):
            raise ValueError('migration configuration publication failed')
        _, sha = self._read()
        if sha != expected_sha:
            raise ValueError('migration configuration changed by another writer')
        payload = {'message': 'Update mirror configuration after migration',
                   'branch': self.branch,
                   'content': base64.b64encode(replacement).decode('ascii'),
                   'sha': sha}
        try:
            self.api.request('PUT', self.url, payload)
        except HTTPError as exc:
            if exc.code == 409:
                exc.close()
                raise ValueError('migration configuration changed by another writer') from None
            raise
        readback, _ = self._read()
        if readback != replacement:
            raise ValueError('migration configuration publication failed')


class GitHubRepositories:
    """Destination repository identity checks and rename through the API."""

    def __init__(self, api, owner):
        self.api = api
        self.owner = _location(owner)

    def _url(self, name):
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in ('.', '..'):
            raise ValueError('migration destination identity mismatch')
        return f'{GITHUB}/repos/{quote(self.owner, safe="")}/{quote(name, safe="")}'

    def get(self, name):
        try:
            repo = self.api.request('GET', self._url(name))
        except HTTPError as exc:
            if exc.code == 404:
                exc.close()
                return None
            raise
        if (not isinstance(repo, dict) or type(repo.get('id')) is not int or repo['id'] <= 0
                or not isinstance(repo.get('full_name'), str)):
            raise ValueError('migration destination identity mismatch')
        return repo

    def rename(self, old, new):
        repo = self.api.request('PATCH', self._url(old), {'name': new})
        if (not isinstance(repo, dict) or type(repo.get('id')) is not int
                or not isinstance(repo.get('full_name'), str)
                or repo['full_name'].lower() != (self.owner + '/' + new).lower()):
            raise ValueError('migration destination identity mismatch')
        return repo


def store_from_environment(environ, config_path):
    """Open the management repository's authoritative config, if configured."""
    slug = environ.get('MIRROR_MANAGEMENT_REPOSITORY') or environ.get('GITHUB_REPOSITORY')
    token = (environ.get('MIRROR_MANAGEMENT_TOKEN') or environ.get('MANAGEMENT_TOKEN')
             or environ.get('GH_TOKEN'))
    if not slug or not token or os.path.basename(config_path) != 'config.json':
        return None
    parts = slug.split('/')
    if len(parts) != 2:
        raise ValueError('invalid management repository')
    return GitHubConfigFile(API(token), *parts, path='config.json')


def transaction_from_environment(environ, config_path, destination_owner, api=None,
                                 destination_api=None):
    """Build a GitHub migration transaction, or ``None`` when unavailable.

    ``api`` reads and writes the management repository. ``destination_api`` must
    be the credential that can see and rename the mirror repositories; the
    workflow token is scoped to the management repository and would see every
    destination as missing.
    """
    slug = environ.get('MIRROR_MANAGEMENT_REPOSITORY') or environ.get('GITHUB_REPOSITORY')
    token = (environ.get('MIRROR_MANAGEMENT_TOKEN') or environ.get('MANAGEMENT_TOKEN')
             or environ.get('GH_TOKEN'))
    secret = environ.get('MIRROR_TIMESTAMP_SECRET')
    path = os.path.basename(config_path)
    if not slug or not token or not secret or path not in CONFIG_PATHS:
        return None
    owner, separator, repo = slug.partition('/')
    if not separator or not owner or not repo:
        raise ValueError('invalid management repository')
    _location(owner)
    _location(repo)
    _location(destination_owner)
    api = api or API(token)
    store = MigrationsStore(GitHubMigrations(api, owner, repo), secret)
    configuration = GitHubConfigFile(api, owner, repo, path)
    repositories = GitHubRepositories(destination_api or api, destination_owner)
    return MigrationTransaction(store, repositories, configuration,
                                secret=secret, config_path=path)
