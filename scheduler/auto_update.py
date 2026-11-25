#!/usr/bin/env python3
"""Fetch remote main and run its scheduler and manifest from one private snapshot.

Install a copy of this launcher outside the checkout. All paths and the remote
are trusted administrator configuration; never put credentials in the URL.
"""
import argparse
import base64
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit

SOURCE = re.compile(r'https://github\.com/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}')
FORGEJO_SOURCE = re.compile(r'https://[A-Za-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9_][A-Za-z0-9_-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*')
DEFAULT_HELPER = Path.home() / '.hermes/workspace/github-app-manobot/github_app.py'


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def valid_source(source, source_type=None):
    if not isinstance(source, str) or source.endswith('.git'):
        return False
    if SOURCE.fullmatch(source):
        return True
    if source_type == 'gitlab' or source.startswith('https://gitlab.com/'):
        if not re.fullmatch(r'https://[A-Za-z0-9.-]+(?::[0-9]+)?(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*){2,}', source):
            return False
        if any(part in ('.', '..') for part in urlsplit(source).path.split('/')[1:]):
            return False
    elif not FORGEJO_SOURCE.fullmatch(source):
        return False
    try:
        parsed = urlsplit(source)
        host, port = parsed.hostname, parsed.port
        return bool(host and host != 'github.com' and not host.startswith('github.com.') and port != 0 and all(
            re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
            for label in host.split('.')))
    except ValueError:
        return False


def valid_profile(settings):
    """Standalone copy: the fetched checkout is not trusted before validation."""
    if not isinstance(settings, dict) or set(settings) != {'type', 'url', 'owner', 'auth'}:
        return False
    if settings.get('type') != 'forgejo' or not all(isinstance(settings[k], str) and settings[k] for k in ('url', 'owner', 'auth')):
        return False
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', settings['owner']) or settings['owner'] in ('.', '..') or not re.fullmatch(r'[a-z][a-z0-9-]*', settings['auth']):
        return False
    try:
        u = urlsplit(settings['url'])
        return bool(u.scheme in ('http', 'https') and u.hostname and not u.username and not u.password and not u.path and not u.query and not u.fragment and u.port != 0)
    except ValueError:
        return False


_AUTO_DISCOVERY_NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]*')
_AUTO_DISCOVERY_HOST_LABEL = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?')
_AUTO_DISCOVERY_GENERATED = re.compile(r'gitlab-[0-9a-f]{24}', re.IGNORECASE)
_AUTO_DISCOVERY_KEYS = frozenset({
    'url', 'type', 'id', 'visibility', 'ignore_releases',
    'destination_name_prefix', 'exclude', 'overrides',
})
_AUTO_DISCOVERY_OVERRIDE_KEYS = frozenset({'name', 'visibility', 'ignore_releases'})


def _origin_key(base_url):
    parsed = urlsplit(base_url)
    return (parsed.hostname.lower(), parsed.port or 443)


def _parse_owner_url(url, source_type=None):
    if not isinstance(url, str) or not url:
        return None
    if source_type is not None and (not isinstance(source_type, str) or source_type not in ('github', 'forgejo', 'gitlab')):
        return None
    if not all(33 <= ord(c) <= 126 for c in url):
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname
    if (
        parsed.scheme != 'https'
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or '@' in parsed.netloc
        or ':' in parsed.netloc
        or port is not None
        or host.startswith('github.com.')
        or not all(_AUTO_DISCOVERY_HOST_LABEL.fullmatch(label) for label in host.split('.'))
        or parsed.query
        or parsed.fragment
        or '?' in url
        or '#' in url
        or not parsed.path.startswith('/')
        or parsed.path.endswith('/')
    ):
        return None
    segments = tuple(parsed.path.split('/')[1:])
    if not segments or any(
        not seg or not _AUTO_DISCOVERY_NAME.fullmatch(seg) or seg in ('.', '..') or seg.lower().endswith('.git')
        for seg in segments
    ):
        return None
    kind = source_type if source_type is not None else (
        'github' if host == 'github.com' else 'gitlab' if host == 'gitlab.com' else 'forgejo'
    )
    if (
        (host == 'github.com' and kind != 'github')
        or (kind == 'github' and host != 'github.com')
        or (host == 'gitlab.com' and kind != 'gitlab')
    ):
        return None
    if kind != 'gitlab' and len(segments) != 1:
        return None
    if host == 'github.com':
        return 'github', 'https://github.com', segments[0]
    return kind, 'https://' + host, '/'.join(segments)


def _parse_repo_identity(url, source_type=None):
    if not isinstance(url, str) or not url or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname
    parts = parsed.path.split('/')
    kind = source_type or ('github' if host == 'github.com' else 'gitlab' if host == 'gitlab.com' else 'forgejo')
    if (
        parsed.scheme != 'https'
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and (host == 'github.com' or port <= 0))
        or parsed.query
        or parsed.fragment
        or '?' in url
        or '#' in url
        or kind not in ('github', 'forgejo', 'gitlab')
        or (host == 'github.com' and kind != 'github')
        or (kind == 'github' and host != 'github.com')
        or (host == 'gitlab.com' and kind != 'gitlab')
        or (len(parts) < 3 if kind == 'gitlab' else len(parts) != 3)
        or parts[0] != ''
    ):
        return None
    if any(not _AUTO_DISCOVERY_NAME.fullmatch(part) or part in ('.', '..') for part in parts[1:]):
        return None
    owner, name = '/'.join(parts[1:-1]), parts[-1]
    if host == 'github.com':
        return 'github', 'https://github.com', owner, name
    authority = parsed.netloc if port is not None else host
    return kind, 'https://' + authority, owner, name


def _valid_auto_discoveries(raw_rules):
    if not isinstance(raw_rules, list):
        return None
    normalized = []
    seen_owners = set()
    seen_ids = set()
    for rule in raw_rules:
        if not isinstance(rule, dict) or 'url' not in rule or set(rule) - _AUTO_DISCOVERY_KEYS:
            return None
        source_type = None
        if 'type' in rule:
            if not isinstance(rule['type'], str) or rule['type'] not in ('github', 'gitlab', 'forgejo'):
                return None
            source_type = rule['type']
        parsed = _parse_owner_url(rule['url'], source_type=source_type)
        if parsed is None:
            return None
        kind, base, owner = parsed
        canonical_url = f'{base}/{owner}'
        if 'id' in rule:
            if type(rule['id']) is not int or rule['id'] <= 0:
                return None
        elif kind == 'gitlab':
            return None
        visibility = 'private'
        if 'visibility' in rule:
            if not isinstance(rule['visibility'], str) or rule['visibility'] not in ('private', 'public'):
                return None
            visibility = rule['visibility']
        ignore_releases = False
        if 'ignore_releases' in rule:
            if not isinstance(rule['ignore_releases'], bool):
                return None
            ignore_releases = rule['ignore_releases']
        if 'destination_name_prefix' in rule:
            prefix = rule['destination_name_prefix']
            if (
                not isinstance(prefix, str)
                or not prefix
                or not _AUTO_DISCOVERY_NAME.fullmatch(prefix)
                or prefix in ('.', '..')
                or _AUTO_DISCOVERY_GENERATED.fullmatch(prefix)
            ):
                return None
        else:
            prefix = 'mirror-' + owner.replace('/', '-')
        exclude_lower = set()
        if 'exclude' in rule:
            if not isinstance(rule['exclude'], list):
                return None
            for item in rule['exclude']:
                if not isinstance(item, str) or not _AUTO_DISCOVERY_NAME.fullmatch(item) or item in ('.', '..'):
                    return None
                if item.lower() in exclude_lower:
                    return None
                exclude_lower.add(item.lower())
        overrides = {}
        override_keys_lower = set()
        if 'overrides' in rule:
            if not isinstance(rule['overrides'], dict):
                return None
            for repo_name, ov in rule['overrides'].items():
                if not isinstance(repo_name, str) or not _AUTO_DISCOVERY_NAME.fullmatch(repo_name) or repo_name in ('.', '..'):
                    return None
                if repo_name.lower() in override_keys_lower or repo_name.lower() in exclude_lower:
                    return None
                override_keys_lower.add(repo_name.lower())
                if not isinstance(ov, dict) or not ov or set(ov) - _AUTO_DISCOVERY_OVERRIDE_KEYS:
                    return None
                if 'name' in ov:
                    ov_name = ov['name']
                    if (
                        not isinstance(ov_name, str)
                        or not (1 <= len(ov_name) <= 100)
                        or not _AUTO_DISCOVERY_NAME.fullmatch(ov_name)
                        or ov_name in ('.', '..')
                        or _AUTO_DISCOVERY_GENERATED.fullmatch(ov_name)
                    ):
                        return None
                if 'visibility' in ov:
                    if not isinstance(ov['visibility'], str) or ov['visibility'] not in ('private', 'public'):
                        return None
                if 'ignore_releases' in ov:
                    if not isinstance(ov['ignore_releases'], bool):
                        return None
                planned = ov['name'] if 'name' in ov else f'{prefix}-{repo_name}'
                if len(planned) > 100 or not _AUTO_DISCOVERY_NAME.fullmatch(planned) or planned in ('.', '..') or _AUTO_DISCOVERY_GENERATED.fullmatch(planned):
                    return None
                overrides[repo_name] = dict(ov)
        owner_key = (base.lower(), owner.lower())
        if owner_key in seen_owners:
            return None
        seen_owners.add(owner_key)
        if 'id' in rule:
            id_key = (base.lower(), rule['id'])
            if id_key in seen_ids:
                return None
            seen_ids.add(id_key)
        entry = {
            'url': canonical_url,
            'type': kind,
            'base_url': base,
            'owner': owner,
            'visibility': visibility,
            'ignore_releases': ignore_releases,
            'destination_name_prefix': prefix,
            'has_explicit_prefix': 'destination_name_prefix' in rule,
            'overrides': overrides,
        }
        if 'id' in rule:
            entry['id'] = rule['id']
        normalized.append(entry)
    return normalized, {r['url']: r for r in normalized}


def valid_manifest(sources):
    """Validate the entire snapshot before running any downloaded scheduler."""
    named = isinstance(sources, dict)
    rules = []
    rules_by_url = {}
    if named:
        from_groups = 'groups' in sources
        group_ids = set()
        group_owners = set()
        if from_groups and set(sources) - {'host_concurrency_limits', 'auto_discoveries'} == {'destinations', 'repositories', 'groups'}:
            groups = sources['groups']
            if not isinstance(groups, list) or not groups:
                return False
            seen_groups = set()
            seen_group_owners = set()
            for g in groups:
                if not isinstance(g, dict) or set(g) != {'type', 'base_url', 'owner', 'id', 'repos'}:
                    return False
                base, owner = g['base_url'], g['owner']
                if not isinstance(base, str) or not isinstance(owner, str):
                    return False
                try:
                    u = urlsplit(base)
                    if (g['type'] != 'gitlab' or g['repos'] != 'all' or type(g['id']) is not int or g['id'] <= 0
                            or u.scheme != 'https' or not u.hostname or u.username is not None or u.password is not None
                            or u.path or u.query or u.fragment or u.port == 0
                            or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]+)?', u.netloc)
                            or any(not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', x) or x in ('.', '..') for x in owner.split('/'))):
                        return False
                except ValueError:
                    return False
                okey = _origin_key(base)
                key = (okey, g['id'])
                owner_key = (okey, owner.lower())
                if key in seen_groups or owner_key in seen_group_owners:
                    return False
                seen_groups.add(key)
                seen_group_owners.add(owner_key)
                group_ids.add(key)
                group_owners.add(owner_key)
            # Reuse the existing strict named-repository checks, permitting a
            # group-only declaration via a harmless validation sentinel.
            sources = dict(sources)
            del sources['groups']
            if 'auto_discoveries' not in sources and sources['repositories'] == []:
                profile = next(iter(sources['destinations']), None) if isinstance(sources['destinations'], dict) else None
                sources['repositories'] = [{'source': {'url': 'https://github.com/check/check'}, 'mirrors': {profile: {}}}]
        if set(sources) - {'host_concurrency_limits', 'auto_discoveries'} != {'destinations', 'repositories'}:
            return False
        if 'auto_discoveries' in sources:
            validated_rules = _valid_auto_discoveries(sources['auto_discoveries'])
            if validated_rules is None:
                return False
            rules, rules_by_url = validated_rules
            if from_groups:
                for rule in rules:
                    r_origin = _origin_key(rule['base_url'])
                    if ('id' in rule and (r_origin, rule['id']) in group_ids) or (r_origin, rule['owner'].lower()) in group_owners:
                        return False
        profiles, repos = sources['destinations'], sources['repositories']
        if not isinstance(profiles, dict) or len(profiles) != 1 or not isinstance(repos, list):
            return False
        profile, settings = next(iter(profiles.items()))
        if not isinstance(profile, str) or not profile or not isinstance(settings, dict):
            return False
        if 'type' in settings:
            if not valid_profile(settings):
                return False
        elif settings not in ({}, {'account_type': 'personal'}, {'account_type': 'organization'}):
            return False
        converted = []
        rules_with_generated = set()
        for repo in repos:
            if not isinstance(repo, dict) or not {'source', 'mirrors'} <= set(repo) or set(repo) - {'source', 'mirrors', 'discovered_by'}:
                return False
            source, mirrors = repo['source'], repo['mirrors']
            if not isinstance(source, dict) or 'url' not in source or set(source) - {'url', 'id', 'type'} or not isinstance(mirrors, dict) or set(mirrors) != {profile}:
                return False
            if source.get('type') not in (None, 'github', 'forgejo', 'gitlab'):
                return False
            mirror = mirrors[profile]
            if not isinstance(mirror, dict) or set(mirror) - {'name', 'id', 'legacy', 'visibility', 'ignore_releases'} or not isinstance(source['url'], str):
                return False
            if mirror.get('visibility', 'private') not in ('private', 'public') or not isinstance(mirror.get('ignore_releases', False), bool):
                return False
            dest_name = mirror.get('name', source['url'].rsplit('/', 1)[-1])
            parsed_repo = _parse_repo_identity(source['url'], source.get('type'))
            if parsed_repo is None:
                return False
            kind, base, owner, name = parsed_repo
            if 'discovered_by' in repo:
                discovered_by = repo['discovered_by']
                if not isinstance(discovered_by, str) or discovered_by not in rules_by_url:
                    return False
                rule = rules_by_url[discovered_by]
                if kind != rule['type'] or base.lower() != rule['base_url'].lower() or owner.lower() != rule['owner'].lower():
                    return False
                ov = next((v for k, v in rule['overrides'].items() if k.lower() == name.lower()), None)
                planned_name = ov['name'] if ov and 'name' in ov else f"{rule['destination_name_prefix']}-{name}"
                if len(planned_name) > 100 or not _AUTO_DISCOVERY_NAME.fullmatch(planned_name) or planned_name in ('.', '..'):
                    return False
                if 'name' in mirror:
                    if mirror['name'] != planned_name and not (rule['type'] == 'gitlab' and _AUTO_DISCOVERY_GENERATED.fullmatch(mirror['name'])):
                        return False
                else:
                    if _AUTO_DISCOVERY_GENERATED.fullmatch(planned_name):
                        return False
                    dest_name = planned_name
            if 'name' in mirror and isinstance(mirror['name'], str) and _AUTO_DISCOVERY_GENERATED.fullmatch(mirror['name']):
                if 'discovered_by' in repo:
                    rules_with_generated.add(repo['discovered_by'])
                if kind == 'gitlab':
                    repo_origin = _origin_key(base)
                    for rule in rules:
                        if (
                            rule['type'] == 'gitlab'
                            and _origin_key(rule['base_url']) == repo_origin
                            and rule['owner'].lower() == owner.lower()
                        ):
                            rules_with_generated.add(rule['url'])
            item = {
                'url': source['url'],
                'destination_name': dest_name,
                '_identity': (base.lower(), owner.lower(), name.lower()),
            }
            if 'type' in source: item['source_type'] = source['type']
            if 'id' in source: item['source_id'] = source['id']
            if 'id' in mirror: item['destination_id'] = mirror['id']
            if 'legacy' in mirror:
                legacy = mirror['legacy']
                if not isinstance(legacy, dict) or set(legacy) != {'url', 'name', 'signature'}:
                    return False
                item.update(legacy_url=legacy['url'], legacy_destination_name=legacy['name'], signature=legacy['signature'])
            converted.append(item)
        sources = converted
    if not isinstance(sources, list) or (not named and not sources):
        return False
    identities, destinations = set(), set()
    seen_identities_by_dest = {}
    for item in sources:
        item_identity = item.pop('_identity', None) if isinstance(item, dict) else None
        if isinstance(item, str):
            url, old = item, item
            names = {url.rsplit('/', 1)[-1].lower()}
        elif named and isinstance(item, dict) and set(item) <= {'url', 'destination_name', 'source_id', 'destination_id', 'source_type'} and {'url', 'destination_name'} <= set(item):
            url = old = item['url']
            name = item['destination_name']
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', name) or name in ('.', '..') or len(name) > 100:
                return False
            if any(type(item[k]) is not int or item[k] <= 0 for k in ('source_id', 'destination_id') if k in item):
                return False
            names = {name.lower()}
        elif isinstance(item, dict) and set(item) == {'url', 'source_id', 'destination_id', 'destination_name',
                                                     'legacy_url', 'legacy_destination_name', 'signature'}:
            if not isinstance(item['signature'], str) or not re.fullmatch(r'[0-9a-f]{64}', item['signature']):
                return False
            url, old = item['url'], item['legacy_url']
            if (not isinstance(url, str) or not isinstance(old, str)
                    or any(type(item[k]) is not int or item[k] <= 0 for k in ('source_id', 'destination_id'))):
                return False
            for key in ('destination_name', 'legacy_destination_name'):
                value = item[key]
                if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', value) or value in ('.', '..') or len(value) > 100:
                    return False
            if url.rsplit('/', 2)[0].lower() != old.rsplit('/', 2)[0].lower():
                return False
            names = {item['destination_name'].lower(), item['legacy_destination_name'].lower()}
        else:
            return False
        source_type = item.get('source_type') if isinstance(item, dict) else None
        if not valid_source(url, source_type) or not valid_source(old, source_type):
            return False
        source_names = {url.lower(), old.lower()}
        if source_names & identities or names & destinations:
            return False
        identities.update(source_names)
        destinations.update(names)
        if item_identity is not None:
            for dest_key in names:
                seen_identities_by_dest[dest_key] = item_identity
    for rule in rules:
        if rule.get('has_explicit_prefix') and rule['url'] in rules_with_generated:
            return False
        for repo_key, ov in rule['overrides'].items():
            ov_identity = (rule['base_url'].lower(), rule['owner'].lower(), repo_key.lower())
            planned = ov['name'] if 'name' in ov else f"{rule['destination_name_prefix']}-{repo_key}"
            dest_key = planned.lower()
            existing_identity = seen_identities_by_dest.get(dest_key)
            if existing_identity is not None and existing_identity != ov_identity:
                return False
            seen_identities_by_dest[dest_key] = ov_identity
    return True


def local_json(path, credential):
    port, token = credential
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=20)
    try:
        connection.request('GET', path, headers={'Authorization': 'token ' + token})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('local configuration read refused')
        raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError('local configuration response too large')
        return json.loads(raw, object_pairs_hook=unique_pairs)
    finally:
        connection.close()


def local_manifest(repo, expected_id, port, token):
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*', repo):
        raise ValueError('invalid local configuration repository')
    path = '/api/v1/repos/' + repo
    def verify():
        data = local_json(path, (port, token))
        if (not isinstance(data, dict) or type(data.get('id')) is not int or data['id'] != expected_id
                or data.get('full_name') != repo or data.get('private') is not True
                or data.get('has_actions') is not False or data.get('default_branch') != 'main'):
            raise ValueError('local configuration repository identity or policy mismatch')
    verify()
    file = local_json(path + '/contents/config.json?ref=main', (port, token))
    if (not isinstance(file, dict) or file.get('type') != 'file' or file.get('encoding') != 'base64'
            or not isinstance(file.get('content'), str)):
        raise ValueError('invalid local configuration blob')
    raw = base64.b64decode(file['content'].replace('\n', ''), validate=True)
    if hashlib.sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest() != file.get('sha'):
        raise ValueError('local configuration blob mismatch')
    verify()
    return raw


def local_token_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 4096:
            raise ValueError('invalid local configuration token file')
        token = stream.read(4097).strip().decode('ascii')
        if not token or any(c.isspace() or ord(c) < 32 for c in token):
            raise ValueError('invalid local configuration token')
        return token


def checked_process(command, *, cwd, env, lock_fd, quiet=True, scheduler=False):
    """Forward termination, allowing the scheduler time to clean up its workers."""
    pending = []
    def forward(signum, _frame):
        pending.append(signum)
    previous = {s: signal.signal(s, forward) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        child = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL if quiet else None,
                                 stderr=subprocess.DEVNULL if quiet else None,
                                 pass_fds=(lock_fd,), start_new_session=True)
        deadline = None
        while True:
            if pending and deadline is None:
                signum = pending[0]
                deadline = time.monotonic() + 5
                try:
                    if scheduler:
                        # Scheduler owns separately-sessioned workers and must reap them.
                        child.send_signal(signal.SIGTERM)
                    else:
                        os.killpg(child.pid, signum)
                except ProcessLookupError:
                    pass
            status = child.poll()
            if status is not None:
                if deadline is not None and not scheduler:
                    # The helper may have exited while its fetch still runs.
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                break
            if deadline is not None and not scheduler and time.monotonic() >= deadline:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                status = child.wait()
                break
            time.sleep(.05)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if pending and status == 0:
        return 128 + pending[0]
    return status if status >= 0 else 128 - status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--remote', required=True, help='trusted repository URL or local path')
    parser.add_argument('--state-dir', required=True, help='private directory for temporary snapshots')
    parser.add_argument('--job-lock', required=True, help='distinct from scheduler --lock')
    parser.add_argument('--helper', default=str(DEFAULT_HELPER))
    parser.add_argument('--helper-python', default=sys.executable, help='Python with PyJWT installed for Manobot')
    parser.add_argument('--installation', default='178997310')
    parser.add_argument('scheduler_args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    extra = args.scheduler_args
    if extra and extra[0] == '--':
        extra = extra[1:]
    stage = 'configuration'
    try:
        if not re.fullmatch(r'[0-9]+', args.installation):
            raise ValueError('installation')
        if args.remote.startswith('-') or '\n' in args.remote or '\r' in args.remote:
            raise ValueError('remote')
        if '://' in args.remote:
            url = urlsplit(args.remote)
            if url.scheme != 'https' or not url.hostname or url.username or url.password:
                raise ValueError('remote')
        elif ':' in args.remote:
            raise ValueError('remote')
        # Validate before invoking the fetched scheduler: its argparse may accept
        # abbreviated options that override the fixed manifest or scheduler lock.
        valued = {'--token-file', '--timestamp-secret-file', '--journal-dir', '--owner',
                  '--port', '--jobs', '--repository-timeout', '--lock', '--helper',
                  '--helper-python', '--sync-python', '--installation', '--app-id',
                  '--private-key-file', '--config-repo', '--config-account-type', '--asset-max-bytes', '--gitlab-token-file',
                  '--forgejo-config-repo', '--forgejo-config-id'}
        seen = {}
        i = 0
        while i < len(extra):
            name, separator, value = extra[i].partition('=')
            if name in seen or name not in valued | {'--organization'}:
                raise ValueError('invalid scheduler option')
            if name == '--organization':
                if separator:
                    raise ValueError('invalid scheduler flag')
                seen[name] = True
            else:
                if not separator:
                    i += 1
                    if i >= len(extra):
                        raise ValueError('missing scheduler option value')
                    value = extra[i]
                if not value or value.startswith('-'):
                    raise ValueError('invalid scheduler option value')
                seen[name] = value
            i += 1
        config_repository = None
        local_repo = seen.get('--forgejo-config-repo')
        local_id = seen.get('--forgejo-config-id')
        if (bool(local_repo) != bool(local_id) or (local_repo and ('--config-repo' in seen
                or not re.fullmatch(r'[1-9][0-9]{0,17}', local_id) or '--token-file' not in seen
                or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*', local_repo)))):
            raise ValueError('invalid local configuration repository options')
        if '--config-repo' in seen:
            # The manifest may live in a separate private repository. The code
            # remote is still fetched here; the configuration repository is
            # fetched separately below so publication and the fetched snapshot
            # share one authority.
            authority = seen['--config-repo']
            parts = authority.split('/')
            if (len(parts) != 2 or not all(re.fullmatch(r'[A-Za-z0-9_.-]+', part) and part not in ('.', '..')
                                          for part in parts)):
                raise ValueError('invalid configuration repository')
            config_repository = authority
            match = re.fullmatch(r'https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?', args.remote, re.IGNORECASE)
            if match and authority.lower() == (match.group(1) + '/' + match.group(2)).lower():
                # The code snapshot already carries the manifest.
                config_repository = None
        locks = [seen['--lock']] if '--lock' in seen else []
        if len(locks) != 1 or os.path.realpath(locks[0]) == os.path.realpath(args.job_lock):
            raise ValueError('distinct scheduler lock required')
        stage = 'state and lock'
        state = Path(args.state_dir)
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = state.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('state directory')
        fd = os.open(args.job_lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, 'r+') as lock:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('job lock')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('Updater skipped: lock held', file=sys.stderr)
                return 75
            if os.path.exists(locks[0]) and os.path.samestat(info, os.stat(locks[0])):
                raise ValueError('distinct scheduler lock required')
            scheduler_env = os.environ.copy()
            scheduler_env.pop('PYTHONPATH', None)
            scheduler_env.pop('PYTHONHOME', None)
            env = {key: value for key, value in scheduler_env.items() if not key.startswith('GIT_')}
            env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
            with tempfile.TemporaryDirectory(prefix='forgejo-update-', dir=state) as tmp:
                root = Path(tmp)
                def run(command, quiet=True):
                    return checked_process(command, cwd=root, env=env, lock_fd=fd, quiet=quiet)
                stage = 'git initialization'
                if run(['git', 'init', '--bare', str(root / 'objects')]):
                    raise ValueError('git init')
                fetch = [args.helper_python, args.helper, '--installation', args.installation, 'run', '--',
                         'git', '--git-dir', str(root / 'objects'), 'fetch', '--no-tags', '--no-recurse-submodules', '--', args.remote, 'refs/heads/main']
                stage = 'fetch'
                if run(fetch):
                    raise ValueError('fetch failed')
                # Resolve only FETCH_HEAD from this successful, fresh fetch, never a cached ref.
                stage = 'commit resolution'
                commit = subprocess.check_output(['git', '--git-dir', str(root / 'objects'), 'rev-parse', '--verify', 'FETCH_HEAD^{commit}'], cwd=root, env=env, stderr=subprocess.DEVNULL, text=True).strip()
                if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', commit):
                    raise ValueError('invalid commit')
                stage = 'snapshot archive'
                archive = root / 'snapshot.tar'
                with archive.open('wb') as output:
                    status = subprocess.run(['git', '--git-dir', str(root / 'objects'), 'archive', '--format=tar', commit], cwd=root, env=env, stdout=output, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, pass_fds=(fd,)).returncode
                if status:
                    raise ValueError('archive')
                def extract_snapshot(archive_file, destination):
                    destination.mkdir(mode=0o700)
                    with tarfile.open(archive_file) as tar:
                        for member in tar:
                            path = PurePosixPath(member.name)
                            if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                                raise ValueError('unsafe snapshot')
                            tar.extract(member, destination, filter='data')

                def fetch_commit(git_dir, url, label):
                    fetch = [args.helper_python, args.helper, '--installation', args.installation, 'run', '--',
                             'git', '--git-dir', str(git_dir), 'fetch', '--no-tags', '--no-recurse-submodules', '--',
                             url, 'refs/heads/main']
                    if run(fetch):
                        raise ValueError(f'{label} fetch failed')
                    resolved = subprocess.check_output(
                        ['git', '--git-dir', str(git_dir), 'rev-parse', '--verify', 'FETCH_HEAD^{commit}'],
                        cwd=root, env=env, stderr=subprocess.DEVNULL, text=True).strip()
                    if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', resolved):
                        raise ValueError(f'invalid {label} commit')
                    return resolved

                def write_archive(git_dir, resolved, archive_file, label):
                    with archive_file.open('wb') as output:
                        status = subprocess.run(
                            ['git', '--git-dir', str(git_dir), 'archive', '--format=tar', resolved],
                            cwd=root, env=env, stdout=output, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, pass_fds=(fd,)).returncode
                    if status:
                        raise ValueError(f'{label} archive')

                stage = 'snapshot extraction'
                snapshot = root / 'snapshot'
                extract_snapshot(archive, snapshot)
                manifest = snapshot / 'config-forgejo.json'
                if local_repo is not None:
                    stage = 'local configuration fetch'
                    port = int(seen.get('--port', '3300'))
                    if not 1 <= port <= 65535:
                        raise ValueError('invalid local Forgejo port')
                    manifest = root / 'config.json'
                    manifest.write_bytes(local_manifest(local_repo, int(local_id), port, local_token_file(seen['--token-file'])))
                elif config_repository is not None:
                    stage = 'configuration git initialization'
                    config_objects = root / 'config-objects'
                    if run(['git', 'init', '--bare', str(config_objects)]):
                        raise ValueError('configuration git init')
                    stage = 'configuration fetch'
                    config_commit = fetch_commit(config_objects, 'https://github.com/' + config_repository,
                                                 'configuration')
                    stage = 'configuration archive'
                    config_archive = root / 'config-snapshot.tar'
                    write_archive(config_objects, config_commit, config_archive, 'configuration')
                    stage = 'configuration extraction'
                    config_snapshot = root / 'config-snapshot'
                    extract_snapshot(config_archive, config_snapshot)
                    manifest = config_snapshot / 'config-forgejo.json'
                stage = 'manifest validation'
                sources = json.loads(manifest.read_text(encoding='utf-8'), object_pairs_hook=unique_pairs)
                if isinstance(sources, dict) and ({'sources', 'destination_account_type'} & sources.keys()):
                    print('legacy configuration format is no longer supported', file=sys.stderr)
                    return 2
                if not valid_manifest(sources):
                    raise ValueError('invalid manifest')
                if isinstance(sources, dict):
                    settings = next(iter(sources['destinations'].values()))
                    if 'type' in settings and (settings['url'] != f"http://127.0.0.1:{seen.get('--port', '3300')}" or settings['owner'].lower() != seen.get('--owner', '').lower() or settings['auth'] != 'forgejo-home' or '--organization' in seen):
                        raise ValueError('destination profile runtime mismatch')
                if not (snapshot / 'repo_mirror' / 'forgejo_schedule.py').is_file():
                    raise ValueError('scheduler missing')
                if 'FORGEJO_MANUAL_INPUTS' in scheduler_env and not (snapshot / 'repo_mirror' / 'forgejo_dispatch_runtime.py').is_file():
                    raise ValueError('manual options require an upgraded scheduler')
                if local_repo is not None and not (snapshot / 'repo_mirror' / 'forgejo_control_config.py').is_file():
                    raise ValueError('local configuration requires an upgraded scheduler')
                stage = 'scheduler launch'
                print(f'Updater fetched commit: {commit}', file=sys.stderr, flush=True)
                return checked_process([sys.executable, '-m', 'repo_mirror.forgejo_schedule', 'run', '--manifest', str(manifest), *extra], cwd=snapshot, env=scheduler_env, lock_fd=fd, quiet=False, scheduler=True)
    except (OSError, ValueError, http.client.HTTPException, subprocess.SubprocessError, tarfile.TarError):
        print(f'Updater failed: {stage}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
