#!/usr/bin/env python3
"""Mirror public GitHub/Forgejo repositories to private GitHub repositories."""
import argparse
from contextlib import contextmanager
from contextvars import ContextVar
from collections import Counter
from functools import wraps
import base64
from datetime import datetime, timezone
import json
import http.client
import hashlib
import hmac
import ipaddress
import math
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
LOG_SECRETS = ContextVar('log_secrets', default=())
REPOSITORY_TIME = ContextVar('repository_time', default=None)



def safe_label(value):
    # Logs are public: conservative allowlist, never emit URLs or suspicious values.
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.@+/-]{1,100}', value):
        return '[redacted]'
    lowered = value.lower()
    if ('secret' in lowered or 'token' in lowered or 'password' in lowered or
            'credential' in lowered or re.search(r'(?i)(?:gh[pousr]_|github_pat_|bearer|awsaccesskey|signature|akia[0-9a-z]{16}|xox[baprs]-|sk[-_])', value) or
            re.search(r'[A-Za-z0-9_=]{32,}', value) or
            re.search(r'(?<![A-Za-z0-9])(?:(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{8}-){3}(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{8}(?![A-Za-z0-9])', value) or
            re.search(r'(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', value) or
            re.search(r'(?i)[0-9a-f]{24,}', value)):
        return '[redacted]'
    # Catch long, opaque hyphenated runs without treating every long project
    # name as a credential. This bounded heuristic cannot recognize all secrets.
    for run in re.findall(r'[A-Za-z0-9_-]{32,}', value):
        chars = run.replace('-', '').replace('_', '')
        if len(chars) >= 32:
            counts = Counter(chars)
            entropy = -sum((n / len(chars)) * math.log2(n / len(chars)) for n in counts.values())
            if entropy >= 4.2:
                return '[redacted]'
    for secret in (*LOG_SECRETS.get(), os.environ.get('GH_TOKEN', ''), os.environ.get('MIRROR_TIMESTAMP_SECRET', '')):
        if callable(secret):
            secret = getattr(secret, 'token', None)
        if secret and (secret in value or value in secret and len(value) >= 8):
            return '[redacted]'
    return value


@contextmanager
def timed(label):
    start = time.monotonic()
    try:
        yield
    finally:
        print(f'{label} elapsed={time.monotonic() - start:.3f}s', file=sys.stderr, flush=True)


def timed_call(label):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with timed(label(*args, **kwargs)):
                return fn(*args, **kwargs)
        return wrapped
    return decorate

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


class AppTokenProvider:
    def __init__(self, app_id, private_key, owner, account_type='personal'):
        if not app_id.isdigit() or not private_key or not NAME.fullmatch(owner) or owner in ('.', '..') or account_type not in ('personal', 'organization'):
            raise ValueError('invalid GitHub App configuration')
        self.app_id, self.private_key, self.owner, self.account_type = app_id, private_key, owner, account_type
        self.token, self.expires = None, 0

    def __call__(self):
        if not self.token or time.time() >= self.expires - 300:
            token, expires = self._mint()
            if not isinstance(token, str) or not token.strip() or not isinstance(expires, (int, float)) or not math.isfinite(expires) or expires <= time.time() + 300:
                raise ValueError('invalid GitHub App token response')
            self.token, self.expires = token, expires
        return self.token

    def _mint(self):
        def encode(data):
            return base64.urlsafe_b64encode(data).rstrip(b'=')
        now = int(time.time())
        message = b'.'.join((encode(b'{"alg":"RS256","typ":"JWT"}'), encode(json.dumps({'iat': now - 60, 'exp': now + 540, 'iss': self.app_id}, separators=(',', ':')).encode())))
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, message)
            os.close(write_fd)
            write_fd = None
            result = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', '/dev/stdin', f'/dev/fd/{read_fd}'], input=self.private_key.encode(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, pass_fds=(read_fd,))
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
        jwt = (message + b'.' + encode(result.stdout)).decode()
        opener = build_opener(SafeRedirect())
        def request(path, method='GET'):
            # Token mint POSTs create only short-lived credentials. An ambiguous
            # failure can leave an unused token, but never retries other writes.
            for attempt in range(4):
                req = Request(GITHUB + path, method=method, data=b'{}' if method == 'POST' else None, headers={'Authorization': 'Bearer ' + jwt, 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json', 'User-Agent': 'repo-mirror'})
                try:
                    with opener.open(req, timeout=60) as response:
                        return json.load(response)
                except HTTPError as exc:
                    exc.mirror_operation = ('destination', 'app authentication', method)
                    if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                        raise
                    exc.close()
                except (URLError, TimeoutError, ConnectionError):
                    if attempt == 3:
                        raise
                print(f'app authentication retry {attempt + 1}/3', file=sys.stderr, flush=True)
                time.sleep(2 ** attempt)
        kind = 'users' if self.account_type == 'personal' else 'orgs'
        installation = request(f'/{kind}/{quote(self.owner, safe="")}/installation')
        installation_id = installation['id']
        if not isinstance(installation_id, int) or isinstance(installation_id, bool) or installation_id <= 0 or installation.get('account', {}).get('login', '').lower() != self.owner.lower():
            raise ValueError('GitHub App installation owner mismatch')
        result = request(f'/app/installations/{installation_id}/access_tokens', 'POST')
        expiry = datetime.fromisoformat(result['expires_at'].replace('Z', '+00:00'))
        if expiry.tzinfo is None:
            raise ValueError('invalid GitHub App token response')
        return result['token'], expiry.timestamp()


class API:
    def __init__(self, token=None):
        self._token = token
        self.role = 'destination' if token else 'source'
        self.opener = build_opener(SafeRedirect())

    @property
    def token(self):
        return self._token() if callable(self._token) else self._token

    def _request_token(self, method, parsed):
        return self.token if self._token else None

    def _request(self, method, url, body=None, headers=None):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.username or parsed.password:
            raise ValueError('API URL must be credential-free HTTPS')
        headers = dict(headers or {})
        headers['User-Agent'] = 'repo-mirror'
        if self._token:
            if parsed.hostname not in ('api.github.com', 'uploads.github.com') or parsed.port not in (None, 443):
                raise ValueError('refusing to send destination credentials to non-GitHub host')
        for attempt in range(4):
            attempt_headers = dict(headers)
            current = self._request_token(method, parsed)
            if current:
                attempt_headers['Authorization'] = 'Bearer ' + current
            with timed(f'network target={self.role} operation=API method={method if method in ("GET", "POST", "PUT", "PATCH", "DELETE") else "request"}'):
                try:
                    with self.opener.open(Request(url, data=body, headers=attempt_headers, method=method), timeout=60) as response:
                        return response.read()
                except HTTPError as e:
                    e.mirror_operation = (self.role, 'API', method if method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE') else 'request')
                    if method not in ('GET', 'PUT', 'PATCH', 'DELETE') or e.code not in (429, 500, 502, 503, 504) or attempt == 3:
                        raise
                except URLError:
                    if method not in ('GET', 'PUT', 'PATCH', 'DELETE') or attempt == 3:
                        raise
            print(f'request retry {attempt + 1}/3', file=sys.stderr, flush=True)
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

    @timed_call(lambda self, url, output: f"network target={self.role} operation=asset method=GET")
    def download(self, url, output):
        if self.token:
            parsed = urlparse(url)
            if (parsed.scheme != 'https' or parsed.username or parsed.password or
                    parsed.hostname not in ('api.github.com', 'uploads.github.com') or
                    parsed.port not in (None, 443)):
                raise ValueError('refusing unsafe destination asset URL')
            request = Request(url, headers={'Accept': 'application/octet-stream',
                              'User-Agent': 'repo-mirror', 'Authorization': 'Bearer ' + self.token})
            try:
                with self.opener.open(request, timeout=60) as response:
                    copy_asset(response, output)
            except HTTPError as exc:
                exc.mirror_operation = ('destination', 'asset', 'GET')
                raise
            return
        # Connect to a validated address directly; do not use a proxy or resolve twice.
        for _ in range(10):
            with timed('network target=source operation=asset-hop method=GET'):
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
                            if response.status == 403:
                                exc = HTTPError(url, 403, '', response.headers, None)
                                exc.mirror_operation = ('source', 'asset', 'GET')
                                raise exc
                            exc = RuntimeError('asset download failed')
                            exc.mirror_asset_status = response.status
                            raise exc
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


class SourceAPI(API):
    def __init__(self, token):
        super().__init__()
        self.source_token = token

    def _request_token(self, method, parsed):
        if (method == 'GET' and parsed.scheme == 'https' and not parsed.username and
                not parsed.password and parsed.hostname == 'api.github.com' and parsed.port in (None, 443)):
            return self.source_token() if callable(self.source_token) else self.source_token
        return None


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
        def credentials():
            current = token() if callable(token) else token
            auth = base64.b64encode(('x-access-token:' + current).encode()).decode()
            return {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'http.https://github.com/.extraheader',
                    'GIT_CONFIG_VALUE_0': 'AUTHORIZATION: basic ' + auth}
        refspecs = ['+refs/heads/*:refs/heads/*', '+refs/tags/*:refs/tags/*']
        run(['-C', tmp, 'push', '--force', destination_url, *refspecs], credentials())
        if after_push:
            after_push()
        run(['-C', tmp, 'push', '--force', '--prune', destination_url, *refspecs], credentials())
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


def compare_reason(previous, source_hash, target_hash, ident, entry):
    reasons = []
    if not previous:
        reasons.append('no-destination-asset')
    if not isinstance(source_hash, str) or not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', source_hash):
        reasons.append('no-source-digest')
    if not isinstance(target_hash, str) or not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', target_hash):
        reasons.append('no-destination-digest')
    if ident and not isinstance(entry, dict):
        reasons.append('no-manifest-entry')
    if ident and isinstance(entry, dict) and entry.get('timestamp') != ident.get('timestamp'):
        reasons.append('timestamp-mismatch')
    if ident is None:
        reasons.append('manifest-unavailable')
    return ','.join(reasons) or 'digest-size-or-identity-mismatch'


def timed_repository(fn):
    @wraps(fn)
    def wrapped(source, name, owner, src, dst, token, account_type='organization', journal=None):
        context = LOG_SECRETS.set((token,))
        started = time.monotonic()
        try:
            with timed(f"repository source_repo={safe_label(source.get('owner'))}/{safe_label(name)}"):
                return fn(source, name, owner, src, dst, token, account_type, journal)
        finally:
            elapsed = time.monotonic() - started
            counter = REPOSITORY_TIME.get()
            if counter is not None:
                counter[0] += elapsed
            LOG_SECRETS.reset(context)
    return wrapped


@timed_repository
def mirror_repository(source, name, owner, src, dst, token, account_type='organization', journal=None):
    print(f'repository started source_repo={safe_label(source.get("owner"))}/{safe_label(name)} target=destination', file=sys.stderr, flush=True)
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
    with timed('step git mirror'):
        mirror_git(clone_url, f'https://github.com/{qowner}/{qname}.git', token, update_default)
    with timed('step release-discovery target=source'):
        releases = src.pages(upstream + '/releases')
    try:
        latest = src.request('GET', upstream + '/releases/latest')
        if (not isinstance(latest, dict) or not latest.get('tag_name') or
                latest.get('draft') or latest.get('prerelease')):
            raise ValueError('invalid source latest release')
    except HTTPError as exc:
        if not not_found(exc):
            raise
        exc.close()
        latest = None

    if initializing or len(releases) <= 1:
        selected = list(releases)
    else:
        pending_tags = {r.get('tag_name') for r in dst.pages(target + '/releases')
                        if isinstance(r.get('name'), str) and r['name'].startswith(
                            ('[repo-mirror:pending:v1]', '[repo-mirror:pending:v2]'))}
        failed_releases = {key.split(':', 1)[0] for key in journal.failures} if journal else set()
        selected = releases[:1] + [r for r in releases[1:]
            if r['tag_name'] in pending_tags or (journal and journal.key(
                source, name, owner, r['tag_name'], '')[:64] in failed_releases)]
    if latest:
        selected = [latest if r['tag_name'] == latest['tag_name'] else r for r in selected]
        if not any(r['tag_name'] == latest['tag_name'] for r in selected):
            selected.append(latest)
    if initializing:
        # Both APIs return release lists newest first. Use timestamps when all
        # are present, otherwise preserve the reverse of their list order.
        selected.reverse()
        try:
            def created_at(release):
                value = datetime.fromisoformat((release.get('published_at') or release['created_at']).replace('Z', '+00:00'))
                if value.tzinfo is None:
                    raise ValueError('missing timezone')
                return value.astimezone(timezone.utc)
            selected.sort(key=created_at)
        except (KeyError, AttributeError, TypeError, ValueError):
            pass
    latest_id = None
    selected_tags = {r['tag_name'] for r in selected}
    skipped = sum(1 for release in releases if release['tag_name'] not in selected_tags)
    print(f'releases discovered={len(releases)} selected={len(selected)} skipped={skipped}', file=sys.stderr, flush=True)
    for release_index, release in enumerate(selected, 1):
        tag = release['tag_name']
        print(f'\n===== Release {release_index}/{len(selected)}: {safe_label(tag)} =====', file=sys.stderr, flush=True)
        print(f'release {release_index}/{len(selected)} started tag={safe_label(tag)}', file=sys.stderr, flush=True)
        with timed(f'release tag={safe_label(tag)}'):
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
            @timed_call(lambda: 'step source-manifest target=source')
            def fetch_source_assets():
                assets = (src.request('GET', asset_url) if source['type'] == 'forgejo'
                          else src.pages(asset_url))
                if not isinstance(assets, list):
                    raise ValueError('release assets response is not an array')
                manifest = []
                for a in assets:
                    entry = [a.get(k) for k in ('name', 'size', 'id', 'updated_at', 'browser_download_url')]
                    published = a.get('digest')
                    if published is not None:
                        if isinstance(published, str) and re.fullmatch(r'sha256:[0-9a-fA-F]{64}', published):
                            published = published.lower()
                        entry.append(published)
                    if a.get('created_at') is not None:
                        entry.append(a['created_at'])
                    manifest.append(entry)
                manifest.sort(key=lambda a: str(a[0]))
                previous_manifest = []
                for a in assets:
                    entry = [a.get(k) for k in ('name', 'size', 'id', 'updated_at', 'browser_download_url')]
                    published = a.get('digest')
                    if published is not None:
                        if isinstance(published, str) and re.fullmatch(r'sha256:[0-9a-fA-F]{64}', published):
                            published = published.lower()
                        entry.append(published)
                    previous_manifest.append(entry)
                previous_manifest.sort(key=lambda a: str(a[0]))
                pre_checksum_digest = hashlib.sha256(json.dumps(previous_manifest, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
                checksums = {}
                checksum_files = [a for a in assets if isinstance(a.get('name'), str) and
                                  re.fullmatch(r'SHA256SUMS(?:\.txt)?', a['name'], re.I)]
                if len(checksum_files) == 1:
                    checksum_file = checksum_files[0]
                    if type(checksum_file.get('size')) is int and 0 <= checksum_file['size'] <= 1024 * 1024:
                        with tempfile.TemporaryFile() as data:
                            src.download(checksum_file['browser_download_url'], data)
                            if data.tell() != checksum_file['size']:
                                raise RuntimeError('source asset size mismatch')
                            data.seek(0)
                            content = data.read()
                        published = checksum_file.get('digest')
                        if isinstance(published, str) and re.fullmatch(r'sha256:[0-9a-fA-F]{64}', published):
                            if hashlib.sha256(content).hexdigest() != published[7:].lower():
                                raise RuntimeError('source asset digest mismatch')
                        manifest.append(['repo-mirror-checksum-content', hashlib.sha256(content).hexdigest()])
                        previous_manifest.append(['repo-mirror-checksum-content', hashlib.sha256(content).hexdigest()])
                        try:
                            lines = content.decode('utf-8').splitlines()
                            for line in lines:
                                match = re.fullmatch(r'([0-9a-fA-F]{64}) [ *]([A-Za-z0-9_.-]+)', line)
                                if not match or match[2] in ('.', '..') or match[2] in checksums:
                                    checksums = {}
                                    break
                                checksums[match[2]] = 'sha256:' + match[1].lower()
                        except UnicodeError:
                            checksums = {}
                digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
                previous_digest = hashlib.sha256(json.dumps(previous_manifest, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
                return assets, digest, checksums, (previous_digest, pre_checksum_digest)
            source_assets, digest, checksums, previous_digest = fetch_source_assets()
            print(f'release {release_index}/{len(selected)} assets={len(source_assets)}', file=sys.stderr, flush=True)
            if existing and pending:
                print('release resuming pending update', file=sys.stderr, flush=True)
                pending_name = existing['name']
                try:
                    original_name, expected_digest = json.loads(base64.urlsafe_b64decode(pending_name[len(prefix):].encode()).decode())
                    if (original_name is not None and not isinstance(original_name, str)) or not isinstance(expected_digest, str):
                        raise ValueError()
                except (ValueError, UnicodeError, TypeError):
                    raise ReleaseRecoveryError('marker') from None
                if digest != expected_digest:
                    legacy_manifest = [[a.get(k) for k in ('name', 'size', 'id', 'updated_at', 'browser_download_url')]
                                       for a in source_assets]
                    legacy_manifest.sort(key=lambda a: str(a[0]))
                    legacy_digest = hashlib.sha256(json.dumps(legacy_manifest, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
                    if expected_digest not in (legacy_digest, *previous_digest):
                        raise ReleaseRecoveryError('changed')
                    # Old markers cannot attest to the original content digest. Only
                    # migrate when every current asset already matches the destination;
                    # otherwise an interrupted replacement cannot be distinguished
                    # safely from an unobserved source-content change.
                    old_assets = dst.pages(target + '/releases/' + str(existing['id']) + '/assets')
                    old_by_name = {a['name']: a for a in old_assets}
                    if len(old_by_name) != len(old_assets) or len({a['name'] for a in source_assets}) != len(source_assets):
                        raise ReleaseRecoveryError('mismatch')
                    for asset in source_assets:
                        previous = old_by_name.get(asset['name'])
                        if (previous is None or previous.get('state') != 'uploaded' or
                                type(asset.get('size')) is not int or asset['size'] < 0 or
                                type(previous.get('size')) is not int or previous['size'] != asset['size']):
                            raise ReleaseRecoveryError('changed')
                        with tempfile.TemporaryFile() as source_data, tempfile.TemporaryFile() as dest_data:
                            src.download(asset['browser_download_url'], source_data)
                            dst.download(previous['url'], dest_data)
                            if source_data.tell() != asset['size'] or not same_file(source_data, dest_data):
                                raise ReleaseRecoveryError('changed')
                            published = asset.get('digest')
                            if isinstance(published, str) and re.fullmatch(r'sha256:[0-9a-fA-F]{64}', published):
                                source_data.seek(0)
                                content_hash = hashlib.sha256()
                                for chunk in iter(lambda: source_data.read(CHUNK_SIZE), b''):
                                    content_hash.update(chunk)
                                if content_hash.hexdigest() != published[7:].lower():
                                    raise ReleaseRecoveryError('changed')
                    _, checked_digest, _, _ = fetch_source_assets()
                    if checked_digest != digest:
                        raise ReleaseRecoveryError('changed')
                    encoded = base64.urlsafe_b64encode(json.dumps([original_name, digest], ensure_ascii=True).encode()).decode()
                    pending_name = prefix + encoded
                    if len(pending_name) > 255:
                        raise ValueError('release name too long for pending marker')
                    dst.request('PATCH', target + '/releases/' + str(existing['id']), {'name': pending_name})
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
                fields.update(tag_name=tag, name=pending_name, make_latest='false')
                if existing:
                    dst.request('PATCH', target + '/releases/' + str(existing['id']), {'name': pending_name})
                else:
                    try:
                        existing = dst.request('POST', target + '/releases', fields)
                    except HTTPError as exc:
                        if getattr(exc, 'mirror_operation', None) == ('destination', 'API', 'POST'):
                            exc.mirror_operation = ('destination', 'release', 'create')
                        raise
            assets = dst.pages(target + '/releases/' + str(existing['id']) + '/assets')
            if any(asset.get('state') == 'starter' for asset in assets):
                raise StarterAssetError()
            by_name = {asset['name']: asset for asset in assets}
            if len(by_name) != len(assets) or len({a['name'] for a in source_assets}) != len(source_assets):
                raise ReleaseRecoveryError('mismatch')
            # A source asset with either reserved name always wins; never treat it as
            # our metadata or attempt to manage the auxiliary names in that release.
            cache_name = 'TIMESTAMPS.txt'
            stage_name = 'TIMESTAMPS.txt.repo-mirror-staging'
            secret = os.environ.get('MIRROR_TIMESTAMP_SECRET', '')
            cache_enabled = bool(secret) and not any(a.get('name') in (cache_name, stage_name) for a in source_assets)
            cache_owner = {'owner': 'repo-mirror:timestamps:v1', 'source': marker, 'tag': tag}
            cached = {}
            old_cache = by_name.get(cache_name) if cache_enabled else None
            old_content = None
            if old_cache:
                with tempfile.TemporaryFile() as f:
                    dst.download(old_cache['url'], f)
                    if f.tell() <= 1024 * 1024:
                        f.seek(0)
                        old_content = f.read()
                try:
                    parsed = json.loads(old_content) if old_content is not None else None
                    if not isinstance(parsed, dict) or any(parsed.get(k) != v for k, v in cache_owner.items()) or not isinstance(parsed.get('assets'), dict):
                        raise ValueError()
                    signature = parsed.pop('signature', None)
                    payload = json.dumps(parsed, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()
                    expected_signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
                    if not isinstance(signature, str) or not hmac.compare_digest(signature, expected_signature):
                        raise ValueError()
                    cached = parsed['assets']
                except (ValueError, TypeError, UnicodeError):
                    # Unowned or malformed assets are never overwritten.
                    cache_enabled = False
            records = {}
            def identity(asset):
                stamp = asset.get('updated_at') or asset.get('created_at')
                aid, size = asset.get('id'), asset.get('size')
                if (type(aid) is not int or aid <= 0 or not isinstance(stamp, str) or not stamp
                        or type(size) is not int or size < 0 or not isinstance(asset.get('name'), str)):
                    return None
                return {'id': aid, 'name': asset['name'], 'timestamp': stamp, 'size': size}
            upload_failures = 0
            first_upload_status = None
            uploaded = updated = skipped = retried = 0
            ordered_assets = journal.retry_order(source, name, owner, tag, source_assets) if journal else source_assets
            for asset_index, asset in enumerate(ordered_assets, 1):
                print(f'asset {asset_index}/{len(ordered_assets)} started asset_name={safe_label(asset.get("name"))}', file=sys.stderr, flush=True)
                with timed(f'asset asset_name={safe_label(asset.get("name"))}'):
                    key = journal.key(source, name, owner, tag, asset['name']) if journal else None
                    was_journaled = journal is not None and key in journal.failures
                    previous = by_name.get(asset['name'])
                    source_hash = asset.get('digest')
                    if not (isinstance(source_hash, str) and re.fullmatch(r'sha256:[0-9a-fA-F]{64}', source_hash)):
                        source_hash = checksums.get(asset['name'])
                    target_hash = previous.get('digest') if previous else None
                    ident = identity(asset) if cache_enabled else None
                    entry = cached.get(asset['name']) if ident else None
                    if (isinstance(entry, dict) and all(entry.get(k) == v for k, v in ident.items())
                            and isinstance(entry.get('sha256'), str)
                            and re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])
                            and previous and previous.get('state') == 'uploaded'
                            and type(previous.get('size')) is int and previous['size'] == ident['size']
                            and isinstance(target_hash, str) and target_hash.lower() == 'sha256:' + entry['sha256']
                            and (not source_hash or source_hash.lower() == target_hash.lower())):
                        print('asset unchanged reason=verified-timestamp-manifest', file=sys.stderr, flush=True)
                        records[asset['name']] = dict(ident, sha256=entry['sha256'])
                        skipped += 1
                        continue
                    if (type(asset.get('size')) is int and asset['size'] >= 0
                            and previous is not None and type(previous.get('size')) is int
                            and previous['size'] >= 0 and asset['size'] == previous['size']
                            and isinstance(source_hash, str) and isinstance(target_hash, str)
                            and re.fullmatch(r'sha256:[0-9a-fA-F]{64}', source_hash)
                            and re.fullmatch(r'sha256:[0-9a-fA-F]{64}', target_hash)
                            and source_hash.lower() == target_hash.lower()):
                        print(f'asset {asset_index}/{len(ordered_assets)} unchanged (published SHA-256) reason=matching-published-digests', file=sys.stderr, flush=True)
                        if ident:
                            records[asset['name']] = dict(ident, sha256=source_hash[7:].lower())
                        skipped += 1
                        continue
                    print(f'asset {asset_index}/{len(ordered_assets)} comparing downloaded bytes reason={compare_reason(previous, source_hash, target_hash, ident, entry)}', file=sys.stderr, flush=True)
                    with tempfile.TemporaryFile() as data:
                        src.download(asset['browser_download_url'], data)
                        if not isinstance(asset.get('size'), int) or data.tell() != asset['size']:
                            raise RuntimeError('source asset size mismatch')
                        if isinstance(source_hash, str) and re.fullmatch(r'sha256:[0-9a-fA-F]{64}', source_hash):
                            data.seek(0)
                            actual_hash = hashlib.sha256()
                            for chunk in iter(lambda: data.read(CHUNK_SIZE), b''):
                                actual_hash.update(chunk)
                            if actual_hash.hexdigest() != source_hash[7:].lower():
                                raise RuntimeError('source asset digest mismatch')
                        data.seek(0)
                        calculated = hashlib.sha256()
                        for chunk in iter(lambda: data.read(CHUNK_SIZE), b''):
                            calculated.update(chunk)
                        if ident:
                            records[asset['name']] = dict(ident, sha256=calculated.hexdigest())
                        changed = False
                        if previous:
                            with tempfile.TemporaryFile() as existing_data:
                                dst.download(previous['url'], existing_data)
                                if isinstance(target_hash, str) and re.fullmatch(r'sha256:[0-9a-fA-F]{64}', target_hash):
                                    existing_data.seek(0)
                                    actual_target_hash = hashlib.sha256()
                                    for chunk in iter(lambda: existing_data.read(CHUNK_SIZE), b''):
                                        actual_target_hash.update(chunk)
                                    if actual_target_hash.hexdigest() != target_hash[7:].lower():
                                        raise RuntimeError('destination asset digest mismatch')
                                with timed('step asset-byte-compare'):
                                    changed = not same_file(data, existing_data)
                                if changed and prefix != prefix_v2:
                                    raise ReleaseRecoveryError('mismatch')
                        if changed:
                            _, current_digest, _, _ = fetch_source_assets()
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
                                if was_journaled:
                                    retried += 1
                                dst.upload(upload_url, data, asset.get('content_type') or 'application/octet-stream')
                            except Exception as exc:
                                detail = error_detail(exc)
                                if isinstance(exc, HTTPError) and first_upload_status is None and type(exc.code) is int and 100 <= exc.code <= 599:
                                    first_upload_status = exc.code
                                if isinstance(exc, HTTPError):
                                    exc.close()
                                print(f'asset upload failed for asset {asset_index}/{len(ordered_assets)}: {detail}', file=sys.stderr, flush=True)
                                if journal is None:
                                    raise
                                upload_failures += 1
                                continue
                            if changed:
                                updated += 1
                            else:
                                uploaded += 1
                        else:
                            skipped += 1

            print(f'assets uploaded={uploaded} updated={updated} skipped={skipped} retried={retried} upload_failed={upload_failures}', file=sys.stderr, flush=True)
            if upload_failures:
                exc = RuntimeError('release asset uploads failed')
                exc.mirror_upload_status = first_upload_status
                raise exc
            _, final_digest, _, _ = fetch_source_assets()
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
            if cache_enabled and (records or old_cache):
                # Verify every recorded destination byte string, including uploads.
                for asset_name, record in records.items():
                    dest_asset = actual[asset_name]
                    dh = dest_asset.get('digest')
                    if not (isinstance(dh, str) and re.fullmatch(r'sha256:[0-9a-fA-F]{64}', dh)):
                        with tempfile.TemporaryFile() as f:
                            dst.download(dest_asset['url'], f)
                            if f.tell() != record['size']:
                                raise RuntimeError('destination asset digest mismatch')
                            f.seek(0)
                            h = hashlib.sha256()
                            for chunk in iter(lambda: f.read(CHUNK_SIZE), b''):
                                h.update(chunk)
                            dh = 'sha256:' + h.hexdigest()
                    if dh.lower() != 'sha256:' + record['sha256']:
                        raise RuntimeError('destination asset digest mismatch')
                document = dict(cache_owner, assets=records)
                payload = json.dumps(document, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()
                document['signature'] = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
                content = (json.dumps(document, sort_keys=True, ensure_ascii=True, separators=(',', ':')) + '\n').encode()
                if content != old_content:
                    stage = actual.get(stage_name)
                    if stage:
                        # Never remove or reuse a foreign or stale staging asset.
                        with tempfile.TemporaryFile() as f:
                            dst.download(stage['url'], f)
                            f.seek(0)
                            if f.read() != content or stage.get('state') != 'uploaded':
                                raise ReleaseRecoveryError('mismatch')
                    elif old_cache:
                        with tempfile.TemporaryFile() as f:
                            f.write(content)
                            f.seek(0)
                            u = existing['upload_url'].split('{', 1)[0] + '?' + urlencode({'name': stage_name})
                            dst.upload(u, f, 'application/json')
                        staged_assets = dst.pages(target + '/releases/' + str(existing['id']) + '/assets')
                        stages = [a for a in staged_assets if a.get('name') == stage_name]
                        if len(stages) != 1 or stages[0].get('state') != 'uploaded':
                            raise ReleaseRecoveryError('mismatch')
                        stage = stages[0]
                        with tempfile.TemporaryFile() as f:
                            dst.download(stage['url'], f)
                            f.seek(0)
                            if f.read() != content:
                                raise ReleaseRecoveryError('mismatch')
                    if stage:
                        stage_id = stage.get('id')
                        if type(stage_id) is not int or stage_id <= 0:
                            raise ReleaseRecoveryError('mismatch')
                    if old_cache:
                        asset_id = old_cache.get('id')
                        if type(asset_id) is not int or asset_id <= 0:
                            raise ReleaseRecoveryError('mismatch')
                        dst.request('DELETE', target + '/releases/assets/' + str(asset_id))
                    if stage:
                        dst.request('PATCH', target + '/releases/assets/' + str(stage_id), {'name': cache_name})
                    else:
                        with tempfile.TemporaryFile() as f:
                            f.write(content)
                            f.seek(0)
                            u = existing['upload_url'].split('{', 1)[0] + '?' + urlencode({'name': cache_name})
                            dst.upload(u, f, 'application/json')
                    verified = [a for a in dst.pages(target + '/releases/' + str(existing['id']) + '/assets')
                                if a.get('name') == cache_name]
                    if len(verified) != 1 or verified[0].get('state') != 'uploaded':
                        raise ReleaseRecoveryError('mismatch')
                    with tempfile.TemporaryFile() as f:
                        dst.download(verified[0]['url'], f)
                        f.seek(0)
                        if f.read() != content:
                            raise ReleaseRecoveryError('mismatch')
            dst.request('PATCH', target + '/releases/' + str(existing['id']),
                        {'name': release.get('name'), 'make_latest': 'false', **{k: release[k] for k in ('body', 'draft', 'prerelease') if k in release}})
            if latest and tag == latest['tag_name']:
                latest_id = existing['id']
            if journal:
                for asset in source_assets:
                    key = journal.key(source, name, owner, tag, asset['name'])
                    if key in journal.failures:
                        journal.set(key, False)
            print(f'release {release_index}/{len(selected)} completed', file=sys.stderr, flush=True)
    if latest_id is not None:
        dst.request('PATCH', target + '/releases/' + str(latest_id), {'make_latest': 'true'})
    if initializing:
        dst.request('PATCH', target, {'description': marker})



SAFE_ERROR_MESSAGES = frozenset({
    'source type must be github or forgejo', 'source base_url must be credential-free HTTPS',
    'invalid owner or prefix', 'repos must be all or a list of repository names',
    'invalid discovered repository name', 'invalid destination repository name',
    'destination collision: repository has no matching source marker', 'unsafe redirect',
    'API URL must be credential-free HTTPS', 'refusing to send destination credentials to non-GitHub host',
    'request retries exhausted', 'pagination response is not an array', 'pagination exceeded safety limit',
    'refusing unsafe destination asset URL', 'unsafe asset URL', 'asset address is not public',
    'asset redirect missing location', 'asset download failed', 'too many asset redirects',
    'asset exceeds GitHub size limit', 'failure journal must not be a symlink', 'invalid failure journal',
    'created repository owner does not match configured destination', 'source clone URL does not match source host',
    'source default branch missing; refusing to prune', 'multiple destination releases for tag',
    'invalid source latest release',
    'release assets response is not an array', 'source release name uses reserved marker',
    'release name too long for pending marker', 'source asset size mismatch', 'source asset digest mismatch',
    'destination asset digest mismatch', 'release asset uploads failed',
    'GH_TOKEN and valid DESTINATION_OWNER are required', 'config must be an object',
    'destination_account_type must be personal or organization', 'sources must be a list',
    'duplicate destination repository',
    *ReleaseRecoveryError.MESSAGES.values(),
    'starter release asset detected; manually remove the starter asset from the pending GitHub release, then rerun',
})


def error_detail(exc):
    # Never emit arbitrary exception text, URLs, response bodies or class names.
    if isinstance(exc, HTTPError):
        if type(exc.code) is int and 100 <= exc.code <= 599:
            detail = f'HTTP {exc.code}'
            operation = getattr(exc, 'mirror_operation', None)
            if type(operation) is tuple and len(operation) == 3 and all(type(part) is str for part in operation) and (operation == ('destination', 'release', 'create') or (operation[0] == 'destination' and operation[1] == 'app authentication' and operation[2] in ('GET', 'POST')) or (operation[0] in ('source', 'destination') and operation[1] in ('API', 'asset') and operation[2] in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'request'))):
                rate = None
                if exc.code == 403:
                    try:
                        rate = exc.headers.get('X-RateLimit-Remaining') if exc.headers else None
                    except Exception:
                        pass
                suffix = '; rate limit remaining=0' if type(rate) is str and rate == '0' else ''
                detail += f' ({operation[0]} {operation[1]} {operation[2]}{suffix})'
            return detail
        return 'HTTPError'
    if type(exc) is RuntimeError and exc.args == ('release asset uploads failed',):
        status = getattr(exc, 'mirror_upload_status', None)
        if type(status) is int and 100 <= status <= 599:
            return f'release asset uploads failed (first HTTP {status})'
    if type(exc) is RuntimeError and exc.args == ('asset download failed',):
        status = getattr(exc, 'mirror_asset_status', None)
        if type(status) is int and 100 <= status <= 599:
            return f'HTTP {status} (source asset GET)'
    if len(exc.args) == 1 and type(exc.args[0]) is str and exc.args[0] in SAFE_ERROR_MESSAGES:
        return exc.args[0]
    for kind in (ValueError, RuntimeError, URLError, OSError, KeyError, TypeError):
        if isinstance(exc, kind):
            return kind.__name__
    return 'Error'


def timed_main(fn):
    @wraps(fn)
    def wrapped(argv=None):
        started = time.monotonic()
        counter = [0.0]
        context = REPOSITORY_TIME.set(counter)
        try:
            return fn(argv)
        finally:
            total = time.monotonic() - started
            REPOSITORY_TIME.reset(context)
            print(f'runtime total elapsed={total:.3f}s overhead={max(0.0, total - counter[0]):.3f}s (outside repository processing)', file=sys.stderr, flush=True)
    return wrapped


@timed_main
def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--failures')
    args = parser.parse_args(argv)
    journal = FailureJournal(args.failures) if args.failures else None
    token, owner = os.environ.get('GH_TOKEN'), os.environ.get('DESTINATION_OWNER')
    app_id = os.environ.get('MIRROR_APP_ID')
    private_key = os.environ.pop('MIRROR_APP_PRIVATE_KEY', None)
    if (not token and not (app_id and private_key)) or not owner or not NAME.fullmatch(owner) or owner in ('.', '..'):
        raise ValueError('GH_TOKEN and valid DESTINATION_OWNER are required')
    with open(args.config, encoding='utf-8') as f:
        config = json.load(f)
    if not isinstance(config, dict):
        raise ValueError('config must be an object')
    account_type = os.environ.get('DESTINATION_ACCOUNT_TYPE', config.get('destination_account_type'))
    if account_type not in ('personal', 'organization'):
        raise ValueError('destination_account_type must be personal or organization')
    if app_id or private_key:
        token = AppTokenProvider(app_id or '', private_key, owner, account_type)
    sources = config.get('sources')
    if not isinstance(sources, list):
        raise ValueError('sources must be a list')
    for source in sources:
        validate_source(source)
    github_src, other_src, dst = SourceAPI(token), API(), API(token)
    seen = set()
    plan = []
    discovery_failures = 0
    uncertain_prefixes = []
    uncertain_names = set()
    for index, source in enumerate(sources, 1):
        src = github_src if source['type'] == 'github' else other_src
        print(f'\n===== Source discovery {index}/{len(sources)}: {safe_label(source.get("owner"))} =====', file=sys.stderr, flush=True)
        print(f'source {index}/{len(sources)} discovery started', file=sys.stderr, flush=True)
        try:
            with timed(f'step source-discovery source={index}'):
                names = discover(source, src)
        except Exception as exc:
            discovery_failures += 1
            if source['repos'] == 'all':
                uncertain_prefixes.append(source.get('prefix', 'mirror-' + source['owner'] + '-').lower())
            else:
                uncertain_names.update(destination_name(source, name).lower() for name in source['repos'])
            print(f'source discovery failed for source {index}: {error_detail(exc)}', file=sys.stderr, flush=True)
            continue
        print(f'source {index}/{len(sources)} discovery completed discovered={len(names)}', file=sys.stderr, flush=True)
        for name in names:
            target = destination_name(source, name).lower()
            if target in seen:
                raise ValueError('duplicate destination repository')
            seen.add(target)
            plan.append((index, source, name))
    failures = []
    ready = []
    for repo_index, (source_index, source, name) in enumerate(plan, 1):
        dest = destination_name(source, name)
        if dest.lower() in uncertain_names or any(dest.lower().startswith(prefix) for prefix in uncertain_prefixes):
            failures.append((repo_index, 'possible collision with undiscovered source; retry discovery or use distinct prefixes'))
            continue
        target = f"{GITHUB}/repos/{quote(owner, safe='')}/{quote(dest, safe='')}"
        try:
            with timed(f'step destination-preflight source_repo={safe_label(source.get("owner"))}/{safe_label(name)} target=destination'):
                check_existing(get_existing(dst, target), source_marker(source, name))
            ready.append((repo_index, source_index, source, name))
        except Exception as exc:
            failures.append((repo_index, error_detail(exc)))
    succeeded = 0
    for repo_index, source_index, source, name in ready:
        src = github_src if source['type'] == 'github' else other_src
        print(f'\n===== Repository {repo_index}/{len(plan)}: {safe_label(source.get("owner"))}/{safe_label(name)} =====', file=sys.stderr, flush=True)
        print(f'repository {repo_index}/{len(plan)} started (source {source_index}/{len(sources)})', file=sys.stderr, flush=True)
        try:
            if journal is None:
                mirror_repository(source, name, owner, src, dst, token, account_type)
            else:
                mirror_repository(source, name, owner, src, dst, token, account_type, journal=journal)
            succeeded += 1
            print(f'repository {repo_index}/{len(plan)} completed', file=sys.stderr, flush=True)
        except Exception as exc:
            detail = error_detail(exc)
            failures.append((repo_index, detail))
            print(f'repository {repo_index}/{len(plan)} failed: {detail}', file=sys.stderr, flush=True)
    print('\n===== Summary =====', file=sys.stderr, flush=True)
    print(f'summary sources={len(sources)} discovery_failed={discovery_failures} repositories={len(plan)} succeeded={succeeded} failed={len(failures)}', file=sys.stderr, flush=True)
    if failures or discovery_failures:
        for repo_index, kind in failures:
            source_index, source, name = plan[repo_index - 1]
            print(f'mirror failed for repository {repo_index} (source {source_index}/{len(sources)}, {safe_label(source.get("owner"))}/{safe_label(name)}): {kind}', file=sys.stderr, flush=True)
        raise RuntimeError(f'{discovery_failures} sources and {len(failures)} repositories failed')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Avoid printing subprocess stderr, URLs, response bodies or credentials.
        detail = error_detail(exc)
        print('mirror failed: ' + detail, file=sys.stderr, flush=True)
        sys.exit(1)
