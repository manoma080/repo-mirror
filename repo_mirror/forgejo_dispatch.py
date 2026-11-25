"""Plan a Forgejo manual dispatch without publishing configuration or running workers."""

import copy
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from repo_mirror.adhoc import parse_repo_url
from repo_mirror.destination_profile import validate_runtime
from repo_mirror.forgejo_sync import parse_manifest, parse_source
from repo_mirror.gitlab_onboarding_forgejo import validate_groups
from repo_mirror.host_limits import CONFIG_KEY, HostLimits

_FIELDS = {'source_urls', 'add_to_config', 'destination_name', 'visibility',
           'remove_from_config', 'ignore_releases'}
_NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}')
_GENERATED = re.compile(r'gitlab-[0-9a-f]{24}', re.IGNORECASE)
_GITHUB_ALIAS = re.compile(r'^https://(?:api\.)?github\.com/', re.IGNORECASE)


class DispatchError(ValueError):
    """A fixed diagnostic safe for a workflow log."""


def _flag(value):
    if value is True or value == 'true':
        return True
    if value is False or value in ('false', ''):
        return False
    raise DispatchError('invalid dispatch flag')


def _options(inputs):
    if not isinstance(inputs, Mapping):
        raise DispatchError('invalid dispatch inputs')
    if inputs.keys() - _FIELDS:
        raise DispatchError('unknown dispatch input')
    urls = inputs.get('source_urls', '')
    destination = inputs.get('destination_name', '')
    add = _flag(inputs.get('add_to_config', False))
    remove = _flag(inputs.get('remove_from_config', False))
    visibility = inputs.get('visibility', 'unchanged')
    releases = inputs.get('ignore_releases', 'keep')
    if not isinstance(urls, str) or not isinstance(destination, str):
        raise DispatchError('invalid dispatch input')
    if visibility not in ('', 'unchanged', 'private', 'public'):
        raise DispatchError('invalid visibility')
    if releases in ('', 'keep'):
        releases = None
    elif releases is True or releases is False or releases in ('true', 'false'):
        releases = _flag(releases)
    else:
        raise DispatchError('invalid ignore-releases setting')
    visibility = visibility if visibility in ('private', 'public') else None
    overrides = bool(destination or visibility or releases is not None)
    parts = [part.strip() for part in urls.split(',')] if urls.strip() else []
    if (remove and (add or overrides)) or (not parts and (add or remove or overrides)):
        raise DispatchError('incompatible dispatch inputs')
    if destination and (len(parts) != 1 or not _NAME.fullmatch(destination)):
        raise DispatchError('custom destination requires one URL and a valid repository name')
    return parts, add, remove, destination, visibility, releases


def _origin(base):
    try:
        parsed = urlsplit(base)
        return parsed.hostname, parsed.port or 443
    except ValueError:
        raise DispatchError('invalid source repository URL') from None


def _route(url, kind=None):
    kind, base, owner, name = parse_repo_url(url, source_type=kind, allow_forgejo_port=True)
    parse_source(url, kind)
    host, port = _origin(base)
    if (not host or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
                            for part in host.split('.')) or name.lower().endswith('.git')):
        raise DispatchError('invalid source repository URL')
    return kind, (host, port), owner, name


def _url_key(route):
    kind, origin, owner, name = route
    if kind == 'github':
        owner, name = owner.lower(), name.lower()
    return origin, owner, name



def _validated(document, owner, port):
    if set(document) - {'groups', CONFIG_KEY, 'auto_discoveries'} != {'destinations', 'repositories'}:
        raise DispatchError('invalid named manifest')
    if 'groups' in document:
        validate_groups(document['groups'])
    if CONFIG_KEY in document:
        HostLimits.from_config(document[CONFIG_KEY], default=4)
    runtime = {key: value for key, value in document.items() if key != 'groups'}
    profiles, repos = runtime['destinations'], runtime['repositories']
    if not isinstance(profiles, dict) or len(profiles) != 1 or not isinstance(repos, list):
        raise DispatchError('invalid named manifest')
    profile, settings = next(iter(profiles.items()))
    if not isinstance(profile, str) or not profile or not isinstance(settings, dict):
        raise DispatchError('invalid destination')
    if 'type' in settings:
        validate_runtime(settings, 'forgejo', f'http://127.0.0.1:{port}', owner, 'forgejo-home')
    elif settings not in ({}, {'account_type': 'personal'}, {'account_type': 'organization'}):
        raise DispatchError('invalid destination')
    parsed = parse_manifest(document)
    return profile, runtime, parsed if repos else []


def _id(value):
    if type(value) is not int or value <= 0:
        raise DispatchError('invalid repository ID')
    return value


def _source_pin(source):
    return _route(source['url'], source.get('type'))[:2], source['id']


def _pin_runtime_gitlab(repos, profile, selected, source_lookup):
    # GitLab needs a project ID even for a one-time mirror. An ephemeral
    # lookup cannot bind a previously pinned destination to a new source ID.
    pinned = {_source_pin(repo['source']) for repo in repos if 'id' in repo['source']}
    for repo, kind in selected:
        source = repo['source']
        if kind != 'gitlab' or 'id' in source:
            continue
        if 'id' in repo['mirrors'][profile] or not callable(source_lookup):
            raise DispatchError('one-time GitLab mirror requires a verified source ID')
        source['id'] = _id(source_lookup(source['url'], kind))
        pin = _source_pin(source)
        if pin in pinned:
            raise DispatchError('duplicate source repository ID')
        pinned.add(pin)


def _pin_additions(repos, profile, selected, source_lookup, prepare_destination):
    if not callable(source_lookup) or not callable(prepare_destination):
        raise DispatchError('adding to configuration requires source and destination verifiers')
    # IDs are scoped to the upstream instance, and all destination IDs share
    # the single local Forgejo instance. Do not bind aliases to new entries.
    selected_ids = {id(repo) for repo, _ in selected}
    sources, destinations = set(), set()
    for repo in repos:
        if id(repo) in selected_ids:
            continue
        source, mirror = repo['source'], repo['mirrors'][profile]
        if 'id' in source:
            sources.add(_source_pin(source))
        if 'id' in mirror:
            destinations.add(mirror['id'])

    prepared = []
    for repo, kind in selected:
        source, mirror = repo['source'], repo['mirrors'][profile]
        sid = _id(source_lookup(source['url'], kind))
        if source.get('id', sid) != sid:
            raise DispatchError('source repository ID mismatch')
        key = (_route(source['url'], kind)[:2], sid)
        if key in sources:
            raise DispatchError('duplicate source repository ID')
        sources.add(key)
        target = {**mirror, 'name': mirror.get('name', source['url'].rsplit('/', 1)[-1])}
        existing = prepare_destination(copy.deepcopy(source), copy.deepcopy(target), create=False)
        if existing is not None:
            _id(existing)
            if 'id' not in source or 'id' not in mirror:
                raise DispatchError('unpinned existing destination requires manual reconciliation')
            if existing in destinations:
                raise DispatchError('duplicate destination repository ID')
            destinations.add(existing)
        if mirror.get('id') != existing:
            raise DispatchError('destination repository ID mismatch or missing')
        prepared.append((source, mirror, target, kind, sid, existing))

    # Nothing is initialized until the last selected route passes preflight.
    # A missing target must be initialized rather than adopted on a race; the
    # callback is responsible for failing if a concurrent writer claims it.
    for source, mirror, target, kind, sid, existing in prepared:
        if _id(source_lookup(source['url'], kind)) != sid:
            raise DispatchError('source repository ID changed')
        did = _id(prepare_destination({**source, 'id': sid}, copy.deepcopy(target), create=True))
        if existing is not None and existing != did:
            raise DispatchError('destination repository ID changed')
        if existing is None and did in destinations:
            raise DispatchError('duplicate destination repository ID')
        destinations.add(did)
        source['id'] = sid
        mirror['id'] = did
        target['id'] = did

    for source, mirror, target, kind, sid, _ in prepared:
        if _id(source_lookup(source['url'], kind)) != sid:
            raise DispatchError('source repository ID changed')
        if _id(prepare_destination(copy.deepcopy(source), copy.deepcopy(target), create=False)) != mirror['id']:
            raise DispatchError('destination repository ID changed')


def plan_dispatch(document, inputs, *, owner, port, source_lookup=None, prepare_destination=None):
    """Return ``(publication_document_or_None, runtime_document, selected_urls_or_None)``.

    ``document`` is the strictly decoded authoritative JSON; ``inputs`` maps the six
    workflow input names to values. A blank dispatch preserves the scheduled
    manifest and returns None for selection. Removal returns an empty URL list
    (no workers). Explicit selections retain configured URL spelling.

    For add/update, ``source_lookup(url, kind)`` verifies the upstream clone
    identity and returns a positive source ID. ``prepare_destination(source,
    mirror, create=False)`` verifies the existing private/public destination
    and recovery binding read-only, returning its ID or None if eligible and
    missing. With ``create=True`` it verifies the pinned destination or creates
    a *new* private/public mirror with Actions disabled, returning its verified
    positive ID; a missing-at-preflight target must not be adopted if it now
    exists. Both callbacks must enforce provider origin and identity. The caller
    owns publication (including a compare-and-swap on the original bytes) and
    reconciliation if a callback fails after creating a destination.
    """
    parts, add, remove, destination, visibility, releases = _options(inputs)
    config = copy.deepcopy(document)
    if isinstance(config, list) and not parts:
        parse_manifest(config)
        return None, config, None
    if not isinstance(config, dict):
        raise DispatchError('manual selection requires a named Forgejo manifest')
    profile, _, entries = _validated(config, owner, port)
    if not parts:
        return None, config, None
    repos = config['repositories']
    groups = {(_origin(group['base_url']), group['owner'].lower()) for group in config.get('groups', ())}
    configured, historical, gitlab_origins = {}, set(), {origin for origin, _ in groups}
    for repo, entry in zip(repos, entries):
        source = repo['source']
        route = _route(source['url'], source.get('type'))
        if route[0] == 'gitlab':
            gitlab_origins.add(route[1])
        key = _url_key(route)
        if key in configured:
            raise DispatchError('ambiguous configured source')
        configured[key] = repo, entry, route
        if 'legacy_url' in entry:
            historical.add(_url_key(_route(entry['legacy_url'], source.get('type'))))

    selected, seen, removed = [], set(), set()
    for url in parts:
        # Only GitHub's recognized HTTPS API alias is rewritten. Preserve the
        # literal spelling of already configured URLs in every worker request.
        url = _GITHUB_ALIAS.sub('https://github.com/', url)
        kind, base, upstream, name = parse_repo_url(url, source_type=(
            'gitlab' if _origin(url) in gitlab_origins and url.count('/') > 4 else None
        ), allow_forgejo_port=True)
        origin = _origin(base)
        key = _url_key((kind, origin, upstream, name))
        duplicate_key = (origin, upstream.lower(), name.lower())
        if duplicate_key in seen:
            raise DispatchError('duplicate requested source')
        seen.add(duplicate_key)
        saved = configured.get(key)
        if ((origin, upstream.lower()) in groups or _GENERATED.fullmatch(destination)
                or (saved and any(_GENERATED.fullmatch(saved[1][k]) for k in
                                  ('destination_name', 'legacy_destination_name') if k in saved[1]))
                or (saved is None and _GENERATED.fullmatch(name))):
            raise DispatchError('manual dispatch cannot select a group-managed repository')
        if saved is None and key in historical:
            raise DispatchError('manual dispatch cannot select a historical source URL')
        if saved is None:
            kind = 'gitlab' if origin in gitlab_origins else kind
            normalized = f'{base}/{upstream}/{name}'
            _route(normalized, kind)
            if remove:
                raise DispatchError('cannot remove an unconfigured repository')
            source = {'url': normalized}
            if kind == 'gitlab' and origin[0] != 'gitlab.com':
                source['type'] = kind
            repo = {'source': source, 'mirrors': {profile: {}}}
            repos.append(repo)
            effective_name = name
        else:
            repo, entry, route = saved
            kind, effective_name = route[0], entry['destination_name']
            _route(url, kind)
        if remove:
            removed.add(id(repo))
            continue
        mirror = repo['mirrors'][profile]
        if destination and destination != effective_name:
            if 'id' in mirror or 'legacy' in mirror:
                raise DispatchError('cannot rename pinned destination without explicit reconciliation')
            mirror['name'] = destination
        if visibility is not None and ('id' not in mirror or visibility != mirror.get('visibility', 'private')):
            if 'id' in mirror:
                raise DispatchError('cannot change pinned destination visibility')
            mirror['visibility'] = visibility
        if releases is True:
            mirror['ignore_releases'] = True
        elif releases is False:
            mirror.pop('ignore_releases', None)
        selected.append((repo, kind))

    if remove:
        config['repositories'] = [repo for repo in repos if id(repo) not in removed]
    _, runtime, _ = _validated(config, owner, port)
    if add:
        _pin_additions(repos, profile, selected, source_lookup, prepare_destination)
    elif not remove:
        _pin_runtime_gitlab(repos, profile, selected, source_lookup)
    if not remove:
        _validated(config, owner, port)
    publish = config if (add or remove) and config != document else None
    return publish, runtime, [repo['source']['url'] for repo, _ in selected]
