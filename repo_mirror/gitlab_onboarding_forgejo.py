"""Fail-closed onboarding of public direct GitLab group projects.

Groups are declarations, not worker entries.  Concrete entries are published to
GitHub before any worker receives them; created but unpublished repos require
manual reconciliation and are never automatically adopted.
"""
import hashlib
import json
import os
import re
from urllib.parse import quote, urlsplit

from repo_mirror.config_json import unique_pairs
from repo_mirror.constants import NAME
from repo_mirror.forgejo_git import APIError, ForgejoDestination
from repo_mirror.gitlab_source_adapter import GitLabSourceAdapter
from repo_mirror.logging_utils import safe_label
from repo_mirror.sources import source_api_base

GENERATED_NAME_RE = re.compile(r'gitlab-[0-9a-f]{24}', re.IGNORECASE)
DUPLICATE_GROUP_MESSAGE = 'duplicate group declaration in groups and auto_discoveries'
PREFIX_WITH_GENERATED_MESSAGE = 'destination_name_prefix is not allowed on a rule with existing generated gitlab destinations'


def group_origin_key(base_url):
    """Return normalized ``(host, port)`` for comparing group and rule origins."""
    parsed = urlsplit(base_url)
    return (parsed.hostname.lower(), parsed.port or 443)


def discover_gitlab_projects(source, api):
    """Capture validated public direct-project IDs from a group listing."""
    from repo_mirror.repository import validate_source_clone_url

    url = f"{source_api_base(source)}/groups/{quote(source['owner'], safe='')}/projects?include_subgroups=false&with_shared=false"
    projects = {}
    listing = api.pages(url)
    if os.environ.get('MIRROR_GITLAB_TOKEN'):
        from repo_mirror.gitlab_source_adapter import fetch_group_projects
        listing = fetch_group_projects(source['base_url'], source['owner'],
                                       os.environ.get('MIRROR_GITLAB_TOKEN'))
    for project in listing:
        if not isinstance(project, dict):
            raise ValueError('invalid GitLab project discovery')
        if project.get('visibility') in ('private', 'internal'):
            continue
        name, identity = project.get('path'), project.get('id')
        if (not isinstance(name, str) or not NAME.fullmatch(name) or name in ('.', '..')
                or type(identity) is not int or identity <= 0 or project.get('visibility') != 'public'
                or project.get('path_with_namespace') != source['owner'] + '/' + name
                or name.lower() in {key.lower() for key in projects} or identity in projects.values()):
            raise ValueError('invalid GitLab direct project identity')
        validate_source_clone_url(source, name, {'clone_url': project.get('http_url_to_repo')})
        projects[name] = identity
    return projects


def gitlab_generated_name(origin, group_id, source_id):
    """Compute the legacy ``gitlab-<24hex>`` destination name for a group project."""
    digest = hashlib.sha256(
        (origin.rstrip('/').lower() + '\0' + str(group_id) + '\0' + str(source_id)).encode()
    ).hexdigest()[:24]
    return 'gitlab-' + digest


def group_deprecation_warning(group):
    """Return one fixed, log-safe deprecation warning line for a ``groups`` entry."""
    return (
        f"DEPRECATION: groups entry ({safe_label(group['owner'])}) is deprecated and translated "
        f"to auto_discoveries in memory; migrate to auto_discoveries before groups is removed "
        f"in the next release"
    )


def translate_groups(config, *, normalized_auto_discoveries=None, stream=None):
    """Translate ``groups[]`` into in-memory ``auto_discoveries`` rules without mutating ``config``.

    Rejects duplicate group declarations within ``groups`` or across ``groups`` and
    ``auto_discoveries``. When ``stream`` is provided, emits one warning line per
    translated group.
    """
    if not isinstance(config, dict) or 'groups' not in config:
        return []
    groups = config['groups']
    validate_groups(groups)
    seen_group_owners = set()
    seen_group_ids = set()
    for group in groups:
        okey = group_origin_key(group['base_url'])
        owner_key = (okey, group['owner'].lower())
        id_key = (okey, group['id'])
        if owner_key in seen_group_owners or id_key in seen_group_ids:
            raise ValueError('duplicate group declaration')
        seen_group_owners.add(owner_key)
        seen_group_ids.add(id_key)
    if normalized_auto_discoveries is None:
        if 'auto_discoveries' in config:
            from repo_mirror.config_named import _normalize_auto_discoveries
            auto_rules = _normalize_auto_discoveries(config['auto_discoveries'])
        else:
            auto_rules = []
    else:
        auto_rules = normalized_auto_discoveries
    auto_ids = {(group_origin_key(r['base_url']), r['id']) for r in auto_rules if 'id' in r}
    auto_owners = {(group_origin_key(r['base_url']), r['owner'].lower()) for r in auto_rules}
    translated = []
    for group in groups:
        okey = group_origin_key(group['base_url'])
        if (okey, group['id']) in auto_ids or (okey, group['owner'].lower()) in auto_owners:
            raise ValueError(DUPLICATE_GROUP_MESSAGE)
        translated.append({
            'url': f"{group['base_url']}/{group['owner']}",
            'type': 'gitlab',
            'base_url': group['base_url'],
            'owner': group['owner'],
            'id': group['id'],
            'visibility': 'private',
            'ignore_releases': False,
            'destination_name_prefix': 'mirror-' + group['owner'].replace('/', '-'),
            'exclude': [],
            'overrides': {},
            '_from_groups': True,
        })
    if stream is not None:
        for group in groups:
            print(group_deprecation_warning(group), file=stream, flush=True)
    return translated


class AnonymousGroupAPI:
    """Anonymous origin-bound requests with bounded GitLab pagination."""
    def __init__(self, origins):
        self.clients = {origin: GitLabSourceAdapter(origin, 'placeholder/project', 1) for origin in origins}

    def _client(self, url):
        matches = [client for origin, client in self.clients.items() if url.startswith(origin + '/api/v4/')]
        if len(matches) != 1:
            raise ValueError('untrusted GitLab API origin')
        return matches[0]

    def request(self, method, url):
        if method != 'GET':
            raise ValueError('read-only GitLab API')
        return self._client(url)._get(url)[0]

    def pages(self, url):
        client = self._client(url)
        result = []
        page = 1
        while page <= 10000:
            data, headers = client._get(url + '&per_page=100&page=' + str(page))
            if not isinstance(data, list):
                raise ValueError('invalid group listing')
            result.extend(data)
            following = headers.get('X-Next-Page', '')
            if not following:
                # GitLab may omit pagination headers; request an empty page to
                # avoid silently truncating a full page.
                if len(data) < 100:
                    return result
                page += 1
                continue
            if not following.isascii() or not following.isdigit() or int(following) <= page:
                raise ValueError('invalid group pagination')
            page = int(following)
        raise ValueError('group pagination limit exceeded')


class PartialOnboarding(ValueError):
    def __init__(self, names):
        self.names = tuple(names)
        super().__init__('destination creation may be partial; reconcile manually: ' + ', '.join(names))


def validate_groups(groups):
    if not isinstance(groups, list) or not groups:
        raise ValueError('invalid groups')
    seen = set()
    for group in groups:
        if not isinstance(group, dict) or set(group) != {'type', 'base_url', 'owner', 'id', 'repos'}:
            raise ValueError('invalid group declaration')
        base, owner = group['base_url'], group['owner']
        if not isinstance(base, str) or not isinstance(owner, str):
            raise ValueError('invalid group declaration')
        p = urlsplit(base)
        if (group['type'] != 'gitlab' or group['repos'] != 'all' or type(group['id']) is not int or group['id'] <= 0
                or p.scheme != 'https' or not p.hostname or p.username is not None or p.password is not None
                or p.path or p.query or p.fragment or p.port == 0
                or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]+)?', p.netloc)
                or any(not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', x) or x in ('.', '..') for x in owner.split('/'))):
            raise ValueError('invalid group declaration')
        key = (base.lower(), group['id'])
        if key in seen:
            raise ValueError('duplicate group declaration')
        seen.add(key)


def onboard(document, source_api, destination_api, cas, owner, *, organization=False, project_factory=GitLabSourceAdapter, active_bindings=None):
    config = json.loads(document.decode('utf-8'), object_pairs_hook=unique_pairs)
    from repo_mirror.host_limits import CONFIG_KEY
    if not isinstance(config, dict) or set(config) - {CONFIG_KEY, 'auto_discoveries'} != {'destinations', 'repositories', 'groups'}:
        raise ValueError('invalid group manifest')
    validate_groups(config['groups'])
    translate_groups(config)
    if not isinstance(config['destinations'], dict) or len(config['destinations']) != 1 or not isinstance(config['repositories'], list):
        raise ValueError('invalid group manifest')
    profile = next(iter(config['destinations']))
    # Fail before destination writes when the snapshot is not authoritative.
    if cas.read() != document:
        raise ValueError('configuration changed')
    existing = config['repositories']
    planned = []
    for group in config['groups']:
        origin, path = group['base_url'], group['owner']
        metadata = source_api.request('GET', origin + '/api/v4/groups/' + quote(path, safe=''))
        if (not isinstance(metadata, dict) or type(metadata.get('id')) is not int or metadata['id'] != group['id']
                or metadata.get('visibility') != 'public' or metadata.get('full_path') != path):
            raise ValueError('group identity or visibility mismatch')
        for name, source_id in sorted(discover_gitlab_projects(group, source_api).items()):
            url = origin + '/' + path + '/' + name
            project_factory(origin, path + '/' + name, source_id).project()
            digest = hashlib.sha256((origin.lower() + '\0' + str(group['id']) + '\0' + str(source_id)).encode()).hexdigest()[:24]
            dest_name = 'gitlab-' + digest
            if active_bindings is not None:
                active_bindings.add((url, source_id, dest_name))
            matches = [r for r in existing if isinstance(r, dict) and isinstance(r.get('source'), dict)
                       and (r['source'].get('url', '').lower() == url.lower() or
                            (r['source'].get('type') == 'gitlab' and r['source'].get('id') == source_id and
                             str(r['source'].get('url', '')).lower().startswith(origin.lower() + '/')))]
            if matches:
                expected_source = {'type': 'gitlab', 'url': url, 'id': source_id}
                mirror = matches[0].get('mirrors', {}).get(profile, {}) if len(matches) == 1 else {}
                if matches[0].get('source') != expected_source or mirror.get('name') != dest_name or type(mirror.get('id')) is not int or mirror['id'] <= 0:
                    raise ValueError('existing source mapping conflicts')
                continue
            planned.append((url, source_id, dest_name))
    # Validate combined manifest before creating anything, including collisions.
    from repo_mirror.forgejo_sync import parse_manifest
    projected = {'destinations': config['destinations'], 'repositories': existing + [
        {'source': {'type': 'gitlab', 'url': u, 'id': i}, 'mirrors': {profile: {'name': n, 'id': 1}}}
        for u, i, n in planned]}
    if 'auto_discoveries' in config:
        projected['auto_discoveries'] = config['auto_discoveries']
    if projected['repositories'] or 'auto_discoveries' in projected:
        parse_manifest(projected)
    for _, _, name in planned:
        try:
            destination_api.request('GET', f'/api/v1/repos/{owner}/{name}')
        except APIError as exc:
            if exc.status != 404:
                raise
        else:
            raise ValueError('unpinned existing destination; reconcile manually')
    created = []
    try:
        for url, source_id, name in planned:
            # Include attempted names: POST can succeed even if response is lost.
            created.append(name)
            dest = ForgejoDestination(destination_api, owner, organization=organization)
            from repo_mirror.forgejo_git import _source_marker
            marker = _source_marker(url)
            if not organization:
                identity = destination_api.request('GET', '/api/v1/user')
                if not isinstance(identity, dict) or str(identity.get('login', '')).lower() != owner.lower():
                    raise ValueError('destination account mismatch')
            endpoint = f'/api/v1/orgs/{owner}/repos' if organization else '/api/v1/user/repos'
            repo = destination_api.request('POST', endpoint, {'name': name, 'private': True, 'auto_init': False,
                                          'description': marker + ' [repo-mirror:initializing:v1]', 'has_actions': False})
            dest._verify(repo, name, marker, actions=True)
            readback = destination_api.request('GET', f'/api/v1/repos/{owner}/{name}')
            dest._verify(readback, name, marker, actions=True)
            if readback['id'] != repo['id'] or readback['description'] != repo['description']:
                raise ValueError('destination changed during creation')
            existing.append({'source': {'type': 'gitlab', 'url': url, 'id': source_id},
                             'mirrors': {profile: {'name': name, 'id': repo['id']}}})
        if not planned:
            if cas.read() != document:
                raise ValueError('configuration changed')
            return document
        after = (json.dumps(config, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
        cas.replace(document, after)
        if cas.read() != after:
            raise ValueError('configuration readback mismatch')
        return after
    except Exception as exc:
        if created:
            raise PartialOnboarding(created) from exc
        raise
