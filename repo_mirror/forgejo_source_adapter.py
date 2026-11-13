"""Authenticated GitHub asset downloads for the Forgejo shared release path."""
import re
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from repo_mirror.forgejo_local import source_identity
from repo_mirror.forgejo_releases import GitHubReleases, _id
from repo_mirror.github_api import SafeRedirect, SourceAPI, transient_transfer_error, transfer_retry_delay


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
        self.client = client if client is not None else GitHubReleases(owner_repo, token)
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

    def download(self, url, output):
        if url not in self._assets:
            raise ValueError('unknown source asset URL')
        position = output.tell()
        for attempt in range(4):
            output.seek(position)
            output.truncate()
            try:
                return self.client._download_once(self._assets[url], output, retry_truncated=True)
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
                print(f'asset download retry {attempt + 1}/3 target=source', file=sys.stderr, flush=True)
                time.sleep(delay)
