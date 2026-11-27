"""Authenticated GitHub asset downloads for the Forgejo shared release path."""
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from repo_mirror.forgejo_local import source_identity
from repo_mirror.forgejo_releases import GitHubReleases, _DOWNLOAD_ATTEMPT_CALLBACK, _download, _id
from repo_mirror.github_api import SafeRedirect, SourceAPI, https_origin, transient_transfer_error, transfer_retry_delay


class _AssetRedirect(SafeRedirect):
    """Allow GitHub's release storage only, with SafeRedirect's auth stripping."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if (target.scheme != 'https' or target.hostname not in {
                'release-assets.githubusercontent.com', 'objects.githubusercontent.com',
                'github-releases.githubusercontent.com'} or target.port not in (None, 443)
                or target.username is not None or target.password is not None or target.fragment):
            raise ValueError('unsafe asset redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ForgejoSourceAdapter:
    def __init__(self, owner_repo, token, *, api=None, client=None):
        owner, repo = source_identity(owner_repo)
        if owner in ('.', '..') or repo in ('.', '..'):
            raise ValueError('invalid source identity')
        self.base = f'https://api.github.com/repos/{owner}/{repo}'
        self.browser_prefix = f'/{owner}/{repo}/releases/download/'
        self.api = api if api is not None else SourceAPI(token)
        if isinstance(self.api, SourceAPI):
            self.api.restrict_origin('https://api.github.com')
        self.client = client if client is not None else GitHubReleases(owner_repo, token)
        self.anonymous = token is None and client is None
        if client is None:
            self.client.api.opener = build_opener(ProxyHandler({}), _AssetRedirect())
        self._assets = {}

    def pages(self, url):
        items = self.api.pages(url)
        if re.fullmatch(re.escape(self.base) + r'/releases/[1-9][0-9]*/assets', url):
            for item in items:
                asset = dict(item)
                _id(asset.get('id'))
                browser = asset.get('browser_download_url')
                if not isinstance(browser, str):
                    raise ValueError('invalid source asset URL')
                parsed = urlsplit(browser)
                if (parsed.scheme != 'https' or parsed.netloc != 'github.com' or
                        not parsed.path.startswith(self.browser_prefix) or
                        not parsed.path[len(self.browser_prefix):] or parsed.query or parsed.fragment):
                    raise ValueError('invalid source asset URL')
                previous = self._assets.get(browser)
                if previous is not None and previous['id'] != asset['id']:
                    raise ValueError('ambiguous source asset URL')
                self._assets[browser] = asset
        return items

    def request(self, method, url, data=None, **kwargs):
        return self.api.request(method, url, data, **kwargs)

    def download_verified_asset(self, asset, output):
        """Download only the browser URL bound to this listed asset identity."""
        url = asset.get('browser_download_url')
        loaded = self._assets.get(url)
        if loaded is None or type(asset.get('id')) is not int or loaded.get('id') != asset['id']:
            raise ValueError('source asset identity changed')
        return self.download(url, output)

    def download(self, url, output):
        if url not in self._assets:
            raise ValueError('unknown source asset URL')
        position = output.tell()
        for attempt in range(4):
            output.seek(position)
            output.truncate()
            notified = False

            def notify_attempt(current_attempt=attempt):
                nonlocal notified
                if not notified:
                    notified = True
                    if current_attempt > 0:
                        print(f'asset download retry {current_attempt}/3 target=source', file=sys.stderr, flush=True)

            try:
                if self.anonymous:
                    req = Request(url, headers={'User-Agent': 'repo-mirror'})
                    notify_attempt()
                    with self.client.api.opener.open(req, timeout=60) as response:
                        return _download(response, self._assets[url], output, retry_truncated=True)
                callback_token = _DOWNLOAD_ATTEMPT_CALLBACK.set(notify_attempt)
                try:
                    return self.client._download_once(self._assets[url], output, retry_truncated=True)
                finally:
                    _DOWNLOAD_ATTEMPT_CALLBACK.reset(callback_token)
            except Exception as exc:
                output.seek(position)
                output.truncate()
                if not transient_transfer_error(exc) or attempt == 3:
                    raise
                delay = transfer_retry_delay(exc, attempt)
                if delay is None:
                    raise
                if isinstance(exc, HTTPError):
                    exc.close()
                time.sleep(delay)


class PublicForgejoSourceAdapter:
    """Public Forgejo API and asset reads; never receives GitHub credentials."""

    def __init__(self, identity, *, api=None):
        from repo_mirror.forgejo_local import forgejo_source_identity
        identity = forgejo_source_identity(identity)
        base, owner, repo = identity.rsplit('/', 2)
        self.base = f'{base}/api/v1/repos/{owner}/{repo}'
        self.origin = https_origin(base)
        self.browser_prefix = f'/{owner}/{repo}/releases/download/'
        self.api = api if api is not None else SourceAPI(None)
        if isinstance(self.api, SourceAPI):
            self.api.restrict_origin(base)
        # The configured Forgejo origin is trusted; never extend this exception
        # to an asset URL supplied by the remote server or to another origin.
        parsed_origin = urlsplit(base)
        self.api.trusted_asset_origin = (parsed_origin.hostname, parsed_origin.port or 443)
        self._assets = {}

    def _normalize_assets(self, items):
        if not isinstance(items, list):
            raise ValueError('invalid source asset listing')
        result = []
        for item in items:
            asset = dict(item)
            _id(asset.get('id'))
            url = asset.get('browser_download_url')
            if not isinstance(url, str):
                raise ValueError('invalid source asset URL')
            parsed = urlsplit(url)
            decoded = parsed.path
            while True:
                if '\\' in decoded or any(part in ('.', '..') for part in decoded.split('/')):
                    raise ValueError('invalid source asset URL')
                next_decoded = unquote(decoded)
                if next_decoded == decoded:
                    break
                decoded = next_decoded
            if (https_origin(url) != self.origin
                    or not parsed.path.startswith(self.browser_prefix)
                    or not parsed.path[len(self.browser_prefix):] or parsed.query or parsed.fragment):
                raise ValueError('invalid source asset URL')
            previous = self._assets.get(url)
            if previous is not None and previous != asset['id']:
                raise ValueError('ambiguous source asset URL')
            self._assets[url] = asset['id']
            asset.setdefault('state', 'uploaded')
            result.append(asset)
        return result

    def pages(self, url):
        if https_origin(url) != self.origin:
            raise ValueError('API request outside configured origin')
        if re.fullmatch(re.escape(self.base) + r'/releases/[1-9][0-9]*/assets', url):
            return self._normalize_assets(self.api.request('GET', url))
        return self.api.pages(url)

    def request(self, method, url, data=None, **kwargs):
        if https_origin(url) != self.origin:
            raise ValueError('API request outside configured origin')
        if method == 'GET' and re.fullmatch(re.escape(self.base) + r'/releases/[1-9][0-9]*/assets', url):
            return self.pages(url)
        return self.api.request(method, url, data, **kwargs)

    def download_verified_asset(self, asset, output):
        url = asset.get('browser_download_url')
        if type(asset.get('id')) is not int or self._assets.get(url) != asset['id']:
            raise ValueError('source asset identity changed')
        return self.api.download(url, output)

    def download(self, url, output):
        if url not in self._assets:
            raise ValueError('unknown source asset URL')
        return self.api.download(url, output)
