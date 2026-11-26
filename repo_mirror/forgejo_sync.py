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
from repo_mirror.staleness import DEFAULT_INACTIVE_DAYS, begin, end, parse_inactive_days, stale, stamp_date
from repo_mirror.config_json import LEGACY_CONFIG_ERROR, reject_legacy_config, unique_pairs
from repo_mirror.constants import NAME
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
SOURCE_APP_OWNER_REQUIRED = 'source GitHub App owner is required'
INSTALLATION_ID_REQUIRED = 'GitHub App installation id is required'

CONFIG_REASONS = frozenset({
    LEGACY_CONFIG_ERROR,
    SOURCE_APP_OWNER_REQUIRED,
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
    'source relocation requires durable migration state and authoritative config publication',
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
    if type(exc) is ValueError and message == 'source relocation requires durable migration state and authoritative config publication':
        return message
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


GITHUB_SOURCE = re.compile(
    r'https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9_.-]{1,100})')


def parse_source(value, source_type=None):
    """Return an owner/name identity for a public mirror source.

    GitHub sources may belong to any account, matching the GitHub runner; the
    repository must be public because the Forgejo destination mirrors anonymous
    content. Forgejo sources keep the host-qualified form.
    """
    from repo_mirror.adhoc import parse_repo_url
    kind, base, owner, name = parse_repo_url(value, source_type=source_type, allow_forgejo_port=True)
    if kind == 'gitlab':
        return f'{base}/{owner}/{name}'
    match = GITHUB_SOURCE.fullmatch(value) if isinstance(value, str) else None
    if match and not match.group(2).endswith('.git') and match.group(2) not in ('.', '..'):
        return '/'.join(match.groups())
    return forgejo_source_identity(value)



def normalize_named(sources):
    from repo_mirror.adhoc import parse_repo_url
    from repo_mirror.config_named import _GENERATED, _normalize_auto_discoveries
    from repo_mirror.constants import NAME
    from repo_mirror.gitlab_onboarding_forgejo import PREFIX_WITH_GENERATED_MESSAGE, group_origin_key, translate_groups
    from repo_mirror.host_limits import CONFIG_KEY
    reject_legacy_config(sources)
    if not isinstance(sources, dict) or set(sources) - {CONFIG_KEY, 'auto_discoveries', 'groups'} != {'destinations', 'repositories'}:
        raise ValueError('invalid named manifest')
    normalized_rules = _normalize_auto_discoveries(sources['auto_discoveries']) if 'auto_discoveries' in sources else []
    if 'groups' in sources:
        translate_groups(sources, normalized_auto_discoveries=normalized_rules)
    rules_by_url = {r['url']: r for r in normalized_rules}
    explicit_prefix_rules = [
        norm for raw, norm in zip(sources.get('auto_discoveries', []), normalized_rules)
        if 'destination_name_prefix' in raw
    ]
    explicit_prefix_urls = {r['url'] for r in explicit_prefix_rules}
    destinations, repositories = sources['destinations'], sources['repositories']
    if not isinstance(destinations, dict) or len(destinations) != 1 or not isinstance(repositories, list):
        raise ValueError('invalid named manifest')
    destination = next(iter(destinations))
    settings = destinations[destination]
    if not isinstance(destination, str) or not destination or not isinstance(settings, dict):
        raise ValueError('invalid destination')
    if 'type' in settings:
        from repo_mirror.destination_profile import validate_profile
        validate_profile(settings, 'forgejo')
    elif settings not in ({}, {'account_type': 'personal'}, {'account_type': 'organization'}):
        raise ValueError('invalid destination')
    result = []
    seen_destinations = {}
    for repo in repositories:
        if (
            not isinstance(repo, dict)
            or not {'source', 'mirrors'} <= set(repo)
            or set(repo) - {'source', 'mirrors', 'discovered_by'}
        ):
            raise ValueError('invalid repository')
        source, mirrors = repo['source'], repo['mirrors']
        if not isinstance(source, dict) or 'url' not in source or set(source) - {'url', 'id', 'type'} or not isinstance(mirrors, dict) or set(mirrors) != {destination}:
            raise ValueError('unsupported repository mapping')
        mirror = mirrors[destination]
        if not isinstance(mirror, dict) or set(mirror) - {'name', 'id', 'legacy', 'ignore_releases', 'visibility'} or not isinstance(source['url'], str):
            raise ValueError('unsupported mirror mapping')
        if 'ignore_releases' in mirror and not isinstance(mirror['ignore_releases'], bool):
            raise ValueError('unsupported mirror mapping')
        item = {'url': source['url'], 'destination_name': mirror.get('name', source['url'].rsplit('/', 1)[-1])}
        if mirror.get('ignore_releases') is True:
            item['ignore_releases'] = True
        if 'visibility' in mirror:
            if mirror['visibility'] not in ('private', 'public'):
                raise ValueError('invalid mirror visibility')
            item['visibility'] = mirror['visibility']
        if 'type' in source: item['source_type'] = source['type']
        if 'id' in source: item['source_id'] = source['id']
        if 'id' in mirror: item['destination_id'] = mirror['id']
        if 'legacy' in mirror:
            legacy = mirror['legacy']
            if not isinstance(legacy, dict) or set(legacy) != {'url', 'name', 'signature'}:
                raise ValueError('invalid legacy mapping')
            item.update(legacy_url=legacy['url'], legacy_destination_name=legacy['name'], signature=legacy['signature'])
        kind, base, owner, name = parse_repo_url(source['url'], source_type=source.get('type'), allow_forgejo_port=True)
        identity = (base.lower(), owner.lower(), name.lower())
        if 'discovered_by' in repo:
            discovered_by = repo['discovered_by']
            if not isinstance(discovered_by, str) or discovered_by not in rules_by_url:
                raise ValueError('orphan discovered_by')
            rule = rules_by_url[discovered_by]
            if kind != rule['type'] or base.lower() != rule['base_url'].lower() or owner.lower() != rule['owner'].lower():
                raise ValueError('discovered_by source mismatch')
            ov = next((v for k, v in rule['overrides'].items() if k.lower() == name.lower()), None)
            planned_name = ov['name'] if ov and 'name' in ov else f"{rule['destination_name_prefix']}-{name}"
            if len(planned_name) > 100 or not NAME.fullmatch(planned_name) or planned_name in ('.', '..'):
                raise ValueError('invalid destination repository name')
            if 'name' in mirror:
                dest_name = mirror['name']
                if dest_name != planned_name and not (rule['type'] == 'gitlab' and _GENERATED.fullmatch(dest_name)):
                    raise ValueError('discovered_by destination name mismatch')
            else:
                if _GENERATED.fullmatch(planned_name):
                    raise ValueError('invalid destination repository name')
                item['destination_name'] = planned_name
            item['discovered_by'] = discovered_by
        if 'name' in mirror and isinstance(mirror['name'], str) and _GENERATED.fullmatch(mirror['name']):
            if repo.get('discovered_by') in explicit_prefix_urls or (
                kind == 'gitlab'
                and any(
                    r['type'] == 'gitlab'
                    and group_origin_key(r['base_url']) == group_origin_key(base)
                    and r['owner'].lower() == owner.lower()
                    for r in explicit_prefix_rules
                )
            ):
                raise ValueError(PREFIX_WITH_GENERATED_MESSAGE)
        dest_key = item['destination_name'].lower()
        if dest_key in seen_destinations:
            raise ValueError('conflicting destination')
        seen_destinations[dest_key] = identity
        if 'legacy_destination_name' in item and isinstance(item['legacy_destination_name'], str):
            legacy_key = item['legacy_destination_name'].lower()
            existing_legacy_id = seen_destinations.get(legacy_key)
            if existing_legacy_id is not None and existing_legacy_id != identity:
                raise ValueError('conflicting destination')
            seen_destinations[legacy_key] = identity
        result.append(item)
    for rule in normalized_rules:
        for repo_key, ov in rule['overrides'].items():
            ov_identity = (rule['base_url'].lower(), rule['owner'].lower(), repo_key.lower())
            planned = ov['name'] if 'name' in ov else f"{rule['destination_name_prefix']}-{repo_key}"
            dest_key = planned.lower()
            existing_identity = seen_destinations.get(dest_key)
            if existing_identity is not None and existing_identity != ov_identity:
                raise ValueError('conflicting destination')
            seen_destinations[dest_key] = ov_identity
    return result


def parse_manifest(sources):
    """Validate every source and its pinned relocation mapping before any worker starts."""
    named = isinstance(sources, dict)
    if named:
        sources = normalize_named(sources)
    if not isinstance(sources, list) or (not named and not sources):
        raise ValueError('invalid source manifest')
    entries, identities, destinations, legacy_identities = [], set(), set(), set()
    for item in sources:
        if isinstance(item, str):
            url = item
            identity = parse_source(url)
            entry = {'url': url, 'identity': identity, 'destination_name': identity.rsplit('/', 1)[-1]}
        elif named and isinstance(item, dict) and set(item) <= {'url', 'destination_name', 'source_id', 'destination_id', 'source_type',
                                                                'ignore_releases', 'visibility', 'discovered_by'} and {'url', 'destination_name'} <= set(item):
            if not isinstance(item['url'], str):
                raise ValueError('invalid source URL')
            identity = parse_source(item['url'], item.get('source_type'))
            name = item['destination_name']
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', name) or name in ('.', '..') or len(name) > 100:
                raise ValueError('invalid destination name')
            for key in ('source_id', 'destination_id'):
                if key in item and (type(item[key]) is not int or item[key] <= 0):
                    raise ValueError('invalid repository ID')
            if 'ignore_releases' in item and not isinstance(item['ignore_releases'], bool):
                raise ValueError('invalid ignore-releases setting')
            entry = {**item, 'identity': identity}
        elif isinstance(item, dict) and set(item) - ({'ignore_releases', 'visibility', 'source_type', 'discovered_by'} if named else {'ignore_releases'}) == {
                'url', 'source_id', 'destination_id', 'destination_name',
                'legacy_url', 'legacy_destination_name', 'signature'}:
            if not isinstance(item['signature'], str) or not re.fullmatch(r'[0-9a-f]{64}', item['signature']):
                raise ValueError('invalid migration attestation')
            if 'ignore_releases' in item and not isinstance(item['ignore_releases'], bool):
                raise ValueError('invalid ignore-releases setting')
            if not isinstance(item['url'], str) or not isinstance(item['legacy_url'], str):
                raise ValueError('invalid source URL')
            identity = parse_source(item['url'], item.get('source_type'))
            legacy_identity = parse_source(item['legacy_url'], item.get('source_type'))
            for key in ('source_id', 'destination_id'):
                if type(item[key]) is not int or item[key] <= 0:
                    raise ValueError('invalid repository ID')
            for key in ('destination_name', 'legacy_destination_name'):
                value = item[key]
                if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', value) or value in ('.', '..') or len(value) > 100:
                    raise ValueError('invalid destination name')
            # Source IDs are scoped to a forge instance; never allow a mapping
            # to assert that the same numeric ID belongs to a different forge.
            if item['url'].rsplit('/', 2)[0].lower() != item['legacy_url'].rsplit('/', 2)[0].lower():
                raise ValueError('source instance mismatch')
            entry = {**item, 'identity': identity, 'legacy_identity': legacy_identity}
        else:
            raise ValueError('invalid source manifest')
        old_identity = entry.get('legacy_identity', identity).lower()
        names = {entry['destination_name'].lower(), entry.get('legacy_destination_name', entry['destination_name']).lower()}
        source_names = {identity.lower(), old_identity}
        if source_names & (identities | legacy_identities):
            raise ValueError('duplicate configured source')
        if names & destinations:
            raise ValueError('conflicting destination')
        identities.add(identity.lower())
        legacy_identities.add(old_identity)
        destinations.update(names)
        entries.append(entry)
    return entries


def verify_source_pin(identity, metadata, expected_id):
    """Validate an already trusted source ID and its canonical clone location."""
    if type(expected_id) is not int or expected_id <= 0 or not isinstance(metadata, dict) or type(metadata.get('id')) is not int or metadata['id'] != expected_id:
        raise ValueError('source repository ID mismatch')
    url = identity if identity.startswith('https://') else 'https://github.com/' + identity
    expected_name = identity.rsplit('/', 2)[-2:]
    actual_name, clone = metadata.get('full_name'), metadata.get('clone_url')
    if not isinstance(actual_name, str) or not isinstance(clone, str):
        raise ValueError('invalid source identity metadata')
    expected_name = '/'.join(expected_name)
    if identity.startswith('https://'):
        valid = actual_name == expected_name and clone == url + '.git'
    else:
        valid = actual_name.lower() == expected_name.lower() and clone.lower() == (url + '.git').lower()
    if not valid:
        raise ValueError('canonical source identity mismatch')


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


def read_owner_secret(path, label):
    """Read a single-line owner-only credential, rejecting whitespace tricks."""
    value = read_secure_file(path)[0].strip()
    if not value or '\n' in value or '\r' in value or any(ch.isspace() for ch in value):
        raise ValueError(f'invalid {label}')
    return value


def resolve_source_app_owner(explicit=None):
    """Resolve and validate the GitHub App installation owner for source access."""
    owner = explicit if explicit is not None else os.environ.get('MIRROR_APP_OWNER')
    if owner is None or owner == '':
        raise ValueError(SOURCE_APP_OWNER_REQUIRED)
    if not isinstance(owner, str) or not NAME.fullmatch(owner) or owner in ('.', '..'):
        raise ValueError('invalid GitHub App configuration')
    return owner


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


def parse_asset_max_bytes(value):
    from repo_mirror.constants import MAX_ASSET_SIZE
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]+', value):
        raise ValueError('invalid Forgejo asset byte limit')
    limit = int(value)
    if not 1 <= limit <= MAX_ASSET_SIZE:
        raise ValueError('invalid Forgejo asset byte limit')
    return limit


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
    parser.add_argument('--asset-max-bytes', help='operator-selected per-asset byte limit for this Forgejo destination')
    parser.add_argument('--gitlab-token-file', help='optional owner-only gitlab.com read token file')
    parser.add_argument('--inactive-after-days', default=str(DEFAULT_INACTIVE_DAYS),
                        help='report sources with no commit or release activity for this many days; 0 disables')
    parser.add_argument('--app-id')
    parser.add_argument('--private-key-file')
    parser.add_argument('--source-app-owner', help='GitHub App installation owner for source access (or MIRROR_APP_OWNER)')
    parser.add_argument('--use-github-token', action='store_true', help='explicitly use GITHUB_TOKEN (not refreshable)')
    parser.add_argument('--ignore-releases', action='store_true',
                        help='skip release synchronization for the requested --source URLs on this run only')
    parser.add_argument('--isolated-dispatch-journal', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--no-dispatch-relocation', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        args.inactive_days = parse_inactive_days(args.inactive_after_days)
        if args.asset_max_bytes is not None:
            parse_asset_max_bytes(args.asset_max_bytes)
        if args.gitlab_token_file is not None:
            os.environ['MIRROR_GITLAB_TOKEN'] = read_owner_secret(args.gitlab_token_file, 'GitLab token')
        with open(args.config, encoding='utf-8') as stream:
            configured = json.load(stream, object_pairs_hook=unique_pairs)
        entries = parse_manifest(configured)
        by_url = {entry['url']: entry for entry in entries}
        if args.asset_max_bytes is None and any(
                s in by_url and (by_url[s].get('source_type') == 'gitlab' or s.startswith('https://gitlab.com/'))
                for s in args.source):
            raise ValueError('Forgejo asset byte limit must be configured')
        requested = [parse_source(s, by_url[s].get('source_type')) if s in by_url else parse_source(s) for s in args.source]
        if len(set(x.lower() for x in requested)) != len(requested):
            raise ValueError('duplicate requested source')
        if any(s not in by_url for s in args.source):
            raise ValueError('requested source not exactly configured')
        if isinstance(configured, dict):
            settings = next(iter(configured['destinations'].values()))
            if 'type' in settings:
                from repo_mirror.destination_profile import validate_runtime
                validate_runtime(settings, 'forgejo', f'http://127.0.0.1:{args.port}', args.owner, 'forgejo-home')
                if args.organization:
                    raise ValueError('destination account type mismatch')
            if 'account_type' in settings and (settings['account_type'] == 'organization') != args.organization:
                raise ValueError('destination account type mismatch')
        mappings = {entry['identity']: entry for entry in entries if isinstance(configured, dict) or 'source_id' in entry}
        secret, secret_id = read_secure_file(args.timestamp_secret_file)
        secret = secret.strip()
        if not secret or any(c in secret for c in ('\n', '\r', '\x00')):
            raise ValueError('invalid signing secret')
        if len(secret.encode('utf-8')) < 32:
            raise ValueError('recovery secret too short')
        from repo_mirror.migration_attestation import forgejo_claim, verify
        for entry in mappings.values():
            if 'legacy_identity' in entry:
                verify(forgejo_claim(entry, args.port, args.owner), entry['signature'], secret)
        local_token, local_id = read_secure_file(args.token_file)
        local_token = local_token.strip()
        if local_id == secret_id:
            raise ValueError('recovery secret must be separate')
        if not local_token or '\n' in local_token or '\r' in local_token:
            raise ValueError('invalid Forgejo token')
        if args.source_app_owner is not None:
            resolve_source_app_owner(args.source_app_owner)
        if not any(not x.startswith('https://') for x in requested) and not (args.app_id or args.private_key_file or args.use_github_token):
            token = None
        elif args.app_id and args.private_key_file and not args.use_github_token:
            key, key_id = read_secure_file(args.private_key_file)
            if key_id == secret_id:
                raise ValueError('recovery secret must be separate')
            app_owner = resolve_source_app_owner(args.source_app_owner)
            token = AppTokenProvider(args.app_id, key, app_owner)
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
        if args.asset_max_bytes is not None:
            os.environ['MIRROR_FORGEJO_ASSET_MAX_BYTES'] = args.asset_max_bytes
        else:
            os.environ.pop('MIRROR_FORGEJO_ASSET_MAX_BYTES', None)
        # The shared directory lock remains compatible with legacy exclusive
        # journal users and pins the directory inode across renames.
        result = 0
        for identity in requested:
            entry = mappings.get(identity)
            lock_targets = ([entry['destination_name'], entry.get('legacy_destination_name', entry['destination_name'])] if entry else [identity])
            with destination_locks(args.port, args.owner, lock_targets):
                with journal_lock(args.journal_dir, shared=True) as journal_dir:
                    if entry:
                        result |= _sync_requested(args, [identity], dest, token, local_token, secret, journal_dir,
                                                  mappings={identity: entry},
                                                  configured=configured if isinstance(configured, dict) else None)
                    else:
                        result |= _sync_requested(args, [identity], dest, token, local_token, secret, journal_dir)
        return result
    except (OSError, ValueError) as exc:
        print(f'FORGEJO_FAILURE: {safe_reason(exc, configuration=True)}', file=sys.stderr)
        return 2


RELOCATION_PREFIX = 'FORGEJO_RELOCATION: '
RELOCATION_FIELDS = frozenset({'url', 'canonical', 'source_id', 'destination_id'})
INACTIVE_PREFIX = 'FORGEJO_INACTIVE: '
INACTIVE_FIELDS = frozenset({'url', 'since', 'idle_days'})


def report_inactive(url, since, idle_days):
    """Emit the machine-readable inactive-source report the scheduler consumes."""
    if (not isinstance(url, str) or not url or not isinstance(since, str) or not since
            or type(idle_days) is not int or idle_days < 0):
        raise ValueError('invalid inactive-source report')
    print(INACTIVE_PREFIX + json.dumps({'url': url, 'since': since, 'idle_days': idle_days}, sort_keys=True),
          file=sys.stderr, flush=True)


def report_relocation(relocation):
    """Emit the machine-readable relocation report the scheduler consumes."""
    if (not isinstance(relocation, dict) or set(relocation) != RELOCATION_FIELDS
            or any(not isinstance(relocation[key], str) or not relocation[key] for key in ('url', 'canonical'))
            or any(type(relocation[key]) is not int or relocation[key] <= 0 for key in ('source_id', 'destination_id'))):
        raise ValueError('invalid relocation report')
    print(RELOCATION_PREFIX + json.dumps(relocation, sort_keys=True), file=sys.stderr, flush=True)


def gitlab_tag_verifier(src, verify_ref):
    """Bridge GitLab tag validation to the already verified Git ref snapshot."""
    from urllib.parse import quote
    original = src.verify_release_tag

    def verify(tag, destination, target):
        class RefView:
            def request(self, method, url):
                if method != 'GET' or url != target + '/git/ref/tags/' + quote(tag, safe=''):
                    raise ValueError('tag verification failed')
                _, sha = verify_ref(tag)
                obj, _ = src._get(src.pinned_base + '/repository/tags/' + quote(tag, safe=''))
                if not isinstance(obj, dict) or obj.get('name') != tag or obj.get('target') != sha or 'message' not in obj:
                    raise ValueError('tag verification failed')
                kind = 'commit' if obj['message'] is None else 'tag'
                return {'ref': 'refs/tags/' + tag, 'object': {'sha': sha, 'type': kind}}
        return original(tag, RefView(), target)
    return verify


def source_pipeline(url, token, *, source_type=None, source_id=None, gitlab_token=None):
    """Return (source, source token, adapter, repository name) for one source URL."""
    from repo_mirror.adhoc import parse_repo_url
    kind, base, owner, name = parse_repo_url(url, source_type=source_type, allow_forgejo_port=True)
    if kind == 'gitlab':
        if type(source_id) is not int or source_id <= 0:
            raise ValueError('source repository ID mismatch')
        from repo_mirror.gitlab_source_adapter import GitLabSourceAdapter
        source = {'type': 'gitlab', 'base_url': base, 'owner': owner, 'name': name}
        extra = {'token': gitlab_token} if gitlab_token else {}
        return source, None, GitLabSourceAdapter(base, owner + '/' + name, source_id, **extra), name
    base, owner, name = url.rsplit('/', 2)
    if base.lower() == 'https://github.com':
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': owner}
        source_api = SourceAPI(token)
        source_api.restrict_origin('https://api.github.com')
        return source, token, ForgejoSourceAdapter(f'{owner}/{name}', token, api=source_api), name
    source = {'type': 'forgejo', 'base_url': base, 'owner': owner, 'name': name}
    return source, None, PublicForgejoSourceAdapter(url, api=SourceAPI(None)), name


def _source_url(identity, entry):
    if entry is not None:
        return entry['url']
    return identity if identity.startswith('https://') else 'https://github.com/' + identity


def resolve_relocation(configured, identity, metadata, port, owner, secret):
    """Validate a live pinned move; return (effective entry, canonical identity, report).

    The destination keeps its configured name so the worker can keep mirroring
    into the *existing* repository; only an auto-derived name follows the source.
    Publication of the signed candidate is the scheduler's job, never the worker's.
    """
    from repo_mirror.forgejo_automatic_transition import prepare_source_transition
    entries = parse_manifest(configured)
    indexes = [index for index, item in enumerate(entries) if item['identity'] == identity]
    failure = ValueError('source relocation requires durable migration state and authoritative config publication')
    if len(indexes) != 1:
        raise failure
    index = indexes[0]
    current = entries[index]
    try:
        candidate = prepare_source_transition(configured, index, metadata, port, owner, secret)
        moved = parse_manifest(candidate)[index]
    except ValueError:
        raise failure from None
    if moved['url'] == current['url'] or 'legacy_identity' not in moved:
        raise failure
    effective = {**moved, 'destination_name': current['destination_name']}
    report = {'url': current['url'], 'canonical': moved['url'],
              'source_id': current['source_id'], 'destination_id': current['destination_id']}
    return effective, moved['url'], report


def _sync_requested(args, requested, dest, token, local_token, secret, journal_dir, *, mappings=None, configured=None):
    failures = []
    inactive_count = 0
    for identity in requested:
        entry = (mappings or {}).get(identity)
        name = entry['destination_name'] if entry else identity.rsplit('/', 1)[-1]
        recovery_identity = entry.get('legacy_identity', identity) if entry else identity
        relocation = None
        activity, activity_token = begin()
        try:
            if entry and 'legacy_identity' in entry:
                from repo_mirror.migration_attestation import forgejo_claim, verify
                verify(forgejo_claim(entry, args.port, args.owner), entry.get('signature'), secret)
            journal_scope = f'{args.port}:{args.owner.lower()}:{recovery_identity.lower()}'
            if getattr(args, 'isolated_dispatch_journal', False):
                journal_id = 'dispatch-' + hashlib.sha256(f'{journal_scope}:{name.lower()}'.encode()).hexdigest()
            else:
                journal_id = hashlib.sha256(journal_scope.encode()).hexdigest()
            journal = FailureJournal(os.path.join(journal_dir, journal_id + '.json'))
            from repo_mirror.adhoc import parse_repo_url
            kind = parse_repo_url(_source_url(identity, entry), source_type=entry.get('source_type') if entry else None, allow_forgejo_port=True)[0]
            if kind == 'gitlab':
                source, source_token, src, source_name = source_pipeline(_source_url(identity, entry), token, source_type='gitlab', source_id=entry.get('source_id') if entry else None, gitlab_token=os.environ.get('MIRROR_GITLAB_TOKEN'))
            elif entry and entry.get('source_type'):
                source, source_token, src, source_name = source_pipeline(_source_url(identity, entry), token, source_type=entry['source_type'])
            else:
                source, source_token, src, source_name = source_pipeline(_source_url(identity, entry), token)
            upstream = (src.pinned_base if source['type'] == 'gitlab' else
                        f"{source_api_base(source)}/repos/{source['owner']}/{source_name}")
            # GitHub uses its historical owner/name form; Forgejo needs its origin.
            git_source = (f"{source['owner']}/{source_name}" if source['type'] == 'github'
                          else _source_url(identity, entry))
            if source['type'] == 'gitlab':
                from repo_mirror.sources import verified_source_identity
                def verify_gitlab():
                    metadata = src.project()
                    if verified_source_identity(source, source_name, entry['source_id'], metadata) != identity:
                        raise ValueError('canonical source identity mismatch')
                verify_gitlab()
            elif entry and 'source_id' in entry:
                metadata = src.request('GET', upstream)
                try:
                    verify_source_pin(identity, metadata, entry['source_id'])
                except ValueError:
                    if (configured is None or 'destination_id' not in entry
                            or getattr(args, 'no_dispatch_relocation', False)
                            or getattr(args, 'isolated_dispatch_journal', False)):
                        raise
                    # The pinned ID still answers under a new canonical name. Such
                    # a move is only actionable with a durable journal and an
                    # authoritative configuration write, so report it instead of
                    # failing closed and mirror into the existing destination.
                    entry, identity, relocation = resolve_relocation(
                        configured, identity, metadata, args.port, args.owner, secret)
                    name = entry['destination_name']
                    recovery_identity = entry['legacy_identity']
                    source, source_token, src, source_name = source_pipeline(entry['url'], token)
                    upstream = f"{source_api_base(source)}/repos/{source['owner']}/{source_name}"
                    git_source = (f"{source['owner']}/{source_name}" if source['type'] == 'github'
                                  else entry['url'])
            public_target = bool(entry and entry.get('visibility') == 'public')

            # A public repository can have authenticated-only drafts and
            # assets. The source must be read anonymously for a public mirror.
            if public_target and source['type'] == 'github':
                source, source_token, src, source_name = source_pipeline(_source_url(identity, entry), None)

            def verify_public_github():
                if public_target and source['type'] == 'github':
                    metadata = src.request('GET', upstream)
                    if not isinstance(metadata, dict):
                        raise ValueError('public mirror requires a public source')
                    sid = entry.get('source_id') if entry else None
                    verify_source_pin(identity, metadata, sid if sid is not None else metadata.get('id'))
                    if metadata.get('private') is not False:
                        raise ValueError('public mirror requires a public source')

            verify_public_github()
            ignore_releases = (bool(entry and entry.get('ignore_releases') is True)
                               or getattr(args, 'ignore_releases', False) is True)
            validation_options = {'allow_empty_target': True} if source['type'] in ('forgejo', 'gitlab') else {}
            if not ignore_releases:
                if source['type'] == 'gitlab':
                    source_releases = src.list_releases()
                    from repo_mirror.repository import select_tag_source_latest
                    select_tag_source_latest(source_releases)
                else:
                    source_releases = src.pages(upstream + '/releases')
                    validate_source_release_listing(source_releases, **validation_options)
                    validate_source_latest(fetch_source_latest(src, upstream), source_releases, **validation_options)
            # This preflight cannot prevent the source changing immediately
            # afterwards; mirror_releases fetches and checks it again.
            destination_options = {}
            if entry:
                if 'destination_id' in entry:
                    destination_options['expected_id'] = entry['destination_id']
                if 'visibility' in entry:
                    destination_options['visibility'] = entry['visibility']
            initializing = dest.begin_initialization(name, recovery_identity, **destination_options)
            expected = dest.run_pin
            def guard():
                verify_public_github()
                if source['type'] == 'gitlab':
                    verify_gitlab()
                elif entry and 'source_id' in entry and not (public_target and source['type'] == 'github'):
                    verify_source_pin(identity, src.request('GET', upstream), entry['source_id'])
                if expected is not None:
                    dest.revalidate(name, recovery_identity, expected)
            guard()
            recovery_options = {'recovery_identity': recovery_identity} if entry and 'legacy_identity' in entry else {}
            if public_target and source['type'] == 'github':
                recovery_options['anonymous_github'] = True
            sync_refs(dest, name, git_source, source_token=source_token, expected=expected, **recovery_options)
            guard()
            before = snapshot_refs(dest, name, git_source, source_token=source_token, expected=expected, **recovery_options)
            guard()
            adapter = ForgejoDestinationAdapter(f'{args.owner}/{name}', local_token, args.port, guard=guard)
            if source['type'] in ('forgejo', 'gitlab'):
                def verify_source_tag(tag):
                    current = snapshot_refs(dest, name, git_source, source_token=source_token, expected=expected, **recovery_options)
                    ref = 'refs/tags/' + tag
                    if current != before or ref not in current[1] or current[1][ref] != current[2].get(ref):
                        raise ValueError('tag verification failed')
                    return ('git-ref', current[1][ref])
                adapter.verify_source_tag = verify_source_tag
                if source['type'] == 'gitlab':
                    src.verify_release_tag = gitlab_tag_verifier(src, verify_source_tag)
            previous_secret = os.environ.get('MIRROR_TIMESTAMP_SECRET')
            os.environ['MIRROR_TIMESTAMP_SECRET'] = secret
            def validate_snapshot(releases, latest):
                if source['type'] == 'gitlab':
                    return
                validate_source_release_listing(releases, **validation_options)
                validate_source_latest(latest, releases, **validation_options)

            journal_options = {}
            if entry and 'source_id' in entry and 'destination_id' in entry:
                journal_options['journal_identity'] = (source['base_url'], entry['source_id'],
                                                       f'http://127.0.0.1:{args.port}', entry['destination_id'])
            try:
                if ignore_releases:
                    print('releases ignored by configuration', file=sys.stderr, flush=True)
                elif entry and 'legacy_identity' in entry:
                    old_owner, old_name = recovery_identity.rsplit('/', 2)[-2:]
                    old_source = {**source, 'owner': old_owner}
                    old_target = adapter.target.rsplit('/', 1)[0] + '/' + entry['legacy_destination_name']
                    mirror_releases(source, source_name, args.owner, src, adapter, upstream, adapter.target,
                                    source_marker(old_source, old_name), initializing=initializing, journal=journal,
                                    validate_snapshot=validate_snapshot, recovery_target=old_target,
                                    journal_source=old_source, journal_name=old_name, **journal_options)
                else:
                    mirror_releases(source, source_name, args.owner, src, adapter, upstream, adapter.target,
                                    source_marker(source, source_name), initializing=initializing, journal=journal,
                                    validate_snapshot=validate_snapshot, **journal_options)
            finally:
                if previous_secret is None:
                    os.environ.pop('MIRROR_TIMESTAMP_SECRET', None)
                else:
                    os.environ['MIRROR_TIMESTAMP_SECRET'] = previous_secret
            guard()
            after = snapshot_refs(dest, name, git_source, source_token=source_token, expected=expected, **recovery_options)
            guard()
            if after[0] != before[0] or after[1] != before[1]:
                raise RuntimeError('source refs changed during sync')
            if after[2] != before[2]:
                raise RuntimeError('Forgejo refs changed during sync')
            if initializing:
                dest.complete_initialization(name, recovery_identity, expected=expected)
            if relocation is not None:
                report_relocation(relocation)
            idle = stale(activity, getattr(args, 'inactive_days', DEFAULT_INACTIVE_DAYS))
            if idle is not None:
                idle_days, stamp = idle
                inactive_count += 1
                # The scheduler matches this against the configured URL list, so
                # report the configured form rather than the owner/name key.
                report_inactive(_source_url(identity, entry), stamp_date(stamp), idle_days)
            print(f'{identity}: OK (Git refs synchronized)', flush=True)
        except Exception as exc:
            failures.append(identity)
            print(f'{identity}: FAILED', file=sys.stderr, flush=True)
            print(f'FORGEJO_FAILURE: {safe_reason(exc)}', file=sys.stderr, flush=True)
        finally:
            end(activity_token)
    summary = f'Summary: {len(requested) - len(failures)} succeeded, {len(failures)} failed'
    if inactive_count:
        summary += f', {inactive_count} inactive'
    print(summary, flush=True)
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
