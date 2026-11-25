"""Fail-closed onboarding pass for ``auto_discoveries`` rules on the GitHub runner."""

import copy
from dataclasses import dataclass
import json
import re
import sys
from urllib.error import HTTPError
from urllib.parse import quote

from repo_mirror.adhoc import parse_owner_url, parse_repo_url
from repo_mirror.config_json import unique_pairs
from repo_mirror.config_named import normalize_config
from repo_mirror.constants import GITHUB, NAME
from repo_mirror.github_api import API as GitHubAPI, not_found
from repo_mirror.gitlab_onboarding_github import OnboardingPublicationError
from repo_mirror.logging_utils import error_detail, safe_label
from repo_mirror.repository import validate_source_clone_url
from repo_mirror.sources import check_existing, destination_name, get_existing, source_api_base, source_marker

_GENERATED = re.compile(r'gitlab-[0-9a-f]{24}', re.IGNORECASE)


class PartialOnboarding(OnboardingPublicationError):
    """Destination creation or publication may be partial; manual reconciliation required."""

    def __init__(self, names, message='destination creation may be partial; reconcile manually'):
        self.names = tuple(names)
        super().__init__(message)


@dataclass(frozen=True)
class PlannedInduction:
    name: str
    source_id: int
    clone_url: str
    full_name: str
    dest_name: str
    visibility: str
    ignore_releases: bool
    source_type: str
    base_url: str
    owner: str
    rule_url: str


@dataclass(frozen=True)
class RulePlan:
    index: int
    total: int
    rule: dict
    owner_id: int
    needs_id_pin: bool
    discovered_count: int
    excluded_count: int
    tracked_count: int
    missing_upstream_count: int
    planned: tuple[PlannedInduction, ...]
    orphaned_children: tuple[tuple[str, str, str], ...]
    old_rule_url: str = ''
    old_owner: str = ''
    relocated_to: str | None = None
    preserve_prefix: str | None = None


@dataclass(frozen=True)
class OnboardingResult:
    config_bytes: bytes
    rule_reports: tuple[str, ...]
    orphaned_children: tuple[tuple[str, str, str], ...]
    orphaned_legacy: tuple[tuple[str, str, str], ...]
    inducted_total: int
    relocated_rules: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class _ExistingEntry:
    index: int
    entry: dict
    kind: str
    base: str
    owner: str
    name: str


def _resolve_github_owner(api_base, owner, api):
    data = api.request('GET', f"{api_base}/users/{quote(owner, safe='')}")
    if not isinstance(data, dict):
        raise ValueError('invalid source owner metadata')
    owner_id = data.get('id')
    login = data.get('login')
    account_type = data.get('type')
    if type(owner_id) is not int or owner_id <= 0 or not isinstance(login, str) or account_type not in ('User', 'Organization'):
        raise ValueError('invalid source owner metadata')
    return {
        'id': owner_id,
        'kind': 'organization' if account_type == 'Organization' else 'user',
        'canonical_owner': login,
    }


def _resolve_forgejo_owner(api_base, owner, api):
    kind = 'user'
    try:
        data = api.request('GET', f"{api_base}/users/{quote(owner, safe='')}")
    except HTTPError as exc:
        if not not_found(exc):
            raise
        exc.close()
        kind = 'organization'
        data = api.request('GET', f"{api_base}/orgs/{quote(owner, safe='')}")
    if not isinstance(data, dict):
        raise ValueError('invalid source owner metadata')
    owner_id = data.get('id')
    if type(owner_id) is not int or owner_id <= 0:
        raise ValueError('invalid source owner metadata')
    if kind == 'user':
        present = [data[k] for k in ('login', 'username') if k in data and data[k] is not None]
        if not present or any(not isinstance(v, str) for v in present) or len(set(present)) != 1:
            raise ValueError('invalid source owner metadata')
        canonical = present[0]
    else:
        if 'username' in data and data['username'] is not None:
            canonical = data['username']
        elif 'name' in data and data['name'] is not None:
            canonical = data['name']
        else:
            canonical = data.get('login')
        if not isinstance(canonical, str):
            raise ValueError('invalid source owner metadata')
    return {'id': owner_id, 'kind': kind, 'canonical_owner': canonical}


def _resolve_gitlab_owner(api_base, owner, api):
    try:
        data = api.request('GET', f"{api_base}/groups/{quote(owner, safe='')}")
    except HTTPError as exc:
        if '/' not in owner and not_found(exc):
            exc.close()
            users = api.request('GET', f"{api_base}/users?username={quote(owner, safe='')}")
            if not isinstance(users, list):
                raise ValueError('invalid source owner metadata')
            if not users:
                raise ValueError('source owner identity mismatch')
            if len(users) != 1 or not isinstance(users[0], dict):
                raise ValueError('invalid source owner metadata')
            user = users[0]
            owner_id = user.get('id')
            username = user.get('username')
            if type(owner_id) is not int or owner_id <= 0 or not isinstance(username, str):
                raise ValueError('invalid source owner metadata')
            return {'id': owner_id, 'kind': 'user', 'canonical_owner': username}
        raise
    if not isinstance(data, dict):
        raise ValueError('invalid source owner metadata')
    owner_id = data.get('id')
    if type(owner_id) is not int or owner_id <= 0 or not isinstance(data.get('full_path'), str):
        raise ValueError('invalid source owner metadata')
    if data.get('visibility') != 'public':
        raise ValueError('source owner identity mismatch')
    return {'id': owner_id, 'kind': 'group', 'canonical_owner': data['full_path']}


def resolve_owner(rule, api):
    """Resolve and verify upstream owner metadata and any pinned owner ID."""
    api_base = source_api_base(rule)
    owner = rule['owner']
    kind = rule['type']
    if kind == 'github':
        info = _resolve_github_owner(api_base, owner, api)
    elif kind == 'forgejo':
        info = _resolve_forgejo_owner(api_base, owner, api)
    elif kind == 'gitlab':
        if 'id' not in rule:
            raise ValueError('GitLab auto_discoveries rule requires id')
        info = _resolve_gitlab_owner(api_base, owner, api)
    else:
        raise ValueError('invalid auto_discoveries type')
    if 'id' in rule and info['id'] != rule['id']:
        raise ValueError('source owner identity mismatch')
    canonical = info.pop('canonical_owner')
    changed = canonical.lower() != owner.lower() if kind == 'github' else canonical != owner
    if changed:
        if 'id' not in rule:
            raise ValueError('source owner identity mismatch')
        try:
            _, _, segments = parse_owner_url(f"{rule['base_url']}/{canonical}", source_type=kind)
        except ValueError:
            raise ValueError('invalid source owner metadata') from None
        if '/'.join(segments) != canonical:
            raise ValueError('invalid source owner metadata')
        return {'id': info['id'], 'kind': info['kind'], 'relocated_owner': canonical}
    return info


def _listing_url(rule, owner_info):
    api_base = source_api_base(rule)
    owner = rule['owner']
    quoted_owner = quote(owner, safe='')
    source_type = rule['type']
    if source_type == 'github':
        if owner_info['kind'] == 'organization':
            return f"{api_base}/orgs/{quoted_owner}/repos?type=all"
        return f"{api_base}/users/{quoted_owner}/repos?type=owner"
    if source_type == 'forgejo':
        if owner_info['kind'] == 'organization':
            return f"{api_base}/orgs/{quoted_owner}/repos"
        return f"{api_base}/users/{quoted_owner}/repos"
    if owner_info['kind'] == 'group':
        return f"{api_base}/groups/{quoted_owner}/projects?include_subgroups=false&with_shared=false"
    return f"{api_base}/users/{owner_info['id']}/projects"


def discover_rule_repositories(rule, owner_info, api):
    """Discover and validate all repositories as ``(name, source_id, clone_url, full_name)``."""
    items = api.pages(_listing_url(rule, owner_info))
    if not isinstance(items, list):
        raise ValueError('pagination response is not an array')
    source_stub = {'type': rule['type'], 'base_url': rule['base_url'], 'owner': rule['owner']}
    seen_names = set()
    seen_ids = set()
    records = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('invalid discovered repository metadata')
        if rule['type'] == 'gitlab':
            visibility = item.get('visibility')
            if visibility in ('private', 'internal'):
                continue
            if visibility != 'public':
                raise ValueError('invalid discovered repository metadata')
            name = item.get('path')
            source_id = item.get('id')
            full_name = item.get('path_with_namespace')
            clone_url = item.get('http_url_to_repo')
        else:
            name = item.get('name')
            source_id = item.get('id')
            full_name = item.get('full_name')
            clone_url = item.get('clone_url')
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in ('.', '..'):
            raise ValueError('invalid discovered repository name')
        if type(source_id) is not int or source_id <= 0 or not isinstance(full_name, str):
            raise ValueError('invalid discovered repository metadata')
        expected_full_name = f"{rule['owner']}/{name}"
        canonical = (
            full_name.lower() == expected_full_name.lower()
            if rule['type'] == 'github'
            else full_name == expected_full_name
        )
        if not canonical:
            raise ValueError('invalid discovered repository metadata')
        validate_source_clone_url(source_stub, name, {'clone_url': clone_url})
        if name.lower() in seen_names or source_id in seen_ids:
            raise ValueError('duplicate discovered repository')
        seen_names.add(name.lower())
        seen_ids.add(source_id)
        records.append((name, source_id, clone_url, full_name))
    records.sort(key=lambda record: (record[0].lower(), record[0]))
    return records


def _parse_existing_entries(repositories, *, rewrite_rule_url=None, rewrite_owner=None):
    parsed = []
    for idx, entry in enumerate(repositories):
        kind, base, owner, name = parse_repo_url(
            entry['source']['url'],
            source_type=entry['source'].get('type'),
        )
        if rewrite_rule_url is not None and entry.get('discovered_by') == rewrite_rule_url:
            owner = rewrite_owner
        parsed.append(_ExistingEntry(idx, entry, kind, base, owner, name))
    return parsed


def _configured_child_dest_name(rule, child, profile):
    mirror = child['mirrors'][profile]
    if 'name' in mirror:
        return mirror['name']
    _, _, _, child_name = parse_repo_url(child['source']['url'], source_type=child['source'].get('type'))
    ov = next((v for k, v in rule['overrides'].items() if k.lower() == child_name.lower()), None)
    if ov and 'name' in ov:
        return ov['name']
    return f"{rule['destination_name_prefix']}-{child_name}"


def _plan_rule(rule_index, total_rules, rule, raw_rule, profile, repositories, api):
    owner_info = resolve_owner(rule, api)
    needs_id_pin = 'id' not in rule
    relocated_to = owner_info.get('relocated_owner')
    old_rule_url = rule['url']
    old_owner = rule['owner']
    rule_children = [entry for entry in repositories if entry.get('discovered_by') == old_rule_url]
    preserve_prefix = None
    if relocated_to is not None:
        new_url = f"{rule['base_url']}/{relocated_to}"
        new_default_prefix = 'mirror-' + relocated_to.replace('/', '-')
        effective_prefix = rule['destination_name_prefix']
        if 'destination_name_prefix' not in raw_rule:
            needs_preserve = False
            for child in rule_children:
                mirror = child['mirrors'][profile]
                if 'name' not in mirror:
                    continue
                _, _, _, child_name = parse_repo_url(child['source']['url'], source_type=child['source'].get('type'))
                ov = next((v for k, v in rule['overrides'].items() if k.lower() == child_name.lower()), None)
                if ov and 'name' in ov:
                    continue
                if rule['type'] == 'gitlab' and _GENERATED.fullmatch(mirror['name']):
                    continue
                if mirror['name'] != f"{new_default_prefix}-{child_name}":
                    needs_preserve = True
                    break
            if needs_preserve:
                preserve_prefix = rule['destination_name_prefix']
                effective_prefix = rule['destination_name_prefix']
            else:
                effective_prefix = new_default_prefix
        effective_rule = {**rule, 'url': new_url, 'owner': relocated_to, 'destination_name_prefix': effective_prefix}
    else:
        effective_rule = rule

    discovered = discover_rule_repositories(effective_rule, owner_info, api)
    if not discovered and rule_children:
        raise ValueError('auto-discovery returned zero repositories for a rule with existing entries')

    parsed_existing = _parse_existing_entries(
        repositories,
        rewrite_rule_url=old_rule_url if relocated_to is not None else None,
        rewrite_owner=relocated_to,
    )
    exclude_lower = {name.lower() for name in effective_rule['exclude']}
    excluded_count = 0
    tracked_count = 0
    planned = []

    for name, source_id, clone_url, full_name in discovered:
        if name.lower() in exclude_lower:
            excluded_count += 1
            continue
        id_matches = [
            rec for rec in parsed_existing
            if rec.kind == effective_rule['type']
            and rec.base.lower() == effective_rule['base_url'].lower()
            and rec.entry['source'].get('id') == source_id
        ]
        if len(id_matches) > 1:
            raise ValueError('existing source mapping conflicts')
        url_identity = (effective_rule['base_url'].lower(), effective_rule['owner'].lower(), name.lower())
        if len(id_matches) == 1:
            matched = id_matches[0]
            other_url_matches = [
                rec for rec in parsed_existing
                if rec.index != matched.index
                and (rec.base.lower(), rec.owner.lower(), rec.name.lower()) == url_identity
            ]
            if other_url_matches:
                raise ValueError('existing source mapping conflicts')
            if 'discovered_by' in matched.entry and matched.entry['discovered_by'] != old_rule_url:
                raise ValueError('existing source mapping conflicts')
            if 'discovered_by' in matched.entry:
                mirror = matched.entry['mirrors'][profile]
                if type(mirror.get('id')) is not int or mirror['id'] <= 0:
                    raise ValueError('existing source mapping conflicts')
            tracked_count += 1
            continue

        url_matches = [
            rec for rec in parsed_existing
            if (rec.base.lower(), rec.owner.lower(), rec.name.lower()) == url_identity
        ]
        if url_matches:
            raise ValueError('source repository id mismatch or missing')

        ov = next((v for k, v in effective_rule['overrides'].items() if k.lower() == name.lower()), None)
        dest_name = ov['name'] if (ov and 'name' in ov) else f"{effective_rule['destination_name_prefix']}-{name}"
        if (
            not isinstance(dest_name, str)
            or not (1 <= len(dest_name) <= 100)
            or not NAME.fullmatch(dest_name)
            or dest_name in ('.', '..')
            or _GENERATED.fullmatch(dest_name)
        ):
            raise ValueError('invalid destination repository name')
        visibility = ov['visibility'] if (ov and 'visibility' in ov) else effective_rule['visibility']
        ignore_releases = ov['ignore_releases'] if (ov and 'ignore_releases' in ov) else effective_rule['ignore_releases']
        planned.append(
            PlannedInduction(
                name=name,
                source_id=source_id,
                clone_url=clone_url,
                full_name=full_name,
                dest_name=dest_name,
                visibility=visibility,
                ignore_releases=ignore_releases,
                source_type=effective_rule['type'],
                base_url=effective_rule['base_url'],
                owner=effective_rule['owner'],
                rule_url=effective_rule['url'],
            )
        )

    raw_discovered_ids = {source_id for _, source_id, _, _ in discovered}
    orphaned_children = []
    for child in rule_children:
        if child['source'].get('id') not in raw_discovered_ids:
            _, _, c_owner, c_name = parse_repo_url(
                child['source']['url'],
                source_type=child['source'].get('type'),
            )
            if relocated_to is not None:
                c_owner = relocated_to
            orphaned_children.append((c_owner, c_name, _configured_child_dest_name(effective_rule, child, profile)))

    return RulePlan(
        index=rule_index,
        total=total_rules,
        rule=effective_rule,
        owner_id=owner_info['id'],
        needs_id_pin=needs_id_pin,
        discovered_count=len(discovered),
        excluded_count=excluded_count,
        tracked_count=tracked_count,
        missing_upstream_count=len(orphaned_children),
        planned=tuple(planned),
        orphaned_children=tuple(orphaned_children),
        old_rule_url=old_rule_url,
        old_owner=old_owner,
        relocated_to=relocated_to,
        preserve_prefix=preserve_prefix,
    )


def _build_repository_entry(item, profile, dest_id):
    source_obj = {'url': f'{item.base_url}/{item.owner}/{item.name}', 'id': item.source_id}
    if item.source_type == 'gitlab' and item.base_url.lower() != 'https://gitlab.com':
        source_obj['type'] = 'gitlab'
    mirror_obj = {
        'name': item.dest_name,
        'id': dest_id,
        'visibility': item.visibility,
    }
    if item.ignore_releases:
        mirror_obj['ignore_releases'] = True
    return {
        'source': source_obj,
        'mirrors': {profile: mirror_obj},
        'discovered_by': item.rule_url,
    }


def _collect_existing_destination_names(raw_config, profile, rules_by_url):
    names = set()
    for entry in raw_config['repositories']:
        mirror = entry['mirrors'][profile]
        if 'name' in mirror:
            names.add(mirror['name'].lower())
        elif 'discovered_by' in entry:
            rule = rules_by_url[entry['discovered_by']]
            names.add(_configured_child_dest_name(rule, entry, profile).lower())
        else:
            kind, base, owner, name = parse_repo_url(
                entry['source']['url'],
                source_type=entry['source'].get('type'),
            )
            names.add(destination_name({'type': kind, 'base_url': base, 'owner': owner, 'repos': [name]}, name).lower())
        legacy = mirror.get('legacy')
        if isinstance(legacy, dict) and isinstance(legacy.get('destination_name'), str):
            names.add(legacy['destination_name'].lower())
    return names


def _log_rule_failure(rule_plan, exc, stream):
    print(
        f"auto_discovery {rule_plan.index}/{rule_plan.total} "
        f"({safe_label(rule_plan.rule['owner'])}): FAILED: {error_detail(exc)}",
        file=stream,
        flush=True,
    )


def _prevalidate_plans(raw_config, profile, plans, dst, owner, stream):
    rules_by_url = {plan.rule['url']: plan.rule for plan in plans}
    planned_dests = set()
    projected = copy.deepcopy(raw_config)
    reloc_by_old_url = {}
    for rule_plan in plans:
        if rule_plan.needs_id_pin:
            projected['auto_discoveries'][rule_plan.index - 1]['id'] = rule_plan.owner_id
        if rule_plan.relocated_to is not None:
            raw_rule = projected['auto_discoveries'][rule_plan.index - 1]
            raw_rule['url'] = rule_plan.rule['url']
            if rule_plan.preserve_prefix is not None:
                raw_rule['destination_name_prefix'] = rule_plan.preserve_prefix
            reloc_by_old_url[rule_plan.old_rule_url] = rule_plan
    for entry in projected['repositories']:
        rule_plan = reloc_by_old_url.get(entry.get('discovered_by'))
        if rule_plan is not None:
            _, _, _, child_name = parse_repo_url(entry['source']['url'], source_type=entry['source'].get('type'))
            entry['source']['url'] = f"{rule_plan.rule['base_url']}/{rule_plan.relocated_to}/{child_name}"
            entry['discovered_by'] = rule_plan.rule['url']
    existing_dests = _collect_existing_destination_names(projected, profile, rules_by_url)
    for rule_plan in plans:
        for item in rule_plan.planned:
            key = item.dest_name.lower()
            if key in existing_dests or key in planned_dests:
                exc = ValueError('duplicate destination repository')
                _log_rule_failure(rule_plan, exc, stream)
                raise exc
            planned_dests.add(key)
            projected['repositories'].append(_build_repository_entry(item, profile, 1))
    try:
        normalize_config(projected)
    except Exception as exc:
        if plans:
            target_plan = next((p for p in plans if p.relocated_to is not None), plans[0])
            _log_rule_failure(target_plan, exc, stream)
        raise

    for rule_plan in plans:
        for item in rule_plan.planned:
            target = f"{GITHUB}/repos/{quote(owner, safe='')}/{quote(item.dest_name, safe='')}"
            try:
                existing = get_existing(dst, target)
            except Exception as exc:
                _log_rule_failure(rule_plan, exc, stream)
                raise
            if existing is not None:
                exc = ValueError('unpinned existing destination requires manual reconciliation')
                _log_rule_failure(rule_plan, exc, stream)
                raise exc


def _create_destination(item, dst, owner, account_type):
    source_stub = {'type': item.source_type, 'base_url': item.base_url, 'owner': item.owner}
    marker = source_marker(source_stub, item.name)
    private = item.visibility != 'public'
    payload = {
        'name': item.dest_name,
        'private': private,
        'auto_init': False,
        'description': marker + ' [repo-mirror:initializing:v1]',
    }
    if account_type == 'personal' and isinstance(dst, GitHubAPI):
        created = dst.create_personal_repository(owner, payload)
    else:
        if account_type == 'personal':
            identity = dst.request('GET', GITHUB + '/user')
            if not isinstance(identity, dict) or str(identity.get('login', '')).lower() != owner.lower():
                raise RuntimeError('authenticated GitHub user does not match configured destination')
        endpoint = '/user/repos' if account_type == 'personal' else '/orgs/' + quote(owner, safe='') + '/repos'
        created = dst.request('POST', GITHUB + endpoint, payload)
    if not isinstance(created, dict) or str(created.get('full_name', '')).lower() != (owner + '/' + item.dest_name).lower():
        raise RuntimeError('created repository owner does not match configured destination')
    created_id = created.get('id')
    if type(created_id) is not int or created_id <= 0:
        raise ValueError('invalid created destination repository id')
    if 'private' in created and created['private'] is not private:
        raise ValueError('created destination repository identity not verified')
    check_existing(created, marker)

    target = f"{GITHUB}/repos/{quote(owner, safe='')}/{quote(item.dest_name, safe='')}"
    dst.request('PUT', target + '/actions/permissions', {'enabled': False})
    permissions = dst.request('GET', target + '/actions/permissions')
    if not isinstance(permissions, dict) or permissions.get('enabled') is not False:
        raise ValueError('destination Actions not verified disabled')

    readback = get_existing(dst, target)
    if (
        not isinstance(readback, dict)
        or type(readback.get('id')) is not int
        or readback['id'] != created_id
        or str(readback.get('full_name', '')).lower() != (owner + '/' + item.dest_name).lower()
        or ('private' in readback and readback['private'] is not private)
    ):
        raise ValueError('created destination repository identity not verified')
    check_existing(readback, marker)
    return created_id


def _publish_rule(store, config_bytes, rule_plan, created_entries, profile):
    current, sha = store.read()
    if current != config_bytes:
        raise ValueError('authoritative configuration differs from checkout')
    doc = json.loads(config_bytes, object_pairs_hook=unique_pairs)
    raw_rule = doc['auto_discoveries'][rule_plan.index - 1]
    if rule_plan.needs_id_pin:
        if 'id' in raw_rule:
            raise ValueError('authoritative configuration differs from checkout')
        raw_rule['id'] = rule_plan.owner_id
    elif raw_rule.get('id') != rule_plan.owner_id:
        raise ValueError('source owner identity mismatch')
    if rule_plan.relocated_to is not None:
        raw_rule['url'] = rule_plan.rule['url']
        if rule_plan.preserve_prefix is not None:
            raw_rule['destination_name_prefix'] = rule_plan.preserve_prefix
        for entry in doc['repositories']:
            if entry.get('discovered_by') == rule_plan.old_rule_url:
                _, _, _, child_name = parse_repo_url(entry['source']['url'], source_type=entry['source'].get('type'))
                entry['source']['url'] = f"{rule_plan.rule['base_url']}/{rule_plan.relocated_to}/{child_name}"
                entry['discovered_by'] = rule_plan.rule['url']
    doc['repositories'].extend(created_entries)
    normalize_config(doc)
    after = (json.dumps(doc, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    store.publish(sha, after)
    observed, _ = store.read()
    if observed != after:
        raise ValueError('configuration readback mismatch')
    return after


def _find_orphaned_legacy(raw_config, profile):
    orphans = []
    for entry in raw_config['repositories']:
        if 'discovered_by' in entry:
            continue
        mirror_name = entry['mirrors'][profile].get('name', '')
        if not isinstance(mirror_name, str) or not _GENERATED.fullmatch(mirror_name):
            continue
        kind, _, owner, name = parse_repo_url(
            entry['source']['url'],
            source_type=entry['source'].get('type'),
        )
        if kind == 'gitlab':
            orphans.append((owner, name, mirror_name))
    return tuple(orphans)


def onboard_auto_discoveries(config_bytes, store, source_client, dst, *, owner, account_type, stream=None):
    """Run fail-closed discovery, destination creation, and per-rule CAS publication."""
    stream = sys.stderr if stream is None else stream
    current, _ = store.read()
    if current != config_bytes:
        raise ValueError('authoritative configuration differs from checkout')
    raw_config = json.loads(config_bytes, object_pairs_hook=unique_pairs)
    normalized = normalize_config(raw_config)
    rules = normalized.get('auto_discoveries', [])
    profile = next(iter(raw_config['destinations']))

    plans = []
    for index, rule in enumerate(rules, 1):
        raw_rule = raw_config['auto_discoveries'][index - 1]
        try:
            plans.append(
                _plan_rule(
                    index,
                    len(rules),
                    rule,
                    raw_rule,
                    profile,
                    raw_config['repositories'],
                    source_client(rule),
                )
            )
        except Exception as exc:
            print(
                f"auto_discovery {index}/{len(rules)} ({safe_label(rule['owner'])}): FAILED: {error_detail(exc)}",
                file=stream,
                flush=True,
            )
            raise

    _prevalidate_plans(raw_config, profile, plans, dst, owner, stream)

    any_writes = False
    inducted_total = 0
    all_orphaned_children = []
    rule_reports = []
    relocated_rules = []
    for rule_plan in plans:
        if rule_plan.planned or rule_plan.needs_id_pin or rule_plan.relocated_to is not None:
            if store.read()[0] != config_bytes:
                exc = ValueError('authoritative configuration differs from checkout')
                _log_rule_failure(rule_plan, exc, stream)
                raise exc
            attempted = []
            created_entries = []
            try:
                for item in rule_plan.planned:
                    attempted.append(item.dest_name)
                    dest_id = _create_destination(item, dst, owner, account_type)
                    created_entries.append(_build_repository_entry(item, profile, dest_id))
                config_bytes = _publish_rule(store, config_bytes, rule_plan, created_entries, profile)
            except Exception as exc:
                if attempted:
                    wrapped = PartialOnboarding(attempted)
                else:
                    wrapped = OnboardingPublicationError('auto-discovery onboarding publication failed; reconcile manually')
                _log_rule_failure(rule_plan, wrapped, stream)
                raise wrapped from exc
            any_writes = True
            inducted_total += len(created_entries)

        if rule_plan.relocated_to is not None:
            reloc_line = (
                f"auto_discovery {rule_plan.index}/{rule_plan.total} "
                f"({safe_label(rule_plan.old_owner)}): relocated to {safe_label(rule_plan.relocated_to)}"
            )
            print(reloc_line, file=stream, flush=True)
            rule_reports.append(reloc_line)
            relocated_rules.append((rule_plan.old_rule_url, rule_plan.rule['url']))

        line = (
            f"auto_discovery {rule_plan.index}/{rule_plan.total} "
            f"({safe_label(rule_plan.rule['owner'])}): OK "
            f"discovered={rule_plan.discovered_count} "
            f"inducted={len(rule_plan.planned)} "
            f"excluded={rule_plan.excluded_count} "
            f"tracked={rule_plan.tracked_count}"
        )
        if rule_plan.missing_upstream_count > 0:
            line += f" missing_upstream={rule_plan.missing_upstream_count}"
        print(line, file=stream, flush=True)
        rule_reports.append(line)
        all_orphaned_children.extend(rule_plan.orphaned_children)

    if not any_writes:
        observed, _ = store.read()
        if observed != config_bytes:
            raise ValueError('authoritative configuration differs from checkout')

    final_config = json.loads(config_bytes, object_pairs_hook=unique_pairs)
    orphaned_legacy = _find_orphaned_legacy(final_config, profile)
    return OnboardingResult(
        config_bytes=config_bytes,
        rule_reports=tuple(rule_reports),
        orphaned_children=tuple(all_orphaned_children),
        orphaned_legacy=orphaned_legacy,
        inducted_total=inducted_total,
        relocated_rules=tuple(relocated_rules),
    )


def report_orphans(result, stream=None):
    """Emit summary diagnostic lines for orphaned children and legacy entries."""
    stream = sys.stderr if stream is None else stream
    for owner, name, dest in result.orphaned_children:
        print(
            f"orphaned child repository {safe_label(owner)}/{safe_label(name)}: destination {safe_label(dest)} untouched",
            file=stream,
            flush=True,
        )
    for owner, name, dest in result.orphaned_legacy:
        print(
            f"orphaned legacy repository {safe_label(owner)}/{safe_label(name)}: destination {safe_label(dest)} untouched",
            file=stream,
            flush=True,
        )
