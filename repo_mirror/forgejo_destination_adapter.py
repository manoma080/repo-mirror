"""GitHub-shaped destination transport for the shared release orchestrator.

Call repository.mirror_releases with this object as dst and its target as target.
Forgejo does not support setting latest: a requested latest must already match.
"""
import re
from urllib.parse import parse_qs, urlsplit

from repo_mirror.forgejo_local import source_identity
from repo_mirror.forgejo_releases import ForgejoReleases, SyncConflict, _id


class ForgejoDestinationAdapter:
    supports_target_commitish = True
    verify_empty_body_read_only = True

    def validate_release_source(self, release):
        pass

    def __init__(self, owner_repo, token, port=3000, *, client=None, guard=None):
        owner, repo = source_identity(owner_repo)
        if owner in ('.', '..') or repo in ('.', '..'):
            raise ValueError('invalid destination identity')
        if not isinstance(token, str) or not token or re.search(r'[\x00-\x20\x7f]', token):
            raise ValueError('invalid Forgejo token')
        self.client = client if client is not None else ForgejoReleases(owner_repo, token, port)
        expected = f'/api/v1/repos/{owner_repo}'
        origin = urlsplit(self.client.origin)
        if (origin.scheme != 'http' or origin.hostname != '127.0.0.1' or
                origin.username is not None or origin.password is not None or
                origin.port is None or origin.path or origin.query or origin.fragment or
                self.client.base != expected):
            raise ValueError('unsafe Forgejo target')
        self.target = self.client.origin + expected
        self._assets = {}
        self.guard = guard or (lambda: None)

    def _path(self, url):
        if not isinstance(url, str) or not url.startswith(self.target + '/releases'):
            raise ValueError('unsafe destination URL')
        path = url[len(self.target):]
        if any(c in path for c in ('#', '\\', '\r', '\n', '\t')):
            raise ValueError('unsafe destination URL')
        return path

    def _release(self, item):
        if not isinstance(item, dict):
            raise SyncConflict('invalid release')
        release = dict(item)

        rid = _id(release.get('id'))
        if 'name' not in release and 'title' in release:
            release['name'] = release.pop('title')
        release['upload_url'] = self.target + '/releases/' + rid + '/assets{?name,label}'
        return release

    def _asset(self, item, release_id):
        if not isinstance(item, dict):
            raise SyncConflict('invalid asset')
        asset = dict(item)
        aid = _id(asset.get('id'))
        if aid in self._assets and self._assets[aid][0] != release_id:
            raise SyncConflict('ambiguous asset ID')
        self._assets[aid] = (release_id, dict(item))
        asset.setdefault('state', 'uploaded')
        asset.setdefault('digest', None)
        asset['url'] = self.target + '/releases/assets/' + aid
        return asset

    def pages(self, url):
        self.guard()
        result = self._pages(url)
        self.guard()
        return result

    def _pages(self, url):
        path = self._path(url)
        if path == '/releases':
            return [self._release(item) for item in self.client.list_releases()]
        match = re.fullmatch(r'/releases/([1-9][0-9]*)/assets', path)
        if match:
            rid = match.group(1)
            return [self._asset(item, rid) for item in self.client.list_assets({'id': int(rid)})]
        raise ValueError('unsupported Forgejo listing')

    def request(self, method, url, body=None):
        self.guard()
        result = self._request(method, url, body)
        self.guard()
        return result

    def publish_authenticated_draft(self, release_id, old_tag, body):
        """Publish a draft whose identity was authenticated by the orchestrator."""
        rid = _id(release_id)
        if (not isinstance(old_tag, str) or not old_tag.startswith('untagged-') or
                not isinstance(body, dict) or body.get('draft') is not False or
                not isinstance(body.get('tag_name'), str) or not body['tag_name']):
            raise SyncConflict('invalid draft publication')
        self.guard()
        result = self._request('PATCH', self.target + '/releases/' + rid, body, old_tag)
        self.guard()
        return result

    def _request(self, method, url, body=None, authorized_old_tag=None):
        path = self._path(url)
        release = re.fullmatch(r'/releases/([1-9][0-9]*)', path)
        asset = re.fullmatch(r'/releases/assets/([1-9][0-9]*)', path)
        is_tag = re.fullmatch(r'/releases/tags/[^/?]+', path)
        if asset and method in ('PATCH', 'DELETE'):
            aid = asset.group(1)
            if aid not in self._assets:
                raise SyncConflict('unknown asset identity')
            path = '/releases/' + self._assets[aid][0] + '/assets/' + aid
        elif not ((method == 'GET' and (release or is_tag or path == '/releases/latest')) or
                  (method == 'POST' and path == '/releases') or
                  (method in ('PATCH', 'DELETE') and release)):
            raise ValueError('unsupported Forgejo request')
        if body is not None:
            if not isinstance(body, dict):
                raise ValueError('invalid request body')
            body = dict(body)
            if (release or path == '/releases') and 'body' in body and body['body'] in (None, '', ' '):
                # An empty PATCH is ignored by Forgejo; a single space clears it.
                body['body'] = ' '
            latest = body.pop('make_latest', None)
            if latest not in (None, 'false', 'true'):
                raise ValueError('invalid latest value')
            if latest == 'true':
                if method != 'PATCH' or not release or body:
                    raise SyncConflict('unsupported latest operation')
                current = self.client._json('GET', self.client.base + '/releases/latest')
                if (not isinstance(current, dict) or type(current.get('id')) is not int or
                        current['id'] != int(release.group(1))):
                    raise SyncConflict('Forgejo latest release cannot be selected')
                return self._release(current)
        full = self.client.base + path
        if method == 'DELETE':
            self.client._request(method, full)
            if asset:
                self._assets.pop(asset.group(1), None)
            return None
        if method == 'PATCH' and release:
            before = self._release(self.client._json('GET', full))
            tag = before.get('tag_name')
            authorized = (authorized_old_tag is not None and tag == authorized_old_tag and
                          before.get('draft') is True and (body or {}).get('draft') is False)
            if (type(before.get('id')) is not int or before['id'] != int(release.group(1)) or
                    not isinstance(tag, str) or not tag or
                    (authorized_old_tag is not None and not authorized) or
                    ('tag_name' in (body or {}) and (body or {})['tag_name'] != tag and not authorized)):
                raise SyncConflict('release identity changed')
            self.guard()
            self.client._json(method, full, body)
            raw_current = self.client._json('GET', full)
            current = self._release(raw_current)
            if (type(current.get('id')) is not int or current['id'] != before['id'] or
                    current.get('tag_name') != ((body or {}).get('tag_name') if authorized else tag)):
                raise SyncConflict('release identity changed')
            for key, expected in (body or {}).items():
                if key not in current:
                    raise SyncConflict('release metadata not persisted: ' + key)
                actual = current[key]

                # Forgejo may represent an empty or null title as the release tag.
                if key == 'name' and expected in ('', None) and actual == current['tag_name']:
                    continue
                if type(actual) is not type(expected) or actual != expected:
                    raise SyncConflict('release metadata not persisted: ' + key)
            return current
        result = self.client._json(method, full, body)
        if asset:
            return self._asset(result, self._assets[asset.group(1)][0])
        return self._release(result)

    def download(self, url, output):
        match = re.fullmatch(r'/releases/assets/([1-9][0-9]*)', self._path(url))
        if not match or match.group(1) not in self._assets:
            raise SyncConflict('unknown asset identity')
        self.guard()
        self.client.download(self._assets[match.group(1)][1], output)
        self.guard()

    def upload(self, url, data, content_type):
        path = self._path(url)
        match = re.fullmatch(r'/releases/([1-9][0-9]*)/assets\?([^#]*)', path)
        if not match:
            raise ValueError('invalid upload URL')
        query = parse_qs(match.group(2), keep_blank_values=True, strict_parsing=True)
        if set(query) != {'name'} or len(query['name']) != 1 or not query['name'][0]:
            raise ValueError('invalid upload name')
        start = data.tell()
        data.seek(0, 2)
        size = data.tell() - start
        data.seek(start)
        self.guard()
        result = self.client.upload({'id': int(match.group(1))}, query['name'][0], data, size,
                                    content_type or 'application/octet-stream')
        self.guard()
        return self._asset(result, match.group(1))
