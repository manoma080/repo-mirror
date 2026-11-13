"""Explicit, synchronous GitHub mirror to local Forgejo runner."""
import argparse
import json
import os
import re
import stat
import sys
import urllib.error
import http.client

from repo_mirror.forgejo_git import APIError, ForgejoDestination, LocalForgejoAPI, sync_refs, snapshot_refs
from repo_mirror.forgejo_releases import ForgejoReleases, GitHubReleases, SyncConflict, sync_releases
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
    'unsafe Forgejo asset URL', 'asset upload failed; reconcile destination before retry',
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
})
CONFIG_REASONS = frozenset({
    'invalid source URL', 'config must be a JSON array of source URLs',
    'duplicate configured source', 'duplicate requested source',
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
    if not match or match.group(2).endswith('.git'):
        raise ValueError('invalid source URL')
    return '/'.join(match.groups())


def read_private_key(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('private key file must be owner-only and owned by current user')
        with os.fdopen(fd, encoding='utf-8') as stream:
            fd = -1
            return stream.read()
    finally:
        if fd != -1:
            os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='JSON array of permitted GitHub mirror URLs')
    parser.add_argument('--source', action='append', required=True, help='exact URL from config; repeat for each source')
    parser.add_argument('--token-file', required=True, help='owner-only Forgejo token file')
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
        requested = [parse_source(s) for s in args.source]
        if len(set(x.lower() for x in requested)) != len(requested):
            raise ValueError('duplicate requested source')
        if any(s not in configured for s in args.source):
            raise ValueError('requested source not exactly configured')
        if args.app_id and args.private_key_file and not args.use_github_token:
            token = AppTokenProvider(args.app_id, read_private_key(args.private_key_file), 'manoma080')
        elif args.use_github_token and not args.app_id and not args.private_key_file:
            token = os.environ.get('GITHUB_TOKEN')
            if not token or '\n' in token or '\r' in token:
                raise ValueError('GITHUB_TOKEN required; run under the Manobot app helper')
        else:
            raise ValueError('invalid source authentication configuration')
        if not 1 <= args.port <= 65535:
            raise ValueError('invalid port')
        api = LocalForgejoAPI(f'http://127.0.0.1:{args.port}', args.token_file)
        local_token = api.token()
        dest = ForgejoDestination(api, args.owner, organization=args.organization)
    except (OSError, ValueError, TypeError) as exc:
        print(f'FORGEJO_FAILURE: {safe_reason(exc, configuration=True)}', file=sys.stderr)
        return 2
    failures = []
    for identity in requested:
        name = identity.split('/')[1]
        try:
            count = sync_refs(dest, name, identity, source_token=token)
            before = snapshot_refs(dest, name, identity, source_token=token)
            sync_releases(GitHubReleases(identity, token), ForgejoReleases(f'{args.owner}/{name}', local_token, args.port))
            after = snapshot_refs(dest, name, identity, source_token=token)
            if after[0] != before[0] or after[1] != before[1]:
                raise RuntimeError('source refs changed during sync')
            if after[2] != before[2]:
                raise RuntimeError('Forgejo refs changed during sync')
            print(f'{identity}: OK ({count} refs updated)', flush=True)
        except Exception as exc:
            failures.append(identity)
            print(f'{identity}: FAILED', file=sys.stderr, flush=True)
            print(f'FORGEJO_FAILURE: {safe_reason(exc)}', file=sys.stderr, flush=True)
    print(f'Summary: {len(requested) - len(failures)} succeeded, {len(failures)} failed', flush=True)
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
