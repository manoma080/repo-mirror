"""Anonymous, origin-bound GitLab project and release API.

Release identity is (origin, project ID, exact tag), not a numeric release ID.
Returned API objects are independent snapshots. No asset download is provided.
"""
import json
import re
from copy import deepcopy
from datetime import datetime
from urllib.error import HTTPError
from urllib.parse import quote, urljoin, urlsplit, parse_qs
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _origin(url):
    p = urlsplit(url)
    if (p.scheme != 'https' or not p.hostname or p.username is not None or
            p.password is not None or p.fragment):
        raise ValueError('invalid GitLab origin')
    return (p.scheme, p.hostname.lower(), p.port or 443)


class GitLabSourceAdapter:
    release_identity_kind = 'tag'

    def __init__(self, origin, project_path, project_id, *, handlers=()):
        p = urlsplit(origin)
        self.origin = _origin(origin)
        if p.path not in ('', '/') or p.query:
            raise ValueError('origin must not include a path')
        if (not isinstance(project_path, str) or len(project_path.split('/')) < 2 or
                any(not part or part in ('.', '..') for part in project_path.split('/'))):
            raise ValueError('invalid project path')
        if type(project_id) is not int or project_id <= 0:
            raise ValueError('invalid project ID')
        self.origin_url = origin.rstrip('/')
        self.project_path = project_path
        self.project_id = project_id
        self.release_scope = (self.origin_url, project_id)
        self.base = self.origin_url + '/api/v4/projects/' + quote(project_path, safe='')
        self.pinned_base = self.origin_url + '/api/v4/projects/' + str(project_id)
        self.opener = build_opener(ProxyHandler({}), _NoRedirect(), *handlers)

    def _get(self, url):
        for _ in range(6):
            if _origin(url) != self.origin:
                raise ValueError('cross-origin API request')
            try:
                with self.opener.open(Request(url, headers={'Accept': 'application/json'}), timeout=30) as response:
                    if _origin(response.geturl()) != self.origin:
                        raise ValueError('cross-origin API response')
                    return json.load(response), response.headers
            except HTTPError as exc:
                if exc.code not in (301, 302, 303, 307, 308):
                    raise
                location = exc.headers.get('Location')
                exc.close()
                if not location:
                    raise ValueError('redirect without location')
                url = urljoin(url, location)
        raise ValueError('too many redirects')

    def project(self):
        data, _ = self._get(self.base)
        if (not isinstance(data, dict) or type(data.get('id')) is not int or
                data['id'] != self.project_id or data.get('visibility') != 'public'):
            raise ValueError('project identity or visibility mismatch')
        if data.get('path_with_namespace') != self.project_path:
            raise ValueError('project path mismatch')
        clone = data.get('http_url_to_repo')
        if clone is not None and clone != self.origin_url + '/' + self.project_path + '.git':
            raise ValueError('project clone identity mismatch')
        return data

    def releases(self):
        """Read every release using GitLab's X-Next-Page pagination."""
        page = 1
        seen = set()
        result = []
        tags = set()
        while True:
            if page in seen:
                raise ValueError('pagination cycle')
            seen.add(page)
            self.project()
            items, headers = self._get(f'{self.pinned_base}/releases?per_page=100&page={page}')
            if not isinstance(items, list):
                raise ValueError('invalid release listing')
            for item in items:
                self.snapshot(item)
                tag = item['tag_name']
                if tag in tags:
                    raise ValueError('duplicate release tag')
                tags.add(tag)
                result.append(item)
            next_page = headers.get('X-Next-Page', '')
            link = headers.get('Link', '')
            next_links = re.findall(r'<([^>]+)>\s*;\s*rel="?next"?', link, re.IGNORECASE)
            if len(next_links) > 1:
                raise ValueError('invalid pagination')
            if next_links:
                url = urljoin(self.pinned_base, next_links[0])
                parsed = urlsplit(url)
                query = parse_qs(parsed.query, keep_blank_values=True)
                if (_origin(url) != self.origin or parsed.path != urlsplit(self.pinned_base + '/releases').path
                        or parsed.fragment or set(query) != {'page', 'per_page'}
                        or query['per_page'] != ['100'] or len(query['page']) != 1):
                    raise ValueError('invalid pagination link')
                if next_page and next_page != query['page'][0]:
                    raise ValueError('conflicting pagination')
                next_page = query['page'][0]
            if not next_page:
                return result
            if not next_page.isascii() or not next_page.isdigit() or int(next_page) <= page:
                raise ValueError('invalid pagination')
            page = int(next_page)

    def release(self, tag):
        """Get a release by exact tag, percent-encoding every reserved byte."""
        if not isinstance(tag, str) or not tag:
            raise ValueError('invalid release tag')
        self.project()
        result, _ = self._get(self.pinned_base + '/releases/' + quote(tag, safe=''))
        self.snapshot(result)
        if result['tag_name'] != tag:
            raise ValueError('release tag mismatch')
        return result

    def latest(self):
        """Return the release with greatest released_at, or None if empty."""
        releases = self.releases()
        return max(releases, key=lambda r: self._released_at(r['released_at'])) if releases else None

    @staticmethod
    def _normalize(release):
        name, body = release.get('name'), release.get('description')
        if ((name is not None and not isinstance(name, str)) or
                (body is not None and not isinstance(body, str))):
            raise ValueError('invalid release metadata')
        return {'tag_name': release['tag_name'], 'name': name, 'body': body,
                'draft': False, 'prerelease': False, 'target_commitish': None,
                'released_at': release['released_at']}

    def list_releases(self):
        return [self._normalize(item) for item in self.releases()]

    def get_release(self, tag):
        return self._normalize(self.release(tag))

    def list_release_assets(self, tag):
        return self.links(self.release(tag))

    def latest_release(self, releases):
        return max(releases, key=lambda r: self._released_at(r['released_at'])) if releases else None

    def verify_release_tag(self, tag, dst, target):
        if not isinstance(tag, str) or not tag:
            raise ValueError('invalid release tag')
        self.project()
        obj, _ = self._get(self.pinned_base + '/repository/tags/' + quote(tag, safe=''))
        if not isinstance(obj, dict) or obj.get('name') != tag:
            raise ValueError('tag verification failed')
        if 'message' not in obj:
            raise ValueError('tag verification failed')
        sha, message = obj.get('target'), obj.get('message')
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise ValueError('tag verification failed')
        if message is not None and not isinstance(message, str):
            raise ValueError('tag verification failed')
        kind = 'commit' if message is None else 'tag'
        if kind == 'commit' and (not isinstance(obj.get('commit'), dict) or obj['commit'].get('id') != sha):
            raise ValueError('tag verification failed')
        ref = dst.request('GET', target + '/git/ref/tags/' + quote(tag, safe=''))
        if (not isinstance(ref, dict) or ref.get('ref') != 'refs/tags/' + tag or
                not isinstance(ref.get('object'), dict) or ref['object'].get('sha') != sha or
                ref['object'].get('type') != kind):
            raise ValueError('tag verification failed')
        return (kind, sha)

    @staticmethod
    def _released_at(value):
        if not isinstance(value, str):
            raise ValueError('invalid released_at')
        try:
            result = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if result.utcoffset() is None:
                raise ValueError('released_at lacks timezone')
            return result
        except (ValueError, OverflowError) as exc:
            raise ValueError('invalid released_at') from exc

    @staticmethod
    def links(release):
        assets = release.get('assets') if isinstance(release, dict) else None
        links = assets.get('links') if isinstance(assets, dict) else None
        if not isinstance(links, list) or any(not isinstance(link, dict) for link in links):
            raise ValueError('invalid release links')
        return deepcopy(links)

    @classmethod
    def snapshot(cls, release):
        """Stable release fields and linked assets; exclude generated sources."""
        if not isinstance(release, dict) or not isinstance(release.get('tag_name'), str) or not release['tag_name']:
            raise ValueError('invalid release')
        cls._released_at(release.get('released_at'))
        links = cls.links(release)
        fields = ('tag_name', 'name', 'description', 'released_at', 'created_at', 'upcoming_release', 'commit')
        result = {key: deepcopy(release[key]) for key in fields if key in release}
        result['links'] = links
        return result
