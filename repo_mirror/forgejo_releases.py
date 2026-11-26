"""GitHub and local Forgejo release transport clients."""

import http.client
import json
import re
import secrets
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from repo_mirror.forgejo_local import source_identity
from repo_mirror.github_api import SourceAPI

MAX_BYTES = 8 * 1024 * 1024  # maximum JSON response, not asset size
CHUNK_SIZE = 1024 * 1024


class SyncConflict(RuntimeError):
    """The destination or source cannot safely be reconciled automatically."""


def _id(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SyncConflict('invalid API identifier')
    return str(value)


def _download(response, asset, output, *, retry_truncated=False):
    if getattr(response, 'status', 200) != 200:
        raise SyncConflict('asset download failed')
    length = response.headers.get('Content-Length')
    if length is not None:
        try:
            if int(length) != asset['size']:
                raise SyncConflict('asset content length mismatch')
        except ValueError:
            raise SyncConflict('invalid asset content length') from None
    total = 0
    while True:
        chunk = response.read(CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > asset['size']:
            raise SyncConflict('asset size mismatch')
        output.write(chunk)
    if total != asset['size']:
        if retry_truncated:
            raise http.client.IncompleteRead(b'', asset['size'] - total)
        raise SyncConflict('asset size mismatch')


class GitHubReleases:
    def __init__(self, owner_repo, token):
        owner, repo = source_identity(owner_repo)
        self.base = f'https://api.github.com/repos/{owner}/{repo}'
        self.api = SourceAPI(token)

    def list_releases(self):
        return self.api.pages(self.base + '/releases')

    def list_assets(self, release):
        return self.api.pages(self.base + '/releases/' + _id(release.get('id')) + '/assets')

    def download(self, asset, output):
        try:
            self._download_once(asset, output)
        except SyncConflict:
            raise
        except Exception:
            raise SyncConflict('source asset download failed') from None

    def _download_once(self, asset, output, *, retry_truncated=False):
        url = self.base + '/releases/assets/' + _id(asset.get('id'))
        token = self.api._request_token('GET', urlsplit(url))
        headers = {'Accept': 'application/octet-stream', 'User-Agent': 'repo-mirror'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        with self.api.opener.open(Request(url, headers=headers), timeout=60) as response:
            _download(response, asset, output, retry_truncated=retry_truncated)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ForgejoReleases:
    def __init__(self, owner_repo, token, port=3000):
        owner, repo = source_identity(owner_repo)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError('invalid local port')
        self.origin = f'http://127.0.0.1:{port}'
        self.base = f'/api/v1/repos/{owner}/{repo}'
        self.download_prefix = f'/{owner}/{repo}/releases/download/'
        self.token = token
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())
        self._listed_assets = {}

    def _request(self, method, path, body=None, content_type=None):
        headers = {'Authorization': 'token ' + self.token, 'User-Agent': 'repo-mirror'}
        if content_type:
            headers['Content-Type'] = content_type
        with self.opener.open(Request(self.origin + path, data=body, headers=headers, method=method), timeout=60) as response:
            data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise SyncConflict('response exceeds size limit')
        return data

    def _json(self, method, path, value=None):
        body = None if value is None else json.dumps(value).encode()
        return json.loads(self._request(method, path, body, 'application/json'))

    def list_releases(self):
        result = []
        seen = set()
        for page in range(1, 10001):
            batch = self._json('GET', self.base + f'/releases?limit=100&page={page}')
            if not isinstance(batch, list):
                raise SyncConflict('invalid release listing')
            if not batch:
                return result
            for item in batch:
                if not isinstance(item, dict):
                    raise SyncConflict('invalid release listing')
                release_id = _id(item.get('id'))
                if release_id in seen:
                    raise SyncConflict('duplicate release ID during pagination')
                seen.add(release_id)
            result.extend(batch)
        raise SyncConflict('release pagination limit exceeded')

    def list_assets(self, release):
        # Forgejo 17 ListReleaseAttachments ignores pagination and returns all assets.
        rid = _id(release.get('id'))
        result = self._json('GET', self.base + '/releases/' + rid + '/assets')
        if not isinstance(result, list):
            raise SyncConflict('invalid asset listing')
        for item in result:
            if not isinstance(item, dict):
                raise SyncConflict('invalid asset listing')
            aid = _id(item.get('id'))
            if aid in self._listed_assets and self._listed_assets[aid][0] != rid:
                raise SyncConflict('ambiguous asset ID')
            self._listed_assets[aid] = (rid, dict(item))
        return result

    def create_release(self, release):
        return self._json('POST', self.base + '/releases', release)

    def download(self, asset, output):
        aid = _id(asset.get('id'))
        listed = self._listed_assets.get(aid)
        if listed is None or listed[1] != asset:
            raise SyncConflict('unknown asset identity')
        release = self._json('GET', self.base + '/releases/' + listed[0])
        if not isinstance(release, dict) or _id(release.get('id')) != listed[0]:
            raise SyncConflict('release identity changed')
        tag = release.get('tag_name')
        # Forgejo 17 GetReleaseAttachments builds this path using Go
        # url.PathEscape on the *whole* tag and asset name (including slashes).
        # Bind the decoded tag to the release whose listing supplied this asset.
        url = asset.get('browser_download_url', '')
        try:
            parsed = urlsplit(url)
            path = parsed.path
            parts = path[len(self.download_prefix):].split('/') if path.startswith(self.download_prefix) else []
            decoded = [unquote(part, errors='strict') for part in parts]
            safe = (isinstance(url, str) and not any(ord(c) < 32 or ord(c) == 127 for c in url) and
                    isinstance(tag, str) and tag and
                    all(part and part not in ('.', '..') for part in tag.split('/')) and
                    not any(c in tag for c in '\\%') and not any(ord(c) < 32 or ord(c) == 127 for c in tag) and
                    parsed.scheme in ('http', 'https') and parsed.hostname and parsed.port != 0 and
                    parsed.username is None and parsed.password is None and not parsed.query and not parsed.fragment and
                    len(decoded) == 2 and decoded[0] == tag and
                    decoded[1] == asset.get('name') and decoded[1] not in ('', '.', '..') and
                    not any(c in decoded[1] for c in '/\\%') and
                    not any(ord(c) < 32 or ord(c) == 127 for c in decoded[1]) and
                    not re.search(r'%(?![0-9a-fA-F]{2})', path))
        except (TypeError, ValueError, UnicodeError):
            safe = False
        if not safe:
            raise SyncConflict('unsafe Forgejo asset URL')
        headers = {'Authorization': 'token ' + self.token, 'User-Agent': 'repo-mirror'}
        try:
            with self.opener.open(Request(self.origin + parsed.path, headers=headers), timeout=60) as response:
                _download(response, asset, output)
        except SyncConflict:
            raise
        except Exception:
            raise SyncConflict('destination asset download failed') from None

    def upload(self, release, name, data, size, content_type='application/octet-stream'):
        if not isinstance(content_type, str) or not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", content_type):
            raise ValueError('invalid asset content type')
        boundary = 'mirror' + secrets.token_hex(16)
        prefix = f'--{boundary}\r\nContent-Disposition: form-data; name="attachment"; filename="asset"\r\nContent-Type: {content_type}\r\n\r\n'.encode()
        suffix = f'\r\n--{boundary}--\r\n'.encode()
        def chunks():
            yield prefix
            while True:
                chunk = data.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
            yield suffix
        path = self.base + '/releases/' + _id(release.get('id')) + '/assets?name=' + quote(name, safe='')
        headers = {'Authorization': 'token ' + self.token, 'User-Agent': 'repo-mirror',
                   'Content-Type': 'multipart/form-data; boundary=' + boundary,
                   'Content-Length': str(len(prefix) + size + len(suffix))}
        try:
            request = Request(self.origin + path, data=chunks(), headers=headers, method='POST')
            with self.opener.open(request, timeout=60) as response:
                result = response.read(MAX_BYTES + 1)
            if len(result) > MAX_BYTES:
                raise SyncConflict('response exceeds size limit')
            return json.loads(result)
        except SyncConflict:
            raise
        except (HTTPError, URLError, OSError, http.client.HTTPException):
            raise
        except Exception:
            # An interrupted POST may have created an asset. Never delete or retry it.
            raise SyncConflict('asset upload failed; reconcile destination before retry') from None
