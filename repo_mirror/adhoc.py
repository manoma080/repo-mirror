"""adhoc: turn repository URLs typed into a manual run into sources.

Two uses:
- ``add_to_config``: write URLs into config.json (the "add to config" checkbox).
- ``one_time_sources``: build in-memory sources for URLs that config.json does
  not already cover, so a one-off mirror works without changing the config.

URLs must be exact ``https://host/owner/name`` links. A ``github.com`` host is
treated as a GitHub source; any other host is treated as a Forgejo source.
Errors never echo the input, which may contain credentials.
"""

import json
import os
import sys
from urllib.parse import urlsplit

from repo_mirror.constants import NAME
from repo_mirror.source_filter import entry_urls


def parse_repo_url(url):
    """Return (source_type, base_url, owner, name) for an exact repository URL."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        parts = parsed.path.split("/")
        valid = (
            bool(url)
            and all(ord(c) > 32 and ord(c) != 127 for c in url)
            and parsed.scheme == "https"
            and bool(host)
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
            and not parsed.query
            and not parsed.fragment
            and "?" not in url
            and "#" not in url
            and len(parts) == 3
            and parts[0] == ""
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("invalid source repository URL")
    owner, name = parts[1], parts[2]
    if not NAME.fullmatch(owner) or not NAME.fullmatch(name) or owner in (".", "..") or name in (".", ".."):
        raise ValueError("invalid source repository URL")
    if host == "github.com":
        return "github", "https://github.com", owner, name
    return "forgejo", "https://" + host, owner, name


def _source_for(source_type, base_url, owner, name):
    return {"type": source_type, "base_url": base_url, "owner": owner, "repos": [name]}


def _matches_owner(source, kind, base, owner):
    configured = str(source.get("base_url", "")).rstrip("/").lower()
    if kind == "github" and configured == "https://api.github.com":
        configured = "https://github.com"
    return (source.get("type") == kind and configured == base
            and str(source.get("owner", "")).lower() == owner.lower())


def add_to_config(config, source_urls, destination="", visibility=None, *, protect_pins=True):
    """Add URLs and optional per-repository destination settings."""
    if 'repositories' in config or 'destinations' in config:
        from repo_mirror.config_named import normalize_config
        normalize_config(config)
        import copy
        candidate = copy.deepcopy(config)
        profile = next(iter(candidate['destinations']))
        parsed = [parse_repo_url(part.strip()) for part in source_urls.split(',')]
        identities = [tuple(x.lower() for x in row) for row in parsed]
        if len(set(identities)) != len(identities):
            raise ValueError('duplicate source repository')
        if visibility not in (None, 'private', 'public'):
            raise ValueError('invalid visibility')
        if destination and (len(parsed) != 1 or len(destination) > 100 or not NAME.fullmatch(destination) or destination in ('.', '..')):
            raise ValueError('custom destination requires one URL and a valid repository name')
        for (kind, base, owner, name), identity in zip(parsed, identities):
            entry = next((e for e in candidate['repositories'] if tuple(x.lower() for x in parse_repo_url(e['source']['url'])) == identity), None)
            if entry is None:
                entry = {'source': {'url': base + '/' + owner + '/' + name}, 'mirrors': {profile: {}}}
                candidate['repositories'].append(entry)
            mirror = entry['mirrors'][profile]
            if destination:
                _, _, saved_owner, saved_name = parse_repo_url(entry['source']['url'])
                current_name = mirror.get('name', 'mirror-' + saved_owner + '-' + saved_name)
                if 'id' in mirror and destination != current_name:
                    raise ValueError('cannot rename pinned destination without explicit reconciliation')
                mirror['name'] = destination
            if visibility is not None:
                mirror['visibility'] = visibility
        normalize_config(candidate)
        changed = candidate != config
        if changed:
            config.clear()
            config.update(candidate)
        return changed
    parts = source_urls.split(",")
    parsed = [parse_repo_url(part.strip()) for part in parts]
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
        if destination or visibility is not None:
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
            if old != new:
                mappings[name.lower()] = new
                changed = True
    return changed


def add_to_config_pinned(config, source_urls, github_source, forgejo_source, dst, *, owner, account_type, destination='', visibility=None):
    """Stage additions, verify identities and create missing mirrors before committing config.

    Failed operations can leave initializing repositories remotely. Existing
    unpinned destinations require manual reconciliation, not marker adoption.
    """
    import copy
    from urllib.parse import quote
    from repo_mirror.config_named import normalize_config
    from repo_mirror.constants import GITHUB
    from repo_mirror.repository import GitHubAPI, validate_source_clone_url
    from repo_mirror.sources import (check_existing, destination_name, discover, get_existing,
                                     source_api_base, source_marker, validate_source)

    candidate = copy.deepcopy(config)
    add_to_config(candidate, source_urls, destination, visibility)
    normalized = normalize_config(candidate)
    profile = normalized.get('destination_profile')
    if profile and (profile['url'] != 'https://github.com' or profile['owner'].lower() != owner.lower()):
        raise ValueError('destination profile runtime mismatch')
    if account_type not in ('personal', 'organization') or not isinstance(owner, str) or not NAME.fullmatch(owner):
        raise ValueError('invalid destination account')
    requested = [tuple(x.lower() for x in parse_repo_url(part.strip())) for part in source_urls.split(',')]
    # A separate anonymous client per Forgejo origin binds both direct requests
    # and every redirect, including discovery and subsequent identity rechecks.
    from repo_mirror.github_api import API, SourceAPI
    forgejo_clients = {}
    def source_client(source):
        if source['type'] == 'github':
            return github_source
        if not isinstance(forgejo_source, API):
            return forgejo_source  # injected test/provider implementation
        base = source['base_url']
        if base not in forgejo_clients:
            client = SourceAPI(None)
            client.restrict_origin(base)
            forgejo_clients[base] = client
        return forgejo_clients[base]

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
        upstream = source_api_base(source) + '/repos/' + quote(source['owner'], safe='') + '/' + quote(name, safe='')
        metadata = api.request('GET', upstream)
        validate_source_clone_url(source, name, metadata)
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
            current = api.request('GET', upstream)
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
        current = api.request('GET', upstream)
        if not isinstance(current, dict) or type(current.get('id')) is not int or current['id'] != sid:
            raise ValueError('source repository id changed')
        if 'repositories' in candidate:
            entry = next(e for e in candidate['repositories'] if tuple(x.lower() for x in parse_repo_url(e['source']['url'])) == (source['type'], source['base_url'].lower(), source['owner'].lower(), name.lower()))
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
        current = api.request('GET', upstream)
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
    """Remove explicit entries; refuse exclusions from dynamically discovered owners."""
    if 'repositories' in config or 'destinations' in config:
        from repo_mirror.config_named import normalize_config
        normalize_config(config)
        identities = {tuple(x.lower() for x in parse_repo_url(part.strip())) for part in source_urls.split(',')}
        kept = [entry for entry in config['repositories'] if tuple(x.lower() for x in parse_repo_url(entry['source']['url'])) not in identities]
        changed = len(kept) != len(config['repositories'])
        if changed:
            config['repositories'] = kept
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


def one_time_sources(source_urls, plan, configured_sources=()):
    """Return sources for URLs not already covered by the config-discovered plan.

    Each returned source lists exactly one repository and is never written to config.
    A selected repository under a configured all source must have been discovered.
    """
    if source_urls is None or not source_urls.strip():
        return []
    covered = set()
    for _, source, name in plan:
        covered |= {url.lower() for url in entry_urls(source, name)}
    sources = []
    for part in source_urls.split(","):
        url = part.strip()
        source_type, base_url, owner, name = parse_repo_url(url)
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
    args = parser.parse_args(argv)
    if not args.urls.strip():
        raise ValueError("at least one repository URL is required")
    if args.remove and (args.destination or args.visibility not in ("private", "unchanged")):
        raise ValueError("remove cannot be combined with destination settings")
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
                                       visibility=None if args.visibility == 'unchanged' else args.visibility)
    else:
        changed = (remove_from_config(config, args.urls) if args.remove else
                   add_to_config(config, args.urls, args.destination, None if args.visibility == "unchanged" else args.visibility))
    if not args.remove and not args.auto_pin:
        from repo_mirror.sources import destination_name, validate_source, discover
        from repo_mirror.github_api import API, SourceAPI
        seen = set()
        requested = [parse_repo_url(part.strip()) for part in args.urls.split(",")]
        github_api, other_api = SourceAPI(os.environ.get("GH_TOKEN")), API()
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
