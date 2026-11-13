"""Conservative GitHub-to-local-Forgejo release copying.

No asset is overwritten or removed. Conflicts and ambiguous drafts stop the sync.
Clients may be injected into sync_releases for tests or alternate transports.
"""

import hashlib
import json
import secrets
import tempfile
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from repo_mirror.forgejo_local import source_identity
from repo_mirror.github_api import SourceAPI

MAX_BYTES = 8 * 1024 * 1024  # maximum JSON response, not asset size
CHUNK_SIZE = 1024 * 1024
_FIELDS = ('tag_name', 'name', 'body', 'draft', 'prerelease', 'target_commitish')


class SyncConflict(RuntimeError):
    """The destination or source cannot safely be reconciled automatically."""


def _id(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SyncConflict('invalid API identifier')
    return str(value)


def _assets(items, source=False):
    out = {}
    for item in items:
        name = item.get('name')
        if not isinstance(name, str) or not name or name in out:
            raise SyncConflict('ambiguous asset names')
        if source and item.get('state') != 'uploaded':
            raise SyncConflict('source asset not uploaded')
        size = item.get('size')
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise SyncConflict('invalid or unsupported asset size')
        _id(item.get('id'))
        out[name] = item
    return out


def _asset_snapshot(assets, fields):
    return {name: tuple(asset.get(k) for k in fields) for name, asset in assets.items()}


def _checked(stream, asset):
    stream.seek(0)
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = stream.read(CHUNK_SIZE)
        if not chunk:
            break
        size += len(chunk)
        if size > asset['size']:
            raise SyncConflict('asset size mismatch')
        digest.update(chunk)
    if size != asset['size']:
        raise SyncConflict('asset size mismatch')
    value = digest.hexdigest()
    expected = asset.get('digest')
    if expected is not None and expected != 'sha256:' + value:
        raise SyncConflict('source asset digest mismatch')
    stream.seek(0)
    return value


def _download(response, asset, output):
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
        raise SyncConflict('asset size mismatch')


def _release_map(items):
    result = {}
    for item in items:
        tag = item.get('tag_name')
        if not isinstance(tag, str) or not tag or tag.startswith('untagged-') or tag in result:
            raise SyncConflict('ambiguous release tag')
        _id(item.get('id'))
        result[tag] = item
    return result


def _destination_metadata_matches(source, target):
    # Forgejo may store an empty release name as the tag; preserve all other checks.
    return all(target.get(k) == source[k] or (
        k == 'name' and source[k] == '' and target.get(k) == source['tag_name']
    ) for k in _FIELDS)


def sync_releases(source, destination):
    """Copy all releases; raise on conflicts, interrupted operations or failed verification.

    Release and asset data are read again after copying. Existing release metadata is
    never changed; mismatches require operator reconciliation. Run without other
    writers: Forgejo does not provide conditional release/asset creation here.
    Assets are read and compared by content even without a published digest, but
    same-size source mutations without a digest can race the final metadata check.
    """
    src = _release_map(source.list_releases())
    src = {tag: dict(release) for tag, release in src.items()}
    dst = _release_map(destination.list_releases())
    target_ids = {}
    source_assets = {}
    target_assets = {}
    source_fields = ('name', 'id', 'size', 'state', 'digest')
    target_fields = ('name', 'id', 'size')
    for tag, release in src.items():
        release = dict(release)
        if any(field not in release for field in _FIELDS):
            raise SyncConflict('incomplete release metadata')
        target = dst.get(tag)
        if target is not None:
            if release['draft'] or target.get('draft'):
                raise SyncConflict('existing draft identity requires reconciliation')
            if not _destination_metadata_matches(release, target):
                raise SyncConflict('release metadata collision')
        else:
            target = destination.create_release({k: release[k] for k in _FIELDS})
            _id(target.get('id'))
        target_ids[tag] = target['id']
        before = _assets(source.list_assets(release), source=True)
        source_assets[tag] = _asset_snapshot(before, source_fields)
        target_assets[tag] = {}
        existing = _assets(destination.list_assets(target))
        for name, asset in before.items():
            # TemporaryFile uses TMPDIR and is unlinked on close, including failures.
            with tempfile.TemporaryFile() as data:
                source.download(asset, data)
                digest = _checked(data, asset)
                current = existing.get(name)
                if current is None:
                    existing = _assets(destination.list_assets(target))
                    current = existing.get(name)
                if current is None:
                    destination.upload(target, name, data, asset['size'])
                    existing = _assets(destination.list_assets(target))
                    current = existing.get(name)
                    if current is None:
                        raise SyncConflict('uploaded asset not visible')
                with tempfile.TemporaryFile() as downloaded:
                    destination.download(current, downloaded)
                    if _checked(downloaded, current) != digest:
                        raise SyncConflict('asset collision or upload corruption')
                target_assets[tag][name] = tuple(current.get(k) for k in target_fields)
        after = _assets(source.list_assets(release), source=True)
        if source_assets[tag] != _asset_snapshot(after, source_fields):
            raise SyncConflict('source assets changed during sync')
        final_source = _release_map(source.list_releases()).get(tag)
        if final_source is None or final_source.get('id') != release['id'] or any(
            final_source.get(k) != release[k] for k in _FIELDS
        ):
            raise SyncConflict('source release changed during sync')
        verified = _release_map(destination.list_releases()).get(tag)
        if verified is None or verified.get('id') != target['id'] or not _destination_metadata_matches(release, verified):
            raise SyncConflict('release verification failed')

    final_src = _release_map(source.list_releases())
    if set(final_src) != set(src) or any(
        final_src[tag].get('id') != release['id'] or
        any(final_src[tag].get(k) != release[k] for k in _FIELDS)
        for tag, release in src.items()
    ):
        raise SyncConflict('source releases changed during sync')
    final_dst = _release_map(destination.list_releases())
    if any(
        tag not in final_dst or final_dst[tag].get('id') != target_ids[tag] or
        not _destination_metadata_matches(release, final_dst[tag])
        for tag, release in src.items()
    ):
        raise SyncConflict('release verification failed')
    for tag, release in src.items():
        current = _assets(source.list_assets(final_src[tag]), source=True)
        if _asset_snapshot(current, source_fields) != source_assets[tag]:
            raise SyncConflict('source assets changed during sync')
        current = _assets(destination.list_assets(final_dst[tag]))
        snapshot = _asset_snapshot(current, target_fields)
        if any(snapshot.get(name) != value for name, value in target_assets[tag].items()):
            raise SyncConflict('destination asset verification failed')


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
        url = self.base + '/releases/assets/' + _id(asset.get('id'))
        token = self.api._request_token('GET', urlsplit(url))
        headers = {'Accept': 'application/octet-stream', 'User-Agent': 'repo-mirror'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        try:
            with self.api.opener.open(Request(url, headers=headers), timeout=60) as response:
                _download(response, asset, output)
        except SyncConflict:
            raise
        except Exception:
            raise SyncConflict('source asset download failed') from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ForgejoReleases:
    def __init__(self, owner_repo, token, port=3300):
        owner, repo = source_identity(owner_repo)
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError('invalid local port')
        self.origin = f'http://127.0.0.1:{port}'
        self.base = f'/api/v1/repos/{owner}/{repo}'
        self.token = token
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

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
        result = self._json('GET', self.base + '/releases/' + _id(release.get('id')) + '/assets')
        if not isinstance(result, list):
            raise SyncConflict('invalid asset listing')
        return result

    def create_release(self, release):
        return self._json('POST', self.base + '/releases', release)

    def download(self, asset, output):
        url = asset.get('browser_download_url', '')
        parsed = urlsplit(url)
        if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1') or parsed.port != int(self.origin.rsplit(':', 1)[1]) or parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.path.startswith('/'):
            raise SyncConflict('unsafe Forgejo asset URL')
        headers = {'Authorization': 'token ' + self.token, 'User-Agent': 'repo-mirror'}
        try:
            with self.opener.open(Request(self.origin + parsed.path, headers=headers), timeout=60) as response:
                _download(response, asset, output)
        except SyncConflict:
            raise
        except Exception:
            raise SyncConflict('destination asset download failed') from None

    def upload(self, release, name, data, size):
        boundary = 'mirror' + secrets.token_hex(16)
        prefix = f'--{boundary}\r\nContent-Disposition: form-data; name="attachment"; filename="asset"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
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
        except Exception:
            # An interrupted POST may have created an asset. Never delete or retry it.
            raise SyncConflict('asset upload failed; reconcile destination before retry') from None
