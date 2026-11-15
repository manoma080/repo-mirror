"""Explicit, synchronous GitHub mirror to local Forgejo runner."""
import argparse
import fcntl
import json
from contextlib import contextmanager
import os
import errno
import socket
import time
import re
import stat
import sys
import urllib.error
import http.client
import hashlib

from repo_mirror.forgejo_git import APIError, ForgejoDestination, LocalForgejoAPI, sync_refs, snapshot_refs
from repo_mirror.forgejo_releases import SyncConflict
from repo_mirror.forgejo_destination_adapter import ForgejoDestinationAdapter
from repo_mirror.forgejo_source_adapter import ForgejoSourceAdapter
from repo_mirror.github_api import SourceAPI
from repo_mirror.journal import FailureJournal
from repo_mirror.repository import (ReleaseAggregateError, mirror_releases, validate_source_release_listing,
                                    fetch_source_latest, validate_source_latest)
from repo_mirror.sources import source_marker, source_api_base
from repo_mirror.forgejo_local import forgejo_source_identity
from repo_mirror.forgejo_source_adapter import PublicForgejoSourceAdapter
from repo_mirror.github_api import AppTokenProvider

CONFLICT_REASONS = frozenset({
    'invalid API identifier', 'ambiguous asset names', 'source asset not uploaded',
    'invalid or unsupported asset size', 'asset size mismatch', 'source asset digest mismatch',
    'asset download failed', 'asset content length mismatch', 'invalid asset content length',
    'ambiguous release tag', 'incomplete release metadata',
    'existing draft identity requires reconciliation', 'release metadata collision',
    'uploaded asset not visible', 'asset collision or upload corruption',
    'source assets changed during sync', 'source release changed during sync',
    'release verification failed', 'source asset download failed',
    'destination asset download failed', 'response exceeds size limit',
    'invalid release listing', 'release pagination limit exceeded', 'invalid asset listing',
    'Forgejo latest release cannot be selected', 'unsupported latest operation',
    'unsafe Forgejo asset URL', 'asset upload failed; reconcile destination before retry',
    'Forgejo empty source body cannot be safely mirrored; inspect any pending release and reconcile manually before retrying',
})
GIT_REASONS = frozenset({
    'Forgejo repository identity, provenance, privacy or Actions not verified',
    'destination ref diverged or tag moved; refusing update',
    'Git operation failed; details suppressed to protect credentials',
    'invalid Git ref listing', 'Forgejo ref readback did not match requested update',
    'GITHUB_TOKEN required for source clone', 'source default branch not verified',
    'source default branch changed during sync', 'source refs changed during sync',
    'Forgejo refs changed during sync', 'Forgejo ref readback did not match source snapshot',
    'Forgejo default branch readback did not match',
    'Forgejo repository identity or initialization state changed',
})
CONFIG_REASONS = frozenset({
    'invalid source URL', 'config must be a JSON array of source URLs',
    'duplicate configured source', 'duplicate requested source', 'conflicting destination',
    'requested source not exactly configured',
    'GITHUB_TOKEN required; run under the Manobot app helper', 'invalid port',
    'Forgejo token file must be owner-only and owned by current user',
    'invalid Forgejo token', 'invalid repository identity',
})
SAFE_REASONS = CONFLICT_REASONS | GIT_REASONS | CONFIG_REASONS | frozenset({
    'configuration file or token file inaccessible', 'invalid configuration',
    'Forgejo API authentication or authorization failed', 'Forgejo API resource not found',
    'Forgejo API request failed', 'remote API authentication or authorization failed',
    'remote API resource not found', 'remote API request failed',
    'network or API transport failure', 'unexpected sync failure',
})


def safe_reason(exc, *, configuration=False):
    if type(exc) is ReleaseAggregateError:
        # Classify only homogeneous, explicitly recognized conflict failures;
        # never promote an arbitrary message or hide mixed failures.
        errors = exc.release_errors
        if errors and all(type(error) is SyncConflict and len(error.args) == 1
                          and type(error.args[0]) is str and error.args[0] in CONFLICT_REASONS
                          and error.args[0] == errors[0].args[0] for error in errors):
            return errors[0].args[0]
        return 'unexpected sync failure'
    message = str(exc)
    if isinstance(exc, SyncConflict) and message in CONFLICT_REASONS:
        return message
    if type(exc) is RuntimeError and message in GIT_REASONS:
        return message
    if configuration and type(exc) is ValueError and message in CONFIG_REASONS:
        return message
    if isinstance(exc, (APIError, urllib.error.HTTPError)):
        status = exc.status if isinstance(exc, APIError) else exc.code
        prefix = 'Forgejo API' if isinstance(exc, APIError) else 'remote API'
        if status in (401, 403):
            return prefix + ' authentication or authorization failed'
        if status == 404:
            return prefix + ' resource not found'
        return prefix + ' request failed'
    if configuration:
        return 'configuration file or token file inaccessible' if isinstance(exc, OSError) else 'invalid configuration'
    if isinstance(exc, (OSError, urllib.error.URLError, http.client.HTTPException)):
        return 'network or API transport failure'
    return 'unexpected sync failure'


def parse_source(value):
    match = re.fullmatch(r'https://github\.com/(manoma080)/(mirror-[A-Za-z0-9_][A-Za-z0-9_.-]*)', value)
    if match and not match.group(2).endswith('.git'):
        return '/'.join(match.groups())
    return forgejo_source_identity(value)


def read_secure_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('private key file must be owner-only and owned by current user')
        with os.fdopen(fd, encoding='utf-8') as stream:
            fd = -1
            return stream.read(), (info.st_dev, info.st_ino)
    finally:
        if fd != -1:
            os.close(fd)


def read_private_key(path):
    return read_secure_file(path)[0]


@contextmanager
def journal_lock(path, *, shared=False):
    # Lock the directory inode itself: no replaceable lock file, symlink or
    # credential hard-link can split the lock or cause a credential write.
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('invalid journal directory')
        fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        # Anchor journal operations to the locked inode even if its path moves.
        yield f'/proc/self/fd/{fd}'
    finally:
        os.close(fd)


# Abstract socket names live in the Linux network namespace, not the filesystem.
# Close inherited descriptors after fork: a child must not extend the parent's lock.
_destination_sockets = set()


def _close_destination_sockets_in_child():
    for sock in _destination_sockets:
        sock.close()
    _destination_sockets.clear()


os.register_at_fork(after_in_child=_close_destination_sockets_in_child)


@contextmanager
def destination_locks(port, owner, requested):
    """Serialize cooperating runners per target in the same network namespace.

    Linux abstract UNIX addresses cannot be renamed or unlinked. They disappear
    automatically on close or process death. External clients do not cooperate.
    """
    names = sorted({hashlib.sha256(
        f'http://127.0.0.1:{port}/{owner.lower()}/{identity.rsplit("/", 1)[-1].lower()}'.encode()
    ).hexdigest() for identity in requested})
    sockets = []
    try:
        for name in names:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sockets.append(sock)
            _destination_sockets.add(sock)
            address = f'\0repo-mirror-forgejo-{os.getuid()}-{name}'
            deadline = time.monotonic() + 3600
            while True:
                try:
                    sock.bind(address)
                    break
                except OSError as exc:
                    if exc.errno != errno.EADDRINUSE:
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError('destination lock timeout') from None
                    time.sleep(0.05)
        yield
    finally:
        for sock in reversed(sockets):
            _destination_sockets.discard(sock)
            sock.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='JSON array of permitted GitHub mirror URLs')
    parser.add_argument('--source', action='append', required=True, help='exact URL from config; repeat for each source')
    parser.add_argument('--token-file', required=True, help='owner-only Forgejo token file')
    parser.add_argument('--timestamp-secret-file', required=True, help='stable owner-only local signing secret file')
    parser.add_argument('--journal-dir', required=True, help='persistent owner-only journal directory')
    parser.add_argument('--owner', required=True, help='local Forgejo account')
    parser.add_argument('--organization', action='store_true')
    parser.add_argument('--port', type=int, default=3300)
    parser.add_argument('--app-id')
    parser.add_argument('--private-key-file')
    parser.add_argument('--use-github-token', action='store_true', help='explicitly use GITHUB_TOKEN (not refreshable)')
    args = parser.parse_args(argv)
    try:
        with open(args.config, encoding='utf-8') as stream:
            configured = json.load(stream)
        if not isinstance(configured, list) or not all(isinstance(x, str) for x in configured):
            raise ValueError('config must be a JSON array of source URLs')
        identities = [parse_source(s) for s in configured]
        if len(set(x.lower() for x in identities)) != len(identities):
            raise ValueError('duplicate configured source')
        destinations = [identity.rsplit('/', 1)[-1].lower() for identity in identities]
        if len(set(destinations)) != len(destinations):
            raise ValueError('conflicting destination')
        requested = [parse_source(s) for s in args.source]
        if len(set(x.lower() for x in requested)) != len(requested):
            raise ValueError('duplicate requested source')
        if any(s not in configured for s in args.source):
            raise ValueError('requested source not exactly configured')
        secret, secret_id = read_secure_file(args.timestamp_secret_file)
        secret = secret.strip()
        if not secret or any(c in secret for c in ('\n', '\r', '\x00')):
            raise ValueError('invalid signing secret')
        if len(secret.encode('utf-8')) < 32:
            raise ValueError('recovery secret too short')
        local_token, local_id = read_secure_file(args.token_file)
        local_token = local_token.strip()
        if local_id == secret_id:
            raise ValueError('recovery secret must be separate')
        if not local_token or '\n' in local_token or '\r' in local_token:
            raise ValueError('invalid Forgejo token')
        if not any(not x.startswith('https://') for x in requested) and not (args.app_id or args.private_key_file or args.use_github_token):
            token = None
        elif args.app_id and args.private_key_file and not args.use_github_token:
            key, key_id = read_secure_file(args.private_key_file)
            if key_id == secret_id:
                raise ValueError('recovery secret must be separate')
            token = AppTokenProvider(args.app_id, key, 'manoma080')
        elif args.use_github_token and not args.app_id and not args.private_key_file:
            token = os.environ.get('GITHUB_TOKEN')
            if not token or '\n' in token or '\r' in token:
                raise ValueError('GITHUB_TOKEN required; run under the Manobot app helper')
        else:
            raise ValueError('invalid source authentication configuration')
        if not 1 <= args.port <= 65535:
            raise ValueError('invalid port')
        info = os.stat(args.journal_dir, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('invalid journal directory')
        api = LocalForgejoAPI(f'http://127.0.0.1:{args.port}', args.token_file)
        # Pin the checked credential; re-opening its path could race with replacement.
        api.token = lambda: local_token
        dest = ForgejoDestination(api, args.owner, organization=args.organization)
    except (OSError, ValueError, TypeError) as exc:
        print(f'FORGEJO_FAILURE: {safe_reason(exc, configuration=True)}', file=sys.stderr)
        return 2
    try:
        # A journal name encodes the source, instance and owner, so every run
        # sharing a journal also shares the corresponding destination lock.
        # The shared directory lock remains compatible with legacy exclusive
        # journal users and pins the directory inode across renames.
        result = 0
        for identity in requested:
            with destination_locks(args.port, args.owner, [identity]):
                with journal_lock(args.journal_dir, shared=True) as journal_dir:
                    result |= _sync_requested(args, [identity], dest, token, local_token, secret, journal_dir)
        return result
    except (OSError, ValueError) as exc:
        print(f'FORGEJO_FAILURE: {safe_reason(exc, configuration=True)}', file=sys.stderr)
        return 2


def _sync_requested(args, requested, dest, token, local_token, secret, journal_dir):
    failures = []
    for identity in requested:
        name = identity.rsplit('/', 1)[-1]
        try:
            journal_id = hashlib.sha256(f'{args.port}:{args.owner.lower()}:{identity.lower()}'.encode()).hexdigest()
            journal = FailureJournal(os.path.join(journal_dir, journal_id + '.json'))
            if identity.startswith('https://'):
                base, source_owner, _ = identity.rsplit('/', 2)
                source = {'type': 'forgejo', 'base_url': base, 'owner': source_owner, 'repos': [name]}
                source_token = None
                src = PublicForgejoSourceAdapter(identity, api=SourceAPI(None))
            else:
                source = {'type': 'github', 'base_url': 'https://github.com', 'owner': identity.split('/')[0]}
                source_token = token
                src = ForgejoSourceAdapter(identity, token, api=SourceAPI(token))
            upstream = f"{source_api_base(source)}/repos/{source['owner']}/{name}"
            source_releases = src.pages(upstream + '/releases')
            validation_options = {'allow_empty_target': True} if source['type'] == 'forgejo' else {}
            validate_source_release_listing(source_releases, **validation_options)
            validate_source_latest(fetch_source_latest(src, upstream), source_releases, **validation_options)
            # This preflight cannot prevent the source changing immediately
            # afterwards; mirror_releases fetches and checks it again.
            initializing = dest.begin_initialization(name, identity)
            expected = dest.run_pin
            def guard():
                if expected is not None:
                    dest.revalidate(name, identity, expected)
            guard()
            sync_refs(dest, name, identity, source_token=source_token, expected=expected)
            guard()
            before = snapshot_refs(dest, name, identity, source_token=source_token, expected=expected)
            guard()
            adapter = ForgejoDestinationAdapter(f'{args.owner}/{name}', local_token, args.port, guard=guard)
            if source['type'] == 'forgejo':
                def verify_source_tag(tag):
                    current = snapshot_refs(dest, name, identity, source_token=source_token, expected=expected)
                    ref = 'refs/tags/' + tag
                    if current != before or ref not in current[1] or current[1][ref] != current[2].get(ref):
                        raise ValueError('tag verification failed')
                    return ('git-ref', current[1][ref])
                adapter.verify_source_tag = verify_source_tag
            previous_secret = os.environ.get('MIRROR_TIMESTAMP_SECRET')
            os.environ['MIRROR_TIMESTAMP_SECRET'] = secret
            def validate_snapshot(releases, latest):
                validate_source_release_listing(releases, **validation_options)
                validate_source_latest(latest, releases, **validation_options)

            try:
                mirror_releases(source, name, args.owner, src, adapter, upstream, adapter.target,
                                source_marker(source, name), initializing=initializing, journal=journal,
                                validate_snapshot=validate_snapshot)
            finally:
                if previous_secret is None:
                    os.environ.pop('MIRROR_TIMESTAMP_SECRET', None)
                else:
                    os.environ['MIRROR_TIMESTAMP_SECRET'] = previous_secret
            guard()
            after = snapshot_refs(dest, name, identity, source_token=source_token, expected=expected)
            guard()
            if after[0] != before[0] or after[1] != before[1]:
                raise RuntimeError('source refs changed during sync')
            if after[2] != before[2]:
                raise RuntimeError('Forgejo refs changed during sync')
            if initializing:
                dest.complete_initialization(name, identity, expected=expected)
            print(f'{identity}: OK (Git refs synchronized)', flush=True)
        except Exception as exc:
            failures.append(identity)
            print(f'{identity}: FAILED', file=sys.stderr, flush=True)
            print(f'FORGEJO_FAILURE: {safe_reason(exc)}', file=sys.stderr, flush=True)
    print(f'Summary: {len(requested) - len(failures)} succeeded, {len(failures)} failed', flush=True)
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
