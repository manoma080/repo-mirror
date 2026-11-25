"""adhoc: turn repository URLs typed into a manual run into sources.

Two uses:
- ``add_to_config``: write URLs into config.json (the "add to config" checkbox).
- ``one_time_sources``: build in-memory sources for URLs that config.json does
  not already cover, so a one-off mirror works without changing the config.

URLs must be exact ``https://host/owner/name`` links. A ``github.com`` host is
treated as a GitHub source; any other host is treated as a Forgejo source.
Errors never echo the input, which may contain credentials.
"""

from dataclasses import dataclass
import json
import os
import re
import sys
from urllib.parse import urlsplit

from repo_mirror.constants import NAME
from repo_mirror.source_filter import entry_urls

_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


@dataclass(frozen=True)
class ClassifiedURL:
    kind: str
    source_type: str
    base_url: str
    owner: str
    name: str | None
    url: str
    existing: bool
    owner_id: int | None = None


def parse_repo_url(url, *, allow_forgejo_port=False, source_type=None):
    """Parse an exact repository URL; ports are opt-in for Forgejo verification."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        parts = parsed.path.split("/")
        kind = source_type or ('github' if host == 'github.com' else 'gitlab' if host == 'gitlab.com' else 'forgejo')
        valid = (
            bool(url)
            and all(ord(c) > 32 and ord(c) != 127 for c in url)
            and parsed.scheme == "https"
            and bool(host)
            and parsed.username is None
            and parsed.password is None
            and (parsed.port is None or (allow_forgejo_port and host != 'github.com' and parsed.port > 0))
            and not parsed.query
            and not parsed.fragment
            and "?" not in url
            and "#" not in url
            and kind in ('github', 'forgejo', 'gitlab')
            and not (host == 'github.com' and kind != 'github')
            and not (kind == 'github' and host != 'github.com')
            and not (host == 'gitlab.com' and kind != 'gitlab')
            and (len(parts) >= 3 if kind == 'gitlab' else len(parts) == 3)
            and parts[0] == ""
        )
    except ValueError:
        raise ValueError("invalid source repository URL") from None
    if not valid:
        raise ValueError("invalid source repository URL")
    if any(not NAME.fullmatch(part) or part in ('.', '..') for part in parts[1:]):
        raise ValueError("invalid source repository URL")
    owner, name = '/'.join(parts[1:-1]), parts[-1]
    if host == "github.com":
        return "github", "https://github.com", owner, name
    authority = parsed.netloc if parsed.port is not None else host
    return kind, "https://" + authority, owner, name


def parse_owner_url(url, *, allow_forgejo_port=False, source_type=None):
    """Parse an exact credential-free HTTPS owner or GitLab group URL."""
    if not isinstance(url, str) or not url:
        raise ValueError("invalid source owner URL")
    if source_type is not None and (not isinstance(source_type, str) or source_type not in ("github", "forgejo", "gitlab")):
        raise ValueError("invalid source owner URL")
    if not all(33 <= ord(c) <= 126 for c in url):
        raise ValueError("invalid source owner URL")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError("invalid source owner URL") from None
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
        or (port is None and ":" in parsed.netloc)
        or (port is not None and not (allow_forgejo_port and host != "github.com" and port > 0))
        or host.startswith("github.com.")
        or not all(_HOST_LABEL.fullmatch(label) for label in host.split("."))
        or parsed.query
        or parsed.fragment
        or "?" in url
        or "#" in url
        or not parsed.path.startswith("/")
        or parsed.path.endswith("/")
    ):
        raise ValueError("invalid source owner URL")
    segments = tuple(parsed.path.split("/")[1:])
    if not segments or any(
        not seg or not NAME.fullmatch(seg) or seg in (".", "..") or seg.lower().endswith(".git")
        for seg in segments
    ):
        raise ValueError("invalid source owner URL")
    kind = source_type if source_type is not None else (
        "github" if host == "github.com" else "gitlab" if host == "gitlab.com" else "forgejo"
    )
    if (
        (host == "github.com" and kind != "github")
        or (kind == "github" and host != "github.com")
        or (host == "gitlab.com" and kind != "gitlab")
    ):
        raise ValueError("invalid source owner URL")
    if kind != "gitlab" and len(segments) != 1:
        raise ValueError("invalid source owner URL")
    if host == "github.com":
        return "github", "https://github.com", segments
    authority = f"{host}:{port}" if port is not None else host
    return kind, "https://" + authority, segments


def _source_for(source_type, base_url, owner, name):
    return {"type": source_type, "base_url": base_url, "owner": owner, "repos": [name]}


def _matches_owner(source, kind, base, owner):
    configured = str(source.get("base_url", "")).rstrip("/").lower()
    if kind == "github" and configured == "https://api.github.com":
        configured = "https://github.com"
    return (source.get("type") == kind and configured == base
            and str(source.get("owner", "")).lower() == owner.lower())


def parse_selected_url(url, sources):
    """Resolve explicitly configured origins before applying host heuristics."""
    matches = []
    for source in sources:
        if source.get('type') != 'gitlab':
            continue
        try:
            parsed = parse_repo_url(url, source_type='gitlab')
        except ValueError:
            continue
        if _matches_owner(source, *parsed[:3]):
            matches.append(parsed)
    if len({row[:3] for row in matches}) > 1:
        raise ValueError('ambiguous source repository URL')
    return matches[0] if matches else parse_repo_url(url)


def _gitlab_probe_client():
    from repo_mirror.github_api import SourceAPI

    client = SourceAPI(None)
    if callable(getattr(client, 'restrict_origin', None)):
        client.restrict_origin('https://gitlab.com')
    return client


def _probe_gitlab_two_segment(path, gitlab_api=None):
    from urllib.error import HTTPError
    from urllib.parse import quote
    from repo_mirror.github_api import not_found

    client = gitlab_api if gitlab_api is not None else _gitlab_probe_client()
    encoded = quote(path, safe='')

    def probe(endpoint):
        try:
            return client.request('GET', f'https://gitlab.com/api/v4/{endpoint}/{encoded}')
        except HTTPError as exc:
            if not_found(exc):
                exc.close()
                return None
            raise

    group_data = probe('groups')
    project_data = probe('projects')
    if group_data is not None and project_data is not None:
        raise ValueError('ambiguous owner or repository URL')
    if group_data is not None:
        if not isinstance(group_data, dict):
            raise ValueError('invalid source owner metadata')
        gid = group_data.get('id')
        if type(gid) is not int or gid <= 0:
            raise ValueError('invalid source owner metadata')
        if 'visibility' in group_data and group_data['visibility'] != 'public':
            raise ValueError('source owner identity mismatch')
        canonical = group_data.get('full_path', path)
        if not isinstance(canonical, str) or canonical.lower() != path.lower():
            raise ValueError('source owner identity mismatch')
        return 'rule', gid, canonical
    if project_data is not None:
        if not isinstance(project_data, dict) or type(project_data.get('id')) is not int or project_data['id'] <= 0:
            raise ValueError('invalid source repository metadata')
        return 'repository', None, None
    raise ValueError('unknown or unsupported source repository URL')


def _probe_gitlab_single_segment(username, gitlab_api=None):
    from urllib.error import HTTPError
    from urllib.parse import quote
    from repo_mirror.github_api import not_found

    client = gitlab_api if gitlab_api is not None else _gitlab_probe_client()
    encoded = quote(username, safe='')
    try:
        users = client.request('GET', f'https://gitlab.com/api/v4/users?username={encoded}')
    except HTTPError as exc:
        if not_found(exc):
            exc.close()
            users = []
        else:
            raise
    if not isinstance(users, list):
        raise ValueError('invalid source owner metadata')
    if not users:
        raise ValueError('unknown or unsupported source repository URL')
    if len(users) != 1 or not isinstance(users[0], dict):
        raise ValueError('invalid source owner metadata')
    user = users[0]
    uid = user.get('id')
    canonical = user.get('username')
    if type(uid) is not int or uid <= 0 or not isinstance(canonical, str):
        raise ValueError('invalid source owner metadata')
    if not NAME.fullmatch(canonical) or canonical.lower() != username.lower():
        raise ValueError('source owner identity mismatch')
    return uid, canonical


def _configured_gitlab_sources(config):
    if not isinstance(config, dict):
        return []
    sources = list(config.get('sources', []))
    for rule in config.get('auto_discoveries', []):
        if isinstance(rule, dict) and rule.get('type') == 'gitlab' and isinstance(rule.get('url'), str):
            try:
                _, base, segments = parse_owner_url(rule['url'], source_type='gitlab')
            except ValueError:
                continue
            sources.append({'type': 'gitlab', 'base_url': base, 'owner': '/'.join(segments)})
    for entry in config.get('repositories', []):
        if not isinstance(entry, dict):
            continue
        src = entry.get('source', {})
        if isinstance(src, dict) and src.get('type') == 'gitlab' and isinstance(src.get('url'), str):
            try:
                _, base, owner, _ = parse_repo_url(src['url'], source_type='gitlab')
            except ValueError:
                continue
            sources.append({'type': 'gitlab', 'base_url': base, 'owner': owner})
    return sources


def classify_source_url(url, config=None, *, gitlab_api=None, probe_live=True):
    """Classify one source_urls token as a discovery rule or a repository URL."""
    if not isinstance(url, str) or not url or url.strip() != url:
        raise ValueError('invalid source repository URL')

    rule_match = None
    repo_match = None
    if isinstance(config, dict):
        for rule in config.get('auto_discoveries', []):
            if not isinstance(rule, dict) or not isinstance(rule.get('url'), str):
                continue
            try:
                r_kind, r_base, r_segments = parse_owner_url(rule['url'], source_type=rule.get('type'))
                u_kind, u_base, u_segments = parse_owner_url(url, source_type=r_kind)
            except ValueError:
                continue
            r_owner = '/'.join(r_segments)
            if (u_base.lower(), '/'.join(u_segments).lower()) == (r_base.lower(), r_owner.lower()):
                rule_id = rule.get('id') if type(rule.get('id')) is int and rule.get('id') > 0 else None
                rule_match = ClassifiedURL(
                    kind='rule',
                    source_type=r_kind,
                    base_url=r_base,
                    owner=r_owner,
                    name=None,
                    url=f'{r_base}/{r_owner}',
                    existing=True,
                    owner_id=rule_id,
                )
                break

        if 'repositories' in config:
            for entry in config.get('repositories', []):
                if not isinstance(entry, dict):
                    continue
                src = entry.get('source', {})
                if not isinstance(src, dict) or not isinstance(src.get('url'), str):
                    continue
                try:
                    e_kind, e_base, e_owner, e_name = parse_repo_url(src['url'], source_type=src.get('type'))
                    u_kind, u_base, u_owner, u_name = parse_repo_url(url, source_type=e_kind)
                except ValueError:
                    continue
                if (u_base.lower(), u_owner.lower(), u_name.lower()) == (
                    e_base.lower(),
                    e_owner.lower(),
                    e_name.lower(),
                ):
                    repo_match = ClassifiedURL(
                        kind='repository',
                        source_type=e_kind,
                        base_url=e_base,
                        owner=e_owner,
                        name=e_name,
                        url=f'{e_base}/{e_owner}/{e_name}',
                        existing=True,
                    )
                    break
        elif 'sources' in config:
            for src in config.get('sources', []):
                if not isinstance(src, dict) or src.get('repos') == 'all':
                    continue
                try:
                    u_kind, u_base, u_owner, u_name = parse_repo_url(url, source_type=src.get('type'))
                except ValueError:
                    continue
                if _matches_owner(src, u_kind, u_base, u_owner):
                    saved_name = next((r for r in src.get('repos', []) if r.lower() == u_name.lower()), None)
                    if saved_name is not None:
                        base_norm = 'https://github.com' if src.get('type') == 'github' and str(src.get('base_url', '')).rstrip('/').lower() == 'https://api.github.com' else str(src.get('base_url', '')).rstrip('/')
                        repo_match = ClassifiedURL(
                            kind='repository',
                            source_type=src['type'],
                            base_url=base_norm,
                            owner=src['owner'],
                            name=saved_name,
                            url=f"{base_norm}/{src['owner']}/{saved_name}",
                            existing=True,
                        )
                        break

    if rule_match is not None and repo_match is not None:
        raise ValueError('ambiguous source URL matches both an auto-discovery rule and a repository entry')
    if rule_match is not None:
        return rule_match
    if repo_match is not None:
        return repo_match

    gitlab_sources = _configured_gitlab_sources(config)
    try:
        kind, base, owner, name = parse_selected_url(url, gitlab_sources)
    except ValueError as exc:
        if str(exc) == 'ambiguous source repository URL':
            raise
        try:
            kind, base, segments = parse_owner_url(url)
        except ValueError:
            raise ValueError('invalid source repository URL') from None
        owner = '/'.join(segments)
        if kind == 'gitlab' and base.lower() == 'https://gitlab.com' and len(segments) == 1:
            if probe_live:
                uid, canonical_owner = _probe_gitlab_single_segment(owner, gitlab_api=gitlab_api)
                return ClassifiedURL(
                    kind='rule',
                    source_type='gitlab',
                    base_url='https://gitlab.com',
                    owner=canonical_owner,
                    name=None,
                    url=f'https://gitlab.com/{canonical_owner}',
                    existing=False,
                    owner_id=uid,
                )
        return ClassifiedURL(
            kind='rule',
            source_type=kind,
            base_url=base,
            owner=owner,
            name=None,
            url=f'{base}/{owner}',
            existing=False,
        )

    if kind == 'gitlab' and base.lower() == 'https://gitlab.com' and '/' not in owner:
        try:
            parse_owner_url(url)
        except ValueError:
            pass
        else:
            if probe_live:
                probe_kind, gid, canonical_owner = _probe_gitlab_two_segment(f'{owner}/{name}', gitlab_api=gitlab_api)
                if probe_kind == 'rule':
                    return ClassifiedURL(
                        kind='rule',
                        source_type='gitlab',
                        base_url='https://gitlab.com',
                        owner=canonical_owner,
                        name=None,
                        url=f'https://gitlab.com/{canonical_owner}',
                        existing=False,
                        owner_id=gid,
                    )
    return ClassifiedURL(
        kind='repository',
        source_type=kind,
        base_url=base,
        owner=owner,
        name=name,
        url=f'{base}/{owner}/{name}',
        existing=False,
    )


def _add_to_config_named(config, classified, destination="", visibility=None, ignore_releases=None):
    from repo_mirror.config_named import normalize_config
    import copy

    normalize_config(config)
    candidate = copy.deepcopy(config)
    profile = next(iter(candidate['destinations']))
    if visibility not in (None, 'private', 'public'):
        raise ValueError('invalid visibility')
    if ignore_releases not in (None, True, False) or (ignore_releases is not None and not isinstance(ignore_releases, bool)):
        raise ValueError('invalid ignore-releases')

    rule_items = [item for item in classified if item.kind == 'rule']
    repo_items = [item for item in classified if item.kind == 'repository']
    if destination and rule_items:
        raise ValueError('destination name is derived per repository for a discovery rule')
    if destination and (len(repo_items) != 1 or len(destination) > 100 or not NAME.fullmatch(destination) or destination in ('.', '..')):
        raise ValueError('custom destination requires one URL and a valid repository name')

    rule_identities = [(item.base_url.lower(), item.owner.lower()) for item in rule_items]
    if len(set(rule_identities)) != len(rule_identities) or any(item.existing for item in rule_items):
        raise ValueError('duplicate auto-discovery rule')

    repo_identities = [(item.source_type, item.base_url.lower(), item.owner.lower(), item.name.lower()) for item in repo_items]
    if len(set(repo_identities)) != len(repo_identities):
        raise ValueError('duplicate source repository')

    for item in rule_items:
        rule_entry = {
            'url': item.url,
            'visibility': visibility if visibility is not None else 'private',
            'ignore_releases': bool(ignore_releases) if ignore_releases is not None else False,
            'destination_name_prefix': 'mirror-' + item.owner.replace('/', '-'),
        }
        if item.source_type == 'gitlab':
            rule_entry['type'] = 'gitlab'
        if item.owner_id is not None:
            rule_entry['id'] = item.owner_id
        candidate.setdefault('auto_discoveries', []).append(rule_entry)

    for item, identity in zip(repo_items, repo_identities):
        entry = next(
            (
                e for e in candidate['repositories']
                if tuple(x.lower() for x in parse_repo_url(e['source']['url'], source_type=e['source'].get('type'))) == identity
            ),
            None,
        )
        if entry is None:
            entry = {'source': {'url': item.base_url + '/' + item.owner + '/' + item.name}, 'mirrors': {profile: {}}}
            if item.source_type == 'gitlab' and item.base_url != 'https://gitlab.com':
                entry['source']['type'] = 'gitlab'
            candidate['repositories'].append(entry)
        mirror = entry['mirrors'][profile]
        if destination:
            _, _, saved_owner, saved_name = parse_repo_url(entry['source']['url'], source_type=entry['source'].get('type'))
            current_name = mirror.get('name', 'mirror-' + saved_owner + '-' + saved_name)
            if 'id' in mirror and destination != current_name:
                raise ValueError('cannot rename pinned destination without explicit reconciliation')
            mirror['name'] = destination
        if visibility is not None:
            mirror['visibility'] = visibility
        if ignore_releases is True:
            mirror['ignore_releases'] = True
        elif ignore_releases is False:
            mirror.pop('ignore_releases', None)
    normalize_config(candidate)
    changed = candidate != config
    if changed:
        config.clear()
        config.update(candidate)
    return changed


def add_to_config(config, source_urls, destination="", visibility=None, *, protect_pins=True, ignore_releases=None, gitlab_api=None):
    """Add URLs and optional per-repository or rule-level destination settings."""
    if 'repositories' in config or 'destinations' in config:
        classified = [classify_source_url(part.strip(), config, gitlab_api=gitlab_api) for part in source_urls.split(',')]
        return _add_to_config_named(config, classified, destination, visibility, ignore_releases)
    parts = source_urls.split(",")
    parsed = [parse_selected_url(part.strip(), config['sources']) for part in parts]
    identities = [tuple(part.lower() for part in item) for item in parsed]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate source repository")
    if visibility not in (None, "private", "public"):
        raise ValueError("invalid visibility")
    if destination and (len(parsed) != 1 or len(destination) > 100 or not NAME.fullmatch(destination) or destination in (".", "..")):
        raise ValueError("custom destination requires one URL and a valid repository name")
    changed = False
    for source_type, base_url, owner, name in parsed:
        matching_owners = [s for s in config["sources"]
                           if _matches_owner(s, source_type, base_url, owner)]
        # Prefer the source that already owns this repository. Named configs
        # normalize to one source per repo, including multiple entries per owner.
        match = next((s for s in matching_owners
                      if s["repos"] != "all" and name.lower() in {r.lower() for r in s["repos"]}), None)
        if match is None:
            match = next((s for s in matching_owners if s["repos"] == "all"), None)
        if match is None:
            match = next(iter(matching_owners), None)
        if match is None:
            match = _source_for(source_type, base_url, owner, name)
            config["sources"].append(match)
            changed = True
        elif match["repos"] != "all" and name.lower() not in {r.lower() for r in match["repos"]}:
            match["repos"].append(name)
            changed = True
        if destination or visibility is not None or ignore_releases is not None:
            mappings = match.setdefault("destinations", {})
            old = mappings.get(name.lower(), {})
            if protect_pins and destination and 'destination_id' in old:
                from repo_mirror.sources import destination_name
                saved_name = next((r for r in match['repos'] if r.lower() == name.lower()), name) if match['repos'] != 'all' else name
                if destination != destination_name(match, saved_name):
                    raise ValueError('cannot rename pinned destination without explicit reconciliation')
            new = dict(old)
            if visibility is not None:
                new["visibility"] = visibility
            if destination:
                new["name"] = destination
            if ignore_releases is True:
                new["ignore_releases"] = True
            elif ignore_releases is False:
                new.pop("ignore_releases", None)
            if old != new:
                mappings[name.lower()] = new
                changed = True
    return changed


def add_to_config_pinned(config, source_urls, github_source, forgejo_source, dst, *, owner, account_type, destination='', visibility=None, ignore_releases=None, gitlab_api=None):
    """Stage additions, verify identities and create missing mirrors before committing config.

    Failed operations can leave initializing repositories remotely. Existing
    unpinned destinations require manual reconciliation, not marker adoption.
    """
    import copy
    from urllib.parse import quote
    from repo_mirror.config_named import normalize_config
    from repo_mirror.constants import GITHUB
    from repo_mirror.github_api import API
    from repo_mirror.repository import GitHubAPI, validate_source_clone_url
    from repo_mirror.sources import (check_existing, destination_name, discover, get_existing,
                                     source_api_base, source_marker, validate_source)

    candidate = copy.deepcopy(config)
    probe_client = gitlab_api if gitlab_api is not None else (
        None if (isinstance(API, type) and isinstance(forgejo_source, API)) else forgejo_source
    )
    is_named = 'repositories' in candidate or 'destinations' in candidate
    if is_named:
        classified = [classify_source_url(part.strip(), candidate, gitlab_api=probe_client) for part in source_urls.split(',')]
        _add_to_config_named(candidate, classified, destination, visibility, ignore_releases)
        repo_items = [item for item in classified if item.kind == 'repository']
        requested = [(item.source_type, item.base_url.lower(), item.owner.lower(), item.name.lower()) for item in repo_items]
    else:
        add_to_config(candidate, source_urls, destination, visibility, ignore_releases=ignore_releases)
        requested = None
    normalized = normalize_config(candidate)
    profile = normalized.get('destination_profile')
    if profile and (profile['url'] != 'https://github.com' or profile['owner'].lower() != owner.lower()):
        raise ValueError('destination profile runtime mismatch')
    if account_type not in ('personal', 'organization') or not isinstance(owner, str) or not NAME.fullmatch(owner):
        raise ValueError('invalid destination account')
    if requested is None:
        requested = [tuple(x.lower() for x in parse_selected_url(part.strip(), normalized['sources'])) for part in source_urls.split(',')]
    if not requested:
        changed = candidate != config
        if changed:
            config.clear()
            config.update(candidate)
        return changed
    # A separate anonymous client per Forgejo origin binds both direct requests
    # and every redirect, including discovery and subsequent identity rechecks.
    from repo_mirror.github_api import API, SourceAPI
    forgejo_clients = {}
    def source_client(source):
        if source['type'] == 'github':
            if callable(getattr(github_source, 'restrict_origin', None)):
                github_source.restrict_origin('https://api.github.com')
            return github_source
        if not isinstance(forgejo_source, API):
            return forgejo_source  # injected test/provider implementation
        base = source['base_url']
        if base not in forgejo_clients:
            client = SourceAPI(None)
            client.restrict_origin(base)
            forgejo_clients[base] = client
        return forgejo_clients[base]

    def read_source(api, source, name, upstream):
        metadata = api.request('GET', upstream)
        if source['type'] == 'gitlab':
            if (not isinstance(metadata, dict) or metadata.get('visibility') != 'public'
                    or metadata.get('path_with_namespace') != source['owner'] + '/' + name):
                raise ValueError('GitLab project identity or visibility mismatch')
            metadata = {**metadata, 'clone_url': metadata.get('http_url_to_repo')}
        validate_source_clone_url(source, name, metadata)
        return metadata

    selected, seen = [], set()
    for source in normalized['sources']:
        validate_source(source)
        names = discover(source, source_client(source))
        for name in names:
            target_name = destination_name(source, name)
            if target_name.lower() in seen:
                raise ValueError('duplicate destination repository')
            seen.add(target_name.lower())
            for identity in requested:
                if _matches_owner(source, *identity[:3]) and name.lower() == identity[3]:
                    selected.append((source, name, target_name))
    if len(selected) != len(requested):
        raise ValueError('selected repository not discovered in config')
    # Validate all source IDs before making any destination writes.
    prepared = []
    for source, name, target_name in selected:
        settings = source.get('destinations', {}).get(name.lower(), {})
        if 'legacy' in settings:
            from repo_mirror.migration_attestation import github_claim, verify
            verify(github_claim(source, name, owner), settings['legacy']['signature'], os.environ.get('MIRROR_TIMESTAMP_SECRET', ''))
            legacy = settings['legacy']
            marker = source_marker({**source, 'owner': legacy['source_owner']}, legacy['source_name'])
        else:
            marker = source_marker(source, name)
        api = source_client(source)
        upstream = (source_api_base(source) + '/projects/' + quote(source['owner'] + '/' + name, safe='')
                    if source['type'] == 'gitlab' else
                    source_api_base(source) + '/repos/' + quote(source['owner'], safe='') + '/' + quote(name, safe=''))
        metadata = read_source(api, source, name, upstream)
        sid = metadata.get('id') if isinstance(metadata, dict) else None
        if type(sid) is not int or sid <= 0:
            raise ValueError('invalid source repository id')
        if 'source_id' in settings and sid != settings['source_id']:
            raise ValueError('source repository id mismatch')
        target = GITHUB + '/repos/' + quote(owner, safe='') + '/' + quote(target_name, safe='')
        repo = get_existing(dst, target)
        check_existing(repo, marker)
        if repo is not None and ('destination_id' not in settings or 'source_id' not in settings):
            raise ValueError('unpinned existing destination requires manual reconciliation')
        if 'destination_id' in settings and (not isinstance(repo, dict) or repo.get('id') != settings['destination_id'] or type(repo.get('id')) is not int):
            raise ValueError('destination repository id mismatch or missing')
        prepared.append((source, name, target_name, settings, marker, api, upstream, sid, target, repo))
    verified_destinations = []
    for source, name, target_name, settings, marker, api, upstream, sid, target, repo in prepared:
        if source['type'] == 'gitlab':
            current = read_source(api, source, name, upstream)
            if type(current.get('id')) is not int or current['id'] != sid:
                raise ValueError('source repository id changed')
        if repo is None:
            payload = {'name': target_name, 'private': settings.get('visibility', 'private') != 'public',
                       'auto_init': False, 'description': marker + ' [repo-mirror:initializing:v1]'}
            if account_type == 'personal' and isinstance(dst, GitHubAPI):
                created = dst.create_personal_repository(owner, payload)
            else:
                if account_type == 'personal':
                    identity = dst.request('GET', GITHUB + '/user')
                    if not isinstance(identity, dict) or str(identity.get('login', '')).lower() != owner.lower():
                        raise RuntimeError('authenticated GitHub user does not match configured destination')
                endpoint = '/user/repos' if account_type == 'personal' else '/orgs/' + quote(owner, safe='') + '/repos'
                created = dst.request('POST', GITHUB + endpoint, payload)
            if not isinstance(created, dict) or str(created.get('full_name', '')).lower() != (owner + '/' + target_name).lower():
                raise RuntimeError('created repository owner does not match configured destination')
            created_id = created.get('id')
            if type(created_id) is not int or created_id <= 0:
                raise ValueError('invalid created destination repository id')
            current = read_source(api, source, name, upstream)
            if not isinstance(current, dict) or type(current.get('id')) is not int or current['id'] != sid:
                raise ValueError('source repository id changed')
            validate_source_clone_url(source, name, current)

            def verify_created(repo):
                if (not isinstance(repo, dict) or type(repo.get('id')) is not int
                        or repo['id'] != created_id
                        or str(repo.get('full_name', '')).lower() != (owner + '/' + target_name).lower()):
                    raise ValueError('created destination repository identity not verified')
                check_existing(repo, marker)

            verify_created(get_existing(dst, target))
            dst.request('PUT', target + '/actions/permissions', {'enabled': False})
            permissions = dst.request('GET', target + '/actions/permissions')
            if not isinstance(permissions, dict) or permissions.get('enabled') is not False:
                raise ValueError('destination Actions not verified disabled')
            repo = get_existing(dst, target)
            verify_created(repo)
        check_existing(repo, marker)
        did = repo.get('id') if isinstance(repo, dict) else None
        if type(did) is not int or did <= 0:
            raise ValueError('invalid destination repository id')
        verified_destinations.append((target, marker, did))
        current = read_source(api, source, name, upstream)
        if not isinstance(current, dict) or type(current.get('id')) is not int or current['id'] != sid:
            raise ValueError('source repository id changed')
        if 'repositories' in candidate:
            entry = next(e for e in candidate['repositories'] if tuple(x.lower() for x in parse_repo_url(e['source']['url'], source_type=e['source'].get('type'))) == (source['type'], source['base_url'].lower(), source['owner'].lower(), name.lower()))
            entry['source']['id'] = sid
            entry['mirrors'][next(iter(candidate['destinations']))]['id'] = did
        else:
            # Normalization returns the original legacy structure.
            source.setdefault('destinations', {}).setdefault(name.lower(), {}).update(source_id=sid, destination_id=did)
    # Re-read every destination after the batch, including pre-existing pins.
    for target, marker, expected in verified_destinations:
        current = get_existing(dst, target)
        check_existing(current, marker)
        if not isinstance(current, dict) or type(current.get('id')) is not int or current['id'] != expected:
            raise ValueError('destination repository id changed')
    for source, name, _, _, _, api, upstream, sid, _, _ in prepared:
        current = read_source(api, source, name, upstream)
        if not isinstance(current, dict) or type(current.get('id')) is not int or current['id'] != sid:
            raise ValueError('source repository id changed')
        validate_source_clone_url(source, name, current)
    normalize_config(candidate)
    changed = candidate != config
    if changed:
        config.clear()
        config.update(candidate)
    return changed


def remove_from_config(config, source_urls):
    """Remove repository entries or discovery rules from config without deleting mirrors."""
    if 'repositories' in config or 'destinations' in config:
        import copy
        from repo_mirror.config_named import normalize_config

        normalize_config(config)
        candidate = copy.deepcopy(config)
        profile = next(iter(candidate['destinations']))
        classified = [
            classify_source_url(part.strip(), candidate, probe_live=False)
            for part in source_urls.split(',')
        ]
        rules = candidate.get('auto_discoveries', [])
        rule_by_url = {}
        for rule in rules:
            r_kind, r_base, r_segments = parse_owner_url(rule['url'], source_type=rule.get('type'))
            canonical = f"{r_base}/{'/'.join(r_segments)}"
            rule_by_url[canonical.lower()] = (rule, r_kind, '/'.join(r_segments))

        repo_identities = {
            (item.base_url.lower(), item.owner.lower(), item.name.lower())
            for item in classified
            if item.kind == 'repository'
        }
        kept_repos = []
        for entry in candidate['repositories']:
            _, e_base, e_owner, e_name = parse_repo_url(entry['source']['url'], source_type=entry['source'].get('type'))
            if (e_base.lower(), e_owner.lower(), e_name.lower()) in repo_identities:
                discovered_by = entry.get('discovered_by')
                if isinstance(discovered_by, str) and discovered_by.lower() in rule_by_url:
                    owner_rule, _, _ = rule_by_url[discovered_by.lower()]
                    exclude = owner_rule.setdefault('exclude', [])
                    if e_name.lower() not in {x.lower() for x in exclude}:
                        exclude.append(e_name)
                    if isinstance(owner_rule.get('overrides'), dict):
                        for ov_key in [k for k in owner_rule['overrides'] if k.lower() == e_name.lower()]:
                            owner_rule['overrides'].pop(ov_key, None)
                        if not owner_rule['overrides']:
                            owner_rule.pop('overrides', None)
                continue
            kept_repos.append(entry)
        candidate['repositories'] = kept_repos

        rule_urls_to_remove = {item.url.lower() for item in classified if item.kind == 'rule' and item.existing}
        if rule_urls_to_remove:
            for entry in candidate['repositories']:
                discovered_by = entry.get('discovered_by')
                if isinstance(discovered_by, str) and discovered_by.lower() in rule_urls_to_remove:
                    rule_info = rule_by_url.get(discovered_by.lower())
                    mirror = entry['mirrors'][profile]
                    if 'name' not in mirror and rule_info is not None:
                        owner_rule, r_kind, _ = rule_info
                        _, _, c_owner, c_name = parse_repo_url(entry['source']['url'], source_type=entry['source'].get('type'))
                        ov = next(
                            (v for k, v in owner_rule.get('overrides', {}).items() if k.lower() == c_name.lower()),
                            None,
                        )
                        prefix = owner_rule.get('destination_name_prefix') or ('mirror-' + c_owner.replace('/', '-'))
                        effective_name = ov['name'] if (ov and 'name' in ov) else f'{prefix}-{c_name}'
                        default_owner = c_owner.replace('/', '-') if r_kind == 'gitlab' else c_owner
                        if effective_name != f'mirror-{default_owner}-{c_name}':
                            mirror['name'] = effective_name
                    entry.pop('discovered_by', None)
            kept_rules = []
            for rule in rules:
                r_kind, r_base, r_segments = parse_owner_url(rule['url'], source_type=rule.get('type'))
                canonical = f"{r_base}/{'/'.join(r_segments)}"
                if canonical.lower() not in rule_urls_to_remove:
                    kept_rules.append(rule)
            if 'auto_discoveries' in candidate:
                candidate['auto_discoveries'] = kept_rules

        normalize_config(candidate)
        changed = candidate != config
        if changed:
            config.clear()
            config.update(candidate)
        return changed
    parsed = [parse_repo_url(part.strip()) for part in source_urls.split(",")]
    for kind, base, owner, name in parsed:
        for source in config["sources"]:
            if _matches_owner(source, kind, base, owner) and source.get("repos") == "all":
                raise ValueError("cannot remove individual repositories from an all source")
    changed = False
    for kind, base, owner, name in parsed:
        for source in list(config["sources"]):
            if _matches_owner(source, kind, base, owner):
                repos = source["repos"]
                kept = [repo for repo in repos if repo.lower() != name.lower()]
                if len(kept) != len(repos):
                    source["repos"] = kept
                    source.get("destinations", {}).pop(name.lower(), None)
                    changed = True
                    if not kept:
                        config["sources"].remove(source)
    return changed


def one_time_sources(source_urls, plan, configured_sources=(), *, auto_discoveries=(), gitlab_api=None):
    """Return sources for URLs not already covered by the config-discovered plan.

    Each returned source lists exactly one repository and is never written to config.
    A selected repository under a configured all source must have been discovered.
    Unconfigured owner URLs are rejected.
    """
    if source_urls is None or not source_urls.strip():
        return []
    covered = set()
    for _, source, name in plan:
        covered |= {url.lower() for url in entry_urls(source, name)}
    lookup_cfg = {'sources': list(configured_sources), 'auto_discoveries': list(auto_discoveries)}
    sources = []
    for part in source_urls.split(","):
        url = part.strip()
        item = classify_source_url(url, lookup_cfg, gitlab_api=gitlab_api)
        if item.kind == 'rule':
            if item.existing:
                continue
            raise ValueError("one-time mirroring is not supported for owner URLs; enable add_to_config")
        source_type, base_url, owner, name = item.source_type, item.base_url, item.owner, item.name
        if url.lower() not in covered:
            if any(source.get("repos") == "all" and _matches_owner(source, source_type, base_url, owner)
                   for source in configured_sources):
                raise ValueError("selected repository not discovered in all source")
            sources.append(_source_for(source_type, base_url, owner, name))
    return sources


def main(argv=None):
    """Command line: ``python -m repo_mirror.adhoc --config config.json --urls "..."``."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--urls", required=True)
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--auto-pin", action="store_true")
    parser.add_argument("--destination", default="")
    parser.add_argument("--visibility", choices=("private", "public", "unchanged"), default="unchanged")
    parser.add_argument("--ignore-releases", choices=("keep", "true", "false"), default="keep",
                        help="persist ignore_releases for the saved repositories; keep leaves each entry unchanged")
    args = parser.parse_args(argv)
    if not args.urls.strip():
        raise ValueError("at least one repository URL is required")
    if args.remove and (args.destination or args.visibility not in ("private", "unchanged")
                        or args.ignore_releases != "keep"):
        raise ValueError("remove cannot be combined with destination settings")
    ignore_releases = None if args.ignore_releases == "keep" else args.ignore_releases == "true"
    with open(args.config, encoding="utf-8") as f:
        from repo_mirror.config_json import unique_pairs
        config = json.load(f, object_pairs_hook=unique_pairs)
    if args.auto_pin and not args.remove:
        from repo_mirror.github_api import API, SourceAPI, AppTokenProvider
        from repo_mirror.config_named import normalize_config
        from repo_mirror.destination_profile import validate_runtime
        runtime = normalize_config(config)
        owner = os.environ.get('DESTINATION_OWNER', '')
        account_type = runtime.get('destination_account_type')
        configured_type = os.environ.get('DESTINATION_ACCOUNT_TYPE')
        if configured_type and account_type and configured_type != account_type:
            raise ValueError('destination account type mismatch')
        account_type = configured_type or account_type
        if account_type not in ('personal', 'organization'):
            raise ValueError('destination_account_type must be personal or organization')
        if runtime.get('destination_profile'):
            validate_runtime(runtime['destination_profile'], 'github', 'https://github.com', owner, 'mirrorbot')
        app_id = os.environ.get('MIRROR_APP_ID', '')
        private_key = os.environ.pop('MIRROR_APP_PRIVATE_KEY', None)
        if not app_id or not private_key:
            raise ValueError('mirrorbot app credentials required')
        token = AppTokenProvider(app_id, private_key, owner, account_type)
        changed = add_to_config_pinned(config, args.urls, SourceAPI(token), API(), API(token),
                                       owner=owner,
                                       account_type=account_type,
                                       destination=args.destination,
                                       visibility=None if args.visibility == 'unchanged' else args.visibility,
                                       ignore_releases=ignore_releases)
    else:
        changed = (remove_from_config(config, args.urls) if args.remove else
                   add_to_config(config, args.urls, args.destination,
                                 None if args.visibility == "unchanged" else args.visibility,
                                 ignore_releases=ignore_releases))
    if not args.remove and not args.auto_pin:
        from repo_mirror.sources import destination_name, validate_source, discover
        from repo_mirror.github_api import API, SourceAPI
        seen = set()
        requested = [
            (item.source_type, item.base_url, item.owner, item.name)
            for item in (classify_source_url(part.strip(), config, probe_live=False) for part in args.urls.split(","))
            if item.kind == 'repository'
        ]
        github_api, other_api = SourceAPI(os.environ.get("GH_TOKEN")), API()
        if callable(getattr(github_api, 'restrict_origin', None)):
            github_api.restrict_origin('https://api.github.com')
        from repo_mirror.config_named import normalize_config
        for source in normalize_config(config)["sources"]:
            validate_source(source)
            names = discover(source, github_api if source["type"] == "github" else other_api)
            if source["repos"] == "all":
                discovered = {name.lower() for name in names}
                for kind, base, owner, name in requested:
                    if _matches_owner(source, kind, base, owner) and name.lower() not in discovered:
                        raise ValueError("selected repository not discovered in all source")
            for name in names:
                target = destination_name(source, name).lower()
                if target in seen:
                    raise ValueError("duplicate destination repository")
                seen.add(target)
    if changed:
        with open(args.config, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print("config changed", file=sys.stderr)
    else:
        print("config unchanged", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        print("add to config failed: " + str(exc), file=sys.stderr)
        sys.exit(1)
