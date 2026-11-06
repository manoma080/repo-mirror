#!/usr/bin/env python3
"""Mirror public GitHub/Forgejo repositories to private GitHub repositories."""
import argparse
import base64
import json
import http.client
import hashlib
import ipaddress
import socket
import ssl
import os
import re
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse, urljoin
from urllib.request import Request, build_opener, HTTPRedirectHandler

GITHUB = 'https://api.github.com'
MAX_ASSET_SIZE = 2 * 1024 * 1024 * 1024 - 1
CHUNK_SIZE = 1024 * 1024
NAME = re.compile(r'^[A-Za-z0-9_.-]+$')


def validate_source(source):
    if not isinstance(source, dict) or source.get('type') not in ('github', 'forgejo'):
        raise ValueError('source type must be github or forgejo')
    u = urlparse(source.get('base_url', ''))
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError('source base_url must be credential-free HTTPS')
    for value in [source.get('owner'), source.get('prefix', '') if source.get('prefix', '') != '' else 'x']:
        if not isinstance(value, str) or not NAME.fullmatch(value) or value in ('.', '..'):
            raise ValueError('invalid owner or prefix')
    repos = source.get('repos')
    if repos != 'all' and (not isinstance(repos, list) or any(not isinstance(x, str) or not NAME.fullmatch(x) or x in ('.', '..') for x in repos)):
        raise ValueError('repos must be all or a list of repository names')


def source_api_base(source):
    base = source['base_url'].rstrip('/')
    if source['type'] == 'forgejo':
        return base + '/api/v1'
    return GITHUB if base == 'https://github.com' else base


def discover(source, api):
    if source['repos'] != 'all':
        return source['repos']
    return [repo['name'] for repo in api.pages(f"{source_api_base(source)}/users/{quote(source['owner'], safe='')}/repos")]


def destination_name(source, name):
    if not isinstance(name, str) or not NAME.fullmatch(name) or name in ('.', '..'):
        raise ValueError('invalid discovered repository name')
    result = source.get('prefix', 'mirror-' + source['owner'] + '-') + name
    if len(result) > 100 or not NAME.fullmatch(result) or result in ('.', '..'):
        raise ValueError('invalid destination repository name')
    return result


def source_marker(source, name):
    return '[repo-mirror: ' + source['type'] + ':' + source['base_url'].rstrip('/').lower() + '/' + source['owner'] + '/' + name + ']'


def check_existing(repo, marker):
    if repo is not None and repo.get('description') not in (marker, marker + ' [repo-mirror:initializing:v1]'):
        raise RuntimeError('destination collision: repository has no matching source marker')


def get_existing(dst, target):
    try:
        return dst.request('GET', target)
    except HTTPError as exc:
        if not not_found(exc):
            raise
        exc.close()
        return None


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlparse(newurl)
        if target.scheme != 'https' or target.username or target.password:
            raise ValueError('unsafe redirect')
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urlparse(req.full_url).netloc.lower() != target.netloc.lower():
            redirected.remove_header('Authorization')
        return redirected


class API:
    def __init__(self, token=None):
        self.token = token
        self.opener = build_opener(SafeRedirect())

    def _request(self, method, url, body=None, headers=None):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.username or parsed.password:
            raise ValueError('API URL must be credential-free HTTPS')
        headers = dict(headers or {})
        headers['User-Agent'] = 'repo-mirror'
        if self.token:
            if parsed.hostname not in ('api.github.com', 'uploads.github.com') or parsed.port not in (None, 443):
                raise ValueError('refusing to send destination credentials to non-GitHub host')
            headers['Authorization'] = 'Bearer ' + self.token
        for attempt in range(4):
            try:
                with self.opener.open(Request(url, data=body, headers=headers, method=method), timeout=60) as response:
                    return response.read()
            except HTTPError as e:
                if method not in ('GET', 'PUT', 'PATCH', 'DELETE') or e.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise
            except URLError:
                if method not in ('GET', 'PUT', 'PATCH', 'DELETE') or attempt == 3:
                    raise
            time.sleep(2 ** attempt)
        raise RuntimeError('request retries exhausted')

    def request(self, method, url, data=None, **kwargs):
        body = None if data is None else json.dumps(data).encode()
        raw = self._request(method, url, body, {'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json'})
        return json.loads(raw) if raw else None

    def pages(self, url):
        result = []
        for page in range(1, 10001):
            separator = '&' if '?' in url else '?'
            items = self.request('GET', f'{url}{separator}per_page=100&limit=100&page={page}')
            if not isinstance(items, list):
                raise ValueError('pagination response is not an array')
            result.extend(items)
            if not items:
                return result
        raise RuntimeError('pagination exceeded safety limit')

    def download(self, url, output):
        if self.token:
            parsed = urlparse(url)
            if (parsed.scheme != 'https' or parsed.username or parsed.password or
                    parsed.hostname not in ('api.github.com', 'uploads.github.com') or
                    parsed.port not in (None, 443)):
                raise ValueError('refusing unsafe destination asset URL')
            request = Request(url, headers={'Accept': 'application/octet-stream',
                              'User-Agent': 'repo-mirror', 'Authorization': 'Bearer ' + self.token})
            with self.opener.open(request, timeout=60) as response:
                copy_asset(response, output)
            return
        # Connect to a validated address directly; do not use a proxy or resolve twice.
        for _ in range(10):
            parsed = urlparse(url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError('unsafe asset URL')
            port = parsed.port or 443
            addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
            ips = [ipaddress.ip_address(a[4][0]) for a in addresses]
            if not ips or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in ips):
                raise ValueError('asset address is not public')
            family, socktype, proto, _, address = addresses[0]
            raw = socket.socket(family, socktype, proto)
            raw.settimeout(60)
            try:
                raw.connect(address)
                tls = ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname)
                conn = http.client.HTTPSConnection(parsed.hostname, port, timeout=60)
                conn.sock = tls
                try:
                    path = parsed.path or '/'
                    if parsed.query:
                        path += '?' + parsed.query
                    conn.request('GET', path, headers={'Accept': 'application/octet-stream', 'User-Agent': 'repo-mirror'})
                    response = conn.getresponse()
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.getheader('Location')
                        if not location:
                            raise ValueError('asset redirect missing location')
                        url = urljoin(url, location)
                        continue
                    if response.status != 200:
                        raise RuntimeError('asset download failed')
                    copy_asset(response, output)
                    return
                finally:
                    conn.close()
            finally:
                raw.close()
        raise ValueError('too many asset redirects')

    def upload(self, url, data, content_type):
        position = data.tell()
        data.seek(0, os.SEEK_END)
        size = data.tell() - position
        data.seek(position)
        if size > MAX_ASSET_SIZE:
            raise ValueError('asset exceeds GitHub size limit')
        self._request('POST', url, data, {'Content-Type': content_type, 'Content-Length': str(size)})


def copy_asset(response, output):
    size = 0
    while True:
        chunk = response.read(CHUNK_SIZE)
        if not chunk:
            return size
        size += len(chunk)
        if size > MAX_ASSET_SIZE:
            raise ValueError('asset exceeds GitHub size limit')
        output.write(chunk)


def same_file(left, right):
    left.seek(0)
    right.seek(0)
    while True:
        a, b = left.read(CHUNK_SIZE), right.read(CHUNK_SIZE)
        if a != b:
            return False
        if not a:
            return True


def not_found(exc):
    return isinstance(exc, HTTPError) and exc.code == 404


def mirror_git(source_url, destination_url, token, after_push=None):
    # Git is isolated from user/system configuration and never receives the token
    # while contacting the source. Hooks are disabled and no checkout occurs.
    with tempfile.TemporaryDirectory(prefix='repo-mirror-') as tmp:
        env = os.environ.copy()
        env.pop('GH_TOKEN', None)
        env.update({'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                    'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': '', 'GIT_LFS_SKIP_SMUDGE': '1'})
        for key in list(env):
            if key.startswith('GIT_') and key not in ('GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL', 'GIT_TERMINAL_PROMPT', 'GIT_ASKPASS', 'GIT_LFS_SKIP_SMUDGE'):
                del env[key]
        def run(args, config_env=None):
            e = env.copy()
            if config_env:
                e.update(config_env)
            result = subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'protocol.file.allow=never', *args],
                                    env=e, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return result.stdout
        run(['init', '--bare', tmp])
        run(['-C', tmp, 'fetch', '--no-tags', '--force', source_url, '+refs/heads/*:refs/heads/*', '+refs/tags/*:refs/tags/*'])
        if not run(['-C', tmp, 'for-each-ref', '--format=%(refname)', 'refs/heads', 'refs/tags']):
            return False
        auth = base64.b64encode(('x-access-token:' + token).encode()).decode()
        credentials = {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'http.https://github.com/.extraheader',
                       'GIT_CONFIG_VALUE_0': 'AUTHORIZATION: Basic ' + auth}
        refspecs = ['+refs/heads/*:refs/heads/*', '+refs/tags/*:refs/tags/*']
        run(['-C', tmp, 'push', '--force', destination_url, *refspecs], credentials)
        if after_push:
            after_push()
        run(['-C', tmp, 'push', '--force', '--prune', destination_url, *refspecs], credentials)
        return True



class ReleaseRecoveryError(RuntimeError):
    MESSAGES = {
        'marker': 'invalid pending release marker; manually inspect the pending GitHub release and reconcile it before retrying',
        'changed': 'source release assets changed during pending sync; manually review the pending GitHub release and source, reconcile or recreate the release, then retry',
        'mismatch': 'release asset mismatch; refusing to delete or replace; manually compare and reconcile the pending GitHub release assets before retrying',
    }

    def __init__(self, reason):
        super().__init__(self.MESSAGES[reason])


class StarterAssetError(ReleaseRecoveryError):
    def __init__(self):
        RuntimeError.__init__(self, 'starter release asset detected; manually remove the starter asset from the pending GitHub release, then rerun')


class FailureJournal:
    """Persist only hashed identifiers, never URLs, error messages or credentials."""
    def __init__(self, path):
        self.path = os.fspath(path)
        if os.path.islink(self.path):
            raise ValueError('failure journal must not be a symlink')
        try:
            with open(self.path, encoding='utf-8') as stream:
                data = json.load(stream)
        except FileNotFoundError:
            data = {'version': 1, 'failures': []}
        if (not isinstance(data, dict) or set(data) != {'version', 'failures'} or
                data['version'] != 1 or not isinstance(data['failures'], list) or
                any(not isinstance(x, str) or not re.fullmatch(r'[0-9a-f]{64}:[0-9a-f]{64}', x) for x in data['failures'])):
            raise ValueError('invalid failure journal')
        self.failures = set(data['failures'])

    @staticmethod
    def key(source, name, owner, tag, asset):
        identity = [source['type'], source['base_url'].rstrip('/').lower(), source['owner'], name, owner, tag]
        digest = lambda value: hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
        return digest(identity) + ':' + digest(asset)

    def retry_order(self, source, name, owner, tag, assets):
        return sorted(assets, key=lambda asset: self.key(source, name, owner, tag, asset['name']) not in self.failures)

    def set(self, key, failed):
        if failed:
            self.failures.add(key)
        else:
            self.failures.discard(key)
        directory = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(prefix='.failures-', dir=directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'version': 1, 'failures': sorted(self.failures)}, stream, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def mirror_repository(source, name, owner, src, dst, token, account_type='organization', journal=None):
    dest_name = destination_name(source, name)
    marker = source_marker(source, name)
    qowner, qname = quote(owner, safe=''), quote(dest_name, safe='')
    target = f'{GITHUB}/repos/{qowner}/{qname}'
    repo = get_existing(dst, target)
    check_existing(repo, marker)
    initializing = repo is None or repo.get('description') == marker + ' [repo-mirror:initializing:v1]'
    if repo is None:
        endpoint = '/user/repos' if account_type == 'personal' else f'/orgs/{qowner}/repos'
        repo = dst.request('POST', GITHUB + endpoint, {'name': dest_name, 'private': True, 'auto_init': False, 'description': marker + ' [repo-mirror:initializing:v1]'})
        if repo.get('full_name', '').lower() != (owner + '/' + dest_name).lower():
            raise RuntimeError('created repository owner does not match configured destination')
    dst.request('PUT', target + '/actions/permissions', {'enabled': False})
    srcbase = source_api_base(source)
    upstream = f"{srcbase}/repos/{quote(source['owner'], safe='')}/{quote(name, safe='')}"
    metadata = src.request('GET', upstream)
    clone_url = metadata['clone_url']
    parsed = urlparse(clone_url)
    base = urlparse(source['base_url'])
    allowed_host = 'github.com' if source['type'] == 'github' and base.hostname == 'api.github.com' else base.hostname
    if parsed.scheme != 'https' or parsed.hostname != allowed_host or parsed.username or parsed.password:
        raise ValueError('source clone URL does not match source host')
    default_branch = metadata.get('default_branch')
    def update_default():
        if not default_branch:
            raise RuntimeError('source default branch missing; refusing to prune')
        if repo.get('default_branch') != default_branch:
            dst.request('PATCH', target, {'default_branch': default_branch})
    mirror_git(clone_url, f'https://github.com/{qowner}/{qname}.git', token, update_default)
    releases = src.pages(upstream + '/releases')
    if initializing or len(releases) <= 10:
        selected = releases
    else:
        pending_tags = {r.get('tag_name') for r in dst.pages(target + '/releases')
                        if isinstance(r.get('name'), str) and r['name'].startswith(
                            ('[repo-mirror:pending:v1]', '[repo-mirror:pending:v2]'))}
        failed_releases = {key.split(':', 1)[0] for key in journal.failures} if journal else set()
        selected = releases[:10] + [r for r in releases[10:]
            if r['tag_name'] in pending_tags or (journal and journal.key(
                source, name, owner, r['tag_name'], '')[:64] in failed_releases)]
    for release in selected:
        tag = release['tag_name']
        try:
            existing = dst.request('GET', target + '/releases/tags/' + quote(tag, safe=''))
        except HTTPError as e:
            if not not_found(e):
                raise
            e.close()
            # The tag endpoint can omit drafts, which still need to be found.
            matches = [r for r in dst.pages(target + '/releases') if r.get('tag_name') == tag]
            if len(matches) > 1:
                raise RuntimeError('multiple destination releases for tag')
            existing = matches[0] if matches else None
        prefix_v1 = '[repo-mirror:pending:v1]'
        prefix_v2 = '[repo-mirror:pending:v2]'
        current_name = existing.get('name') if existing else None
        pending = isinstance(current_name, str) and current_name.startswith((prefix_v1, prefix_v2))
        prefix = prefix_v2 if pending and current_name.startswith(prefix_v2) else prefix_v1
        asset_url = upstream + '/releases/' + str(release['id']) + '/assets'
        def fetch_source_assets():
            assets = (src.request('GET', asset_url) if source['type'] == 'forgejo'
                      else src.pages(asset_url))
            if not isinstance(assets, list):
                raise ValueError('release assets response is not an array')
            manifest = sorted([[a.get(k) for k in ('name', 'size', 'id', 'updated_at', 'browser_download_url')]
                               for a in assets], key=lambda a: str(a[0]))
            digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
            return assets, digest
        source_assets, digest = fetch_source_assets()
        if existing and pending:
            pending_name = existing['name']
            try:
                original_name, expected_digest = json.loads(base64.urlsafe_b64decode(pending_name[len(prefix):].encode()).decode())
                if (original_name is not None and not isinstance(original_name, str)) or not isinstance(expected_digest, str):
                    raise ValueError()
            except (ValueError, UnicodeError, TypeError):
                raise ReleaseRecoveryError('marker') from None
            if digest != expected_digest:
                raise ReleaseRecoveryError('changed')
        else:
            original_name = release.get('name')
            if isinstance(original_name, str) and original_name.startswith((prefix_v1, prefix_v2)):
                raise ValueError('source release name uses reserved marker')
            prefix = prefix_v2 if existing else prefix_v1
            encoded = base64.urlsafe_b64encode(json.dumps([original_name, digest], ensure_ascii=True).encode()).decode()
            pending_name = prefix + encoded
            if len(pending_name) > 255:
                raise ValueError('release name too long for pending marker')
            fields = {k: release.get(k) for k in ('body', 'draft', 'prerelease')}
            fields.update(tag_name=tag, name=pending_name)
            if existing:
                dst.request('PATCH', target + '/releases/' + str(existing['id']), {'name': pending_name})
            else:
                existing = dst.request('POST', target + '/releases', fields)
        assets = dst.pages(target + '/releases/' + str(existing['id']) + '/assets')
        if any(asset.get('state') == 'starter' for asset in assets):
            raise StarterAssetError()
        by_name = {asset['name']: asset for asset in assets}
        if len(by_name) != len(assets) or len({a['name'] for a in source_assets}) != len(source_assets):
            raise ReleaseRecoveryError('mismatch')
        upload_failures = 0
        ordered_assets = journal.retry_order(source, name, owner, tag, source_assets) if journal else source_assets
        for asset in ordered_assets:
            key = journal.key(source, name, owner, tag, asset['name']) if journal else None
            previous = by_name.get(asset['name'])
            with tempfile.TemporaryFile() as data:
                src.download(asset['browser_download_url'], data)
                if not isinstance(asset.get('size'), int) or data.tell() != asset['size']:
                    raise RuntimeError('source asset size mismatch')
                changed = False
                if previous:
                    with tempfile.TemporaryFile() as existing_data:
                        dst.download(previous['url'], existing_data)
                        changed = not same_file(data, existing_data)
                        if changed and prefix != prefix_v2:
                            raise ReleaseRecoveryError('mismatch')
                if changed:
                    _, current_digest = fetch_source_assets()
                    if current_digest != digest:
                        raise ReleaseRecoveryError('changed')
                    asset_id = previous.get('id')
                    if not isinstance(asset_id, int) or isinstance(asset_id, bool) or asset_id <= 0:
                        raise ReleaseRecoveryError('mismatch')
                    if journal:
                        journal.set(key, True)
                    dst.request('DELETE', target + '/releases/assets/' + str(asset_id))
                if not previous or changed:
                    data.seek(0)
                    upload_url = existing['upload_url'].split('{', 1)[0] + '?' + urlencode({'name': asset['name']})
                    if journal:
                        journal.set(key, True)
                    try:
                        dst.upload(upload_url, data, asset.get('content_type') or 'application/octet-stream')
                    except Exception:
                        if journal is None:
                            raise
                        upload_failures += 1
                        continue

        if upload_failures:
            raise RuntimeError('release asset uploads failed')
        _, final_digest = fetch_source_assets()
        if final_digest != digest:
            raise ReleaseRecoveryError('changed')
        final_assets = dst.pages(target + '/releases/' + str(existing['id']) + '/assets')
        if any(asset.get('state') == 'starter' for asset in final_assets):
            raise StarterAssetError()
        expected = {asset['name']: asset['size'] for asset in source_assets}
        actual = {asset['name']: asset for asset in final_assets}
        if (len(expected) != len(source_assets) or len(actual) != len(final_assets) or
                not expected.keys() <= actual.keys() or
                any(actual[name].get('state') != 'uploaded' or actual[name].get('size') != size
                    for name, size in expected.items())):
            raise ReleaseRecoveryError('mismatch')
        dst.request('PATCH', target + '/releases/' + str(existing['id']),
                    {'name': release.get('name'), **{k: release[k] for k in ('body', 'draft', 'prerelease') if k in release}})
        if journal:
            for asset in source_assets:
                key = journal.key(source, name, owner, tag, asset['name'])
                if key in journal.failures:
                    journal.set(key, False)
    if initializing:
        dst.request('PATCH', target, {'description': marker})



def error_detail(exc):
    # HTTPError URLs, reasons and bodies can contain credentials or attacker data.
    if isinstance(exc, HTTPError):
        if type(exc.code) is int and 100 <= exc.code <= 599:
            return f'HTTP {exc.code}'
        return 'HTTPError'
    return str(exc) if isinstance(exc, ReleaseRecoveryError) else type(exc).__name__


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--failures')
    args = parser.parse_args(argv)
    journal = FailureJournal(args.failures) if args.failures else None
    token, owner = os.environ.get('GH_TOKEN'), os.environ.get('DESTINATION_OWNER')
    if not token or not owner or not NAME.fullmatch(owner) or owner in ('.', '..'):
        raise ValueError('GH_TOKEN and valid DESTINATION_OWNER are required')
    with open(args.config, encoding='utf-8') as f:
        config = json.load(f)
    if not isinstance(config, dict):
        raise ValueError('config must be an object')
    account_type = os.environ.get('DESTINATION_ACCOUNT_TYPE', config.get('destination_account_type'))
    if account_type not in ('personal', 'organization'):
        raise ValueError('destination_account_type must be personal or organization')
    sources = config.get('sources')
    if not isinstance(sources, list):
        raise ValueError('sources must be a list')
    for source in sources:
        validate_source(source)
    src, dst = API(), API(token)
    seen = set()
    plan = []
    discovery_failures = 0
    uncertain_prefixes = []
    uncertain_names = set()
    for index, source in enumerate(sources, 1):
        try:
            names = discover(source, src)
        except Exception as exc:
            discovery_failures += 1
            if source['repos'] == 'all':
                uncertain_prefixes.append(source.get('prefix', 'mirror-' + source['owner'] + '-').lower())
            else:
                uncertain_names.update(destination_name(source, name).lower() for name in source['repos'])
            print(f"source discovery failed for source {index} ({source['owner']}): {error_detail(exc)}", file=sys.stderr)
            continue
        for name in names:
            target = destination_name(source, name).lower()
            if target in seen:
                raise ValueError('duplicate destination repository: ' + target)
            seen.add(target)
            plan.append((source, name))
    failures = []
    ready = []
    for source, name in plan:
        dest = destination_name(source, name)
        if dest.lower() in uncertain_names or any(dest.lower().startswith(prefix) for prefix in uncertain_prefixes):
            failures.append((dest, 'possible collision with undiscovered source; retry discovery or use distinct prefixes'))
            continue
        target = f"{GITHUB}/repos/{quote(owner, safe='')}/{quote(dest, safe='')}"
        try:
            check_existing(get_existing(dst, target), source_marker(source, name))
            ready.append((source, name))
        except Exception as exc:
            failures.append((destination_name(source, name), error_detail(exc)))
    for source, name in ready:
        try:
            if journal is None:
                mirror_repository(source, name, owner, src, dst, token, account_type)
            else:
                mirror_repository(source, name, owner, src, dst, token, account_type, journal=journal)
        except Exception as exc:
            detail = error_detail(exc)
            failures.append((destination_name(source, name), detail))
    if failures or discovery_failures:
        for name, kind in failures:
            print(f'mirror failed for {name}: {kind}', file=sys.stderr)
        raise RuntimeError(f'{discovery_failures} sources and {len(failures)} repositories failed')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Avoid printing subprocess stderr, URLs, response bodies or credentials.
        detail = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
        print('mirror failed: ' + detail, file=sys.stderr)
        sys.exit(1)
