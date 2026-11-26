"""Fail-closed onboarding pass for ``auto_discoveries`` rules."""

import copy
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
import json
import re
import sys
from urllib.error import HTTPError
from urllib.parse import quote

from repo_mirror.adhoc import parse_owner_url, parse_repo_url
from repo_mirror.config_json import unique_pairs
from repo_mirror.config_named import _normalize_auto_discoveries, normalize_config
from repo_mirror.constants import GITHUB, NAME
from repo_mirror.forgejo_git import APIError, ForgejoDestination, LocalForgejoAPI, _name, _source_marker
from repo_mirror.forgejo_sync import parse_manifest, parse_source
from repo_mirror.git_sync import archive_name
from repo_mirror.github_api import API as GitHubAPI, not_found
from repo_mirror.gitlab_onboarding_forgejo import (
    gitlab_generated_name,
    group_origin_key,
    translate_groups,
)
from repo_mirror.host_limits import CONFIG_KEY
from repo_mirror.logging_utils import error_detail, safe_label
from repo_mirror.migrations import blob_sha
from repo_mirror.repository import validate_source_clone_url
from repo_mirror.sources import check_existing, destination_name, get_existing, source_api_base, source_marker

_GENERATED = re.compile(r'gitlab-[0-9a-f]{24}', re.IGNORECASE)


class OnboardingPublicationError(RuntimeError):
    """Publication failed or its result is uncertain; reconciliation is required."""


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
class IdentityConflict:
    source_type: str
    base_url: str
    owner: str
    name: str
    pinned_id: int
    discovered_id: int
    reason: str | None = None


@dataclass(frozen=True)
class PlannedAdoption:
    item: PlannedInduction
    old_source_id: int
    old_source_url: str
    old_dest_id: int


def conflict_identity_keys(conflicts):
    """Return normalized identity keys for conflicted entries skipped from mirroring."""
    return frozenset(
        (c.source_type, group_origin_key(c.base_url), c.owner.lower(), c.name.lower(), c.pinned_id)
        for c in conflicts
    )


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
    conflicts: tuple[IdentityConflict, ...] = ()
    adoptions: tuple[PlannedAdoption, ...] = ()
    old_rule_url: str = ''
    old_owner: str = ''
    relocated_to: str | None = None
    preserve_prefix: str | None = None
    raw_index: int | None = None
    from_groups: bool = False
    active_bindings: tuple[tuple[str, int, str], ...] = ()


@dataclass(frozen=True)
class OnboardingResult:
    config_bytes: bytes
    rule_reports: tuple[str, ...]
    orphaned_children: tuple[tuple[str, str, str], ...]
    orphaned_legacy: tuple[tuple[str, str, str], ...]
    inducted_total: int
    conflicts: tuple[IdentityConflict, ...] = ()
    adopted_total: int = 0
    relocated_rules: tuple[tuple[str, str], ...] = ()
    active_group_bindings: frozenset[tuple[str, int, str]] = frozenset()


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


def _resolve_gitlab_owner(api_base, owner, api, *, group_only=False):
    try:
        data = api.request('GET', f"{api_base}/groups/{quote(owner, safe='')}")
    except HTTPError as exc:
        if not group_only and '/' not in owner and not_found(exc):
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
    from_groups = bool(rule.get('_from_groups'))
    if kind == 'github':
        info = _resolve_github_owner(api_base, owner, api)
    elif kind == 'forgejo':
        info = _resolve_forgejo_owner(api_base, owner, api)
    elif kind == 'gitlab':
        if 'id' not in rule:
            raise ValueError('GitLab auto_discoveries rule requires id')
        info = _resolve_gitlab_owner(api_base, owner, api, group_only=from_groups)
    else:
        raise ValueError('invalid auto_discoveries type')
    if from_groups and info['kind'] != 'group':
        raise ValueError('source owner identity mismatch')
    if 'id' in rule and info['id'] != rule['id']:
        raise ValueError('source owner identity mismatch')
    canonical = info.pop('canonical_owner')
    changed = canonical.lower() != owner.lower() if kind == 'github' else canonical != owner
    if changed:
        if from_groups or 'id' not in rule:
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


def _discover_rule_repositories_detailed(rule, owner_info, api):
    """Discover and validate repositories as ``(name, source_id, clone_url, full_name, source_is_public)``."""
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
            source_is_public = True
        else:
            name = item.get('name')
            source_id = item.get('id')
            full_name = item.get('full_name')
            clone_url = item.get('clone_url')
            source_is_public = item.get('private') is False
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
        records.append((name, source_id, clone_url, full_name, source_is_public))
    records.sort(key=lambda record: (record[0].lower(), record[0]))
    return records


def discover_rule_repositories(rule, owner_info, api):
    """Discover and validate all repositories as ``(name, source_id, clone_url, full_name)``."""
    return [record[:4] for record in _discover_rule_repositories_detailed(rule, owner_info, api)]


def _parse_existing_entries(repositories, *, rewrite_rule_url=None, rewrite_owner=None, allow_forgejo_port=False):
    parsed = []
    for idx, entry in enumerate(repositories):
        kind, base, owner, name = parse_repo_url(
            entry['source']['url'],
            source_type=entry['source'].get('type'),
            allow_forgejo_port=allow_forgejo_port,
        )
        if rewrite_rule_url is not None and entry.get('discovered_by') == rewrite_rule_url:
            owner = rewrite_owner
        parsed.append(_ExistingEntry(idx, entry, kind, base, owner, name))
    return parsed


def _configured_child_dest_name(rule, child, profile, *, allow_forgejo_port=False):
    mirror = child['mirrors'][profile]
    if 'name' in mirror:
        return mirror['name']
    _, _, _, child_name = parse_repo_url(
        child['source']['url'],
        source_type=child['source'].get('type'),
        allow_forgejo_port=allow_forgejo_port,
    )
    ov = next((v for k, v in rule['overrides'].items() if k.lower() == child_name.lower()), None)
    if ov and 'name' in ov:
        return ov['name']
    return f"{rule['destination_name_prefix']}-{child_name}"


def _utc_timestamp():
    return datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')


def _confirm_github_source_deleted(rule, pinned_id, api):
    try:
        data = api.request('GET', f"{source_api_base(rule)}/repositories/{pinned_id}")
    except HTTPError as exc:
        is_missing = not_found(exc)
        exc.close()
        if is_missing:
            return 'deleted', None
        return 'error', 'could not confirm upstream repository deletion'
    except Exception:
        return 'error', 'could not confirm upstream repository deletion'
    if isinstance(data, dict):
        return 'alive', None
    return 'error', 'could not confirm upstream repository deletion'


def _build_planned_induction(effective_rule, name, source_id, clone_url, full_name, source_is_public, runner):
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
    requested_visibility = ov['visibility'] if (ov and 'visibility' in ov) else effective_rule['visibility']
    if runner == 'forgejo':
        visibility = 'public' if (requested_visibility == 'public' and source_is_public) else 'private'
    else:
        visibility = requested_visibility
    ignore_releases = ov['ignore_releases'] if (ov and 'ignore_releases' in ov) else effective_rule['ignore_releases']
    return PlannedInduction(
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


def _plan_rule(rule_index, total_rules, rule, raw_rule, profile, repositories, api, *, runner='github', raw_index=None):
    allow_forgejo_port = runner == 'forgejo'
    from_groups = bool(rule.get('_from_groups'))
    owner_info = resolve_owner(rule, api)
    needs_id_pin = (not from_groups) and ('id' not in rule)
    relocated_to = owner_info.get('relocated_owner')
    old_rule_url = rule['url']
    old_owner = rule['owner']
    rule_children = [] if from_groups else [entry for entry in repositories if entry.get('discovered_by') == old_rule_url]
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
                _, _, _, child_name = parse_repo_url(
                    child['source']['url'],
                    source_type=child['source'].get('type'),
                    allow_forgejo_port=allow_forgejo_port,
                )
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

    discovered = _discover_rule_repositories_detailed(effective_rule, owner_info, api)
    if not discovered and rule_children:
        raise ValueError('auto-discovery returned zero repositories for a rule with existing entries')

    parsed_existing = _parse_existing_entries(
        repositories,
        rewrite_rule_url=old_rule_url if relocated_to is not None else None,
        rewrite_owner=relocated_to,
        allow_forgejo_port=allow_forgejo_port,
    )
    exclude_lower = {name.lower() for name in effective_rule['exclude']}
    excluded_count = 0
    tracked_count = 0
    planned = []
    conflicts = []
    adoptions = []
    handled_entry_ids = set()
    active_bindings = []

    for name, source_id, clone_url, full_name, source_is_public in discovered:
        if name.lower() in exclude_lower:
            excluded_count += 1
            continue
        id_matches = [
            rec for rec in parsed_existing
            if rec.kind == effective_rule['type']
            and group_origin_key(rec.base) == group_origin_key(effective_rule['base_url'])
            and rec.entry['source'].get('id') == source_id
        ]
        if len(id_matches) > 1:
            raise ValueError('existing source mapping conflicts')
        url_identity = (group_origin_key(effective_rule['base_url']), effective_rule['owner'].lower(), name.lower())
        if len(id_matches) == 1:
            matched = id_matches[0]
            other_url_matches = [
                rec for rec in parsed_existing
                if rec.index != matched.index
                and (group_origin_key(rec.base), rec.owner.lower(), rec.name.lower()) == url_identity
            ]
            if other_url_matches:
                raise ValueError('existing source mapping conflicts')
            if 'discovered_by' in matched.entry and matched.entry['discovered_by'] != old_rule_url:
                raise ValueError('existing source mapping conflicts')
            if from_groups or 'discovered_by' in matched.entry:
                mirror = matched.entry['mirrors'][profile]
                if type(mirror.get('id')) is not int or mirror['id'] <= 0:
                    raise ValueError('existing source mapping conflicts')
            if from_groups:
                mirror = matched.entry['mirrors'][profile]
                active_bindings.append((matched.entry['source']['url'], source_id, mirror.get('name', '')))
            tracked_count += 1
            continue

        url_matches = [
            rec for rec in parsed_existing
            if (group_origin_key(rec.base), rec.owner.lower(), rec.name.lower()) == url_identity
        ]
        if url_matches:
            if len(url_matches) > 1:
                raise ValueError('existing source mapping conflicts')
            matched = url_matches[0]
            if matched.kind != effective_rule['type']:
                raise ValueError('existing source mapping conflicts')
            if 'discovered_by' in matched.entry and matched.entry['discovered_by'] != old_rule_url:
                raise ValueError('existing source mapping conflicts')
            pinned_id = matched.entry['source'].get('id')
            if type(pinned_id) is not int or pinned_id <= 0:
                raise ValueError('source repository id mismatch or missing')

            can_adopt = (
                runner == 'github'
                and effective_rule['type'] == 'github'
                and not from_groups
                and 'discovered_by' in matched.entry
            )
            if not can_adopt:
                conflicts.append(
                    IdentityConflict(
                        source_type=effective_rule['type'],
                        base_url=effective_rule['base_url'],
                        owner=effective_rule['owner'],
                        name=name,
                        pinned_id=pinned_id,
                        discovered_id=source_id,
                    )
                )
                handled_entry_ids.add(id(matched.entry))
                continue

            status, reason = _confirm_github_source_deleted(effective_rule, pinned_id, api)
            if status != 'deleted':
                conflicts.append(
                    IdentityConflict(
                        source_type=effective_rule['type'],
                        base_url=effective_rule['base_url'],
                        owner=effective_rule['owner'],
                        name=name,
                        pinned_id=pinned_id,
                        discovered_id=source_id,
                        reason=None if status == 'alive' else reason,
                    )
                )
                handled_entry_ids.add(id(matched.entry))
                continue

            derived_name = f"{effective_rule['destination_name_prefix']}-{name}"
            ov = next((v for k, v in effective_rule['overrides'].items() if k.lower() == name.lower()), None)
            mirror = matched.entry['mirrors'][profile]
            configured_name = mirror.get('name')
            if (ov is not None and 'name' in ov) or (configured_name is not None and configured_name != derived_name):
                conflicts.append(
                    IdentityConflict(
                        source_type=effective_rule['type'],
                        base_url=effective_rule['base_url'],
                        owner=effective_rule['owner'],
                        name=name,
                        pinned_id=pinned_id,
                        discovered_id=source_id,
                        reason='cannot adopt a repository with a configured destination name',
                    )
                )
                handled_entry_ids.add(id(matched.entry))
                continue

            old_dest_id = mirror.get('id')
            if type(old_dest_id) is not int or old_dest_id <= 0:
                conflicts.append(
                    IdentityConflict(
                        source_type=effective_rule['type'],
                        base_url=effective_rule['base_url'],
                        owner=effective_rule['owner'],
                        name=name,
                        pinned_id=pinned_id,
                        discovered_id=source_id,
                        reason='cannot adopt a repository without a pinned destination id',
                    )
                )
                handled_entry_ids.add(id(matched.entry))
                continue

            item = _build_planned_induction(
                effective_rule, name, source_id, clone_url, full_name, source_is_public, runner
            )
            adoptions.append(
                PlannedAdoption(
                    item=item,
                    old_source_id=pinned_id,
                    old_source_url=matched.entry['source']['url'],
                    old_dest_id=old_dest_id,
                )
            )
            handled_entry_ids.add(id(matched.entry))
            continue

        if from_groups:
            dest_name = gitlab_generated_name(effective_rule['base_url'], owner_info['id'], source_id)
            visibility = 'private'
            ignore_releases = False
            active_bindings.append((f"{effective_rule['base_url']}/{effective_rule['owner']}/{name}", source_id, dest_name))
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
        else:
            planned.append(
                _build_planned_induction(
                    effective_rule, name, source_id, clone_url, full_name, source_is_public, runner
                )
            )

    raw_discovered_ids = {source_id for _, source_id, _, _, _ in discovered}
    orphaned_children = []
    for child in rule_children:
        if id(child) in handled_entry_ids:
            continue
        if child['source'].get('id') not in raw_discovered_ids:
            _, _, c_owner, c_name = parse_repo_url(
                child['source']['url'],
                source_type=child['source'].get('type'),
                allow_forgejo_port=allow_forgejo_port,
            )
            if relocated_to is not None:
                c_owner = relocated_to
            orphaned_children.append(
                (c_owner, c_name, _configured_child_dest_name(effective_rule, child, profile, allow_forgejo_port=allow_forgejo_port))
            )

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
        conflicts=tuple(conflicts),
        adoptions=tuple(adoptions),
        old_rule_url=old_rule_url,
        old_owner=old_owner,
        relocated_to=relocated_to,
        preserve_prefix=preserve_prefix,
        raw_index=raw_index,
        from_groups=from_groups,
        active_bindings=tuple(active_bindings),
    )


class CASConfigStore:
    """Adapt a byte-level CAS (``read()`` / ``replace(before, after)``) to ``store``."""

    def __init__(self, cas):
        self.cas = cas

    def read(self):
        data = self.cas.read()
        if not isinstance(data, bytes):
            raise ValueError('invalid configuration bytes')
        return data, blob_sha(data)

    def publish(self, expected_sha, replacement):
        current, sha = self.read()
        if sha != expected_sha:
            raise ValueError('authoritative configuration differs from checkout')
        self.cas.replace(current, replacement)


class GitHubDestinationOnboardingAdapter:
    """Destination adapter for GitHub onboarding (byte-identical to original GitHub flow)."""

    def __init__(self, dst, owner, account_type):
        self.dst = dst
        self.owner = owner
        self.account_type = account_type
        self._created_state = {}

    def marker_for(self, item):
        source_stub = {'type': item.source_type, 'base_url': item.base_url, 'owner': item.owner}
        return source_marker(source_stub, item.name)

    def _repo_target(self, dest_name):
        return f"{GITHUB}/repos/{quote(self.owner, safe='')}/{quote(dest_name, safe='')}"

    def owner_identity(self):
        if self.account_type != 'personal':
            return self.owner
        identity = self.dst.request('GET', GITHUB + '/user')
        if not isinstance(identity, dict) or str(identity.get('login', '')).lower() != self.owner.lower():
            raise RuntimeError('authenticated GitHub user does not match configured destination')
        return identity['login']

    def get_repository(self, dest_name):
        return get_existing(self.dst, self._repo_target(dest_name))

    def create_repository(self, dest_name, private, marker):
        payload = {
            'name': dest_name,
            'private': private,
            'auto_init': False,
            'description': marker + ' [repo-mirror:initializing:v1]',
        }
        if self.account_type == 'personal' and isinstance(self.dst, GitHubAPI):
            created = self.dst.create_personal_repository(self.owner, payload)
        else:
            if self.account_type == 'personal':
                self.owner_identity()
            endpoint = '/user/repos' if self.account_type == 'personal' else '/orgs/' + quote(self.owner, safe='') + '/repos'
            created = self.dst.request('POST', GITHUB + endpoint, payload)
        if not isinstance(created, dict) or str(created.get('full_name', '')).lower() != (self.owner + '/' + dest_name).lower():
            raise RuntimeError('created repository owner does not match configured destination')
        created_id = created.get('id')
        if type(created_id) is not int or created_id <= 0:
            raise ValueError('invalid created destination repository id')
        if 'private' in created and created['private'] is not private:
            raise ValueError('created destination repository identity not verified')
        check_existing(created, marker)
        self._created_state[dest_name] = {'private': private, 'marker': marker}
        return created_id

    def disable_actions(self, dest_name):
        target = self._repo_target(dest_name)
        self.dst.request('PUT', target + '/actions/permissions', {'enabled': False})
        permissions = self.dst.request('GET', target + '/actions/permissions')
        if not isinstance(permissions, dict) or permissions.get('enabled') is not False:
            raise ValueError('destination Actions not verified disabled')

    def verify_repository(self, dest_name, expected_id, *, private=None, marker=None):
        state = self._created_state.get(dest_name, {})
        if private is None:
            private = state.get('private')
        if marker is None:
            marker = state.get('marker')
        readback = self.get_repository(dest_name)
        if (
            not isinstance(readback, dict)
            or type(readback.get('id')) is not int
            or readback['id'] != expected_id
            or str(readback.get('full_name', '')).lower() != (self.owner + '/' + dest_name).lower()
            or ('private' in readback and private is not None and readback['private'] is not private)
        ):
            raise ValueError('created destination repository identity not verified')
        if marker is not None:
            check_existing(readback, marker)
        return readback

    def rename_repository(self, old_name, new_name, *, expected_id):
        patched = self.dst.request('PATCH', self._repo_target(old_name), {'name': new_name})
        if (
            not isinstance(patched, dict)
            or type(patched.get('id')) is not int
            or patched['id'] != expected_id
            or patched.get('name') != new_name
            or str(patched.get('full_name', '')).lower() != (self.owner + '/' + new_name).lower()
        ):
            raise ValueError('adoption destination rename verification failed')
        readback = self.get_repository(new_name)
        if (
            not isinstance(readback, dict)
            or type(readback.get('id')) is not int
            or readback['id'] != expected_id
            or readback.get('name') != new_name
            or str(readback.get('full_name', '')).lower() != (self.owner + '/' + new_name).lower()
        ):
            raise ValueError('adoption destination rename verification failed')
        return readback


class ForgejoDestinationOnboardingAdapter:
    """Destination adapter for local Forgejo onboarding via the sync API path."""

    def __init__(self, api, owner, *, organization=False):
        self.api = api
        self.owner = _name(owner)
        self.organization = bool(organization)
        self._created_state = {}

    def _helper(self):
        return ForgejoDestination(self.api, self.owner, organization=self.organization)

    def marker_for(self, item):
        identity = parse_source(f'{item.base_url}/{item.owner}/{item.name}', item.source_type)
        return _source_marker(identity)

    def owner_identity(self):
        if self.organization:
            return self.owner
        request = self.api.request
        if isinstance(self.api, LocalForgejoAPI):
            request = partial(request, token=self.api.token())
        identity = request('GET', '/api/v1/user')
        login = identity.get('login') if isinstance(identity, dict) else None
        if not isinstance(login, str) or not login or login.lower() != self.owner.lower():
            raise RuntimeError('authenticated Forgejo user does not match configured destination')
        return login

    def get_repository(self, dest_name):
        dest_name = _name(dest_name)
        try:
            return self.api.request('GET', f'/api/v1/repos/{self.owner}/{dest_name}')
        except APIError as exc:
            if exc.status == 404:
                return None
            raise

    def create_repository(self, dest_name, private, marker):
        dest_name = _name(dest_name)
        request = self.api.request
        if not self.organization:
            if isinstance(self.api, LocalForgejoAPI):
                request = partial(request, token=self.api.token())
            identity = request('GET', '/api/v1/user')
            login = identity.get('login') if isinstance(identity, dict) else None
            if not isinstance(login, str) or not login or login.lower() != self.owner.lower():
                raise RuntimeError('authenticated Forgejo user does not match configured destination')
        endpoint = f'/api/v1/orgs/{self.owner}/repos' if self.organization else '/api/v1/user/repos'
        created = request(
            'POST',
            endpoint,
            {
                'name': dest_name,
                'private': private,
                'auto_init': False,
                'description': marker + ' [repo-mirror:initializing:v1]',
                'has_actions': False,
            },
        )
        self._helper()._verify(created, dest_name, marker, actions=False, private=private)
        self._created_state[dest_name] = {
            'private': private,
            'marker': marker,
            'description': created['description'],
            'has_actions': created.get('has_actions'),
        }
        return created['id']

    def disable_actions(self, dest_name):
        state = self._created_state.get(dest_name)
        if state is not None and state.get('has_actions') is not False:
            raise RuntimeError('Forgejo repository identity, provenance, privacy or Actions not verified')

    def verify_repository(self, dest_name, expected_id, *, private=None, marker=None):
        dest_name = _name(dest_name)
        state = self._created_state.get(dest_name, {})
        if private is None:
            private = state.get('private', True)
        if marker is None:
            marker = state.get('marker')
        readback = self.get_repository(dest_name)
        if marker is None:
            raise ValueError('destination changed during creation')
        self._helper()._verify(readback, dest_name, marker, actions=True, private=private)
        if readback['id'] != expected_id:
            raise ValueError('destination changed during creation')
        expected_description = state.get('description')
        if expected_description is not None and readback.get('description') != expected_description:
            raise ValueError('destination changed during creation')
        return readback


def _validate_document(doc, runner):
    if runner == 'github':
        return normalize_config(doc)
    if runner != 'forgejo':
        raise ValueError('invalid onboarding runner')
    if not isinstance(doc, dict):
        raise ValueError('invalid named manifest')
    if 'groups' in doc and set(doc) - {CONFIG_KEY, 'auto_discoveries'} != {'destinations', 'repositories', 'groups'}:
        raise ValueError('invalid group manifest')
    parse_manifest(doc)
    return {k: v for k, v in doc.items() if k != 'groups'} if 'groups' in doc else doc


def _extract_rules(raw_config, validated, runner, *, stream=None, emit_group_warnings=True):
    if runner == 'github':
        return [(idx, rule, raw_config['auto_discoveries'][idx]) for idx, rule in enumerate(validated.get('auto_discoveries', []))]
    auto_rules = _normalize_auto_discoveries(raw_config['auto_discoveries']) if 'auto_discoveries' in raw_config else []
    items = [(idx, rule, raw_config['auto_discoveries'][idx]) for idx, rule in enumerate(auto_rules)]
    if 'groups' in raw_config:
        group_rules = translate_groups(
            raw_config,
            normalized_auto_discoveries=auto_rules,
            stream=(stream if emit_group_warnings else None),
        )
        items.extend((None, rule, None) for rule in group_rules)
    return items


def _build_repository_entry(item, profile, dest_id, *, from_groups=False):
    if from_groups:
        return {
            'source': {'type': 'gitlab', 'url': f'{item.base_url}/{item.owner}/{item.name}', 'id': item.source_id},
            'mirrors': {profile: {'name': item.dest_name, 'id': dest_id}},
        }
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


def _collect_existing_destination_names(raw_config, profile, rules_by_url, *, forgejo=False):
    names = set()
    for entry in raw_config['repositories']:
        mirror = entry['mirrors'][profile]
        if 'name' in mirror:
            names.add(mirror['name'].lower())
        elif 'discovered_by' in entry:
            rule = rules_by_url[entry['discovered_by']]
            names.add(_configured_child_dest_name(rule, entry, profile, allow_forgejo_port=forgejo).lower())
        else:
            kind, base, owner, name = parse_repo_url(
                entry['source']['url'],
                source_type=entry['source'].get('type'),
                allow_forgejo_port=forgejo,
            )
            if forgejo:
                names.add(name.lower())
            else:
                names.add(destination_name({'type': kind, 'base_url': base, 'owner': owner}, name).lower())
        legacy = mirror.get('legacy')
        if isinstance(legacy, dict):
            for legacy_key in ('destination_name', 'name'):
                if isinstance(legacy.get(legacy_key), str):
                    names.add(legacy[legacy_key].lower())
    return names


def _log_rule_failure(rule_plan, exc, stream):
    print(
        f"auto_discovery {rule_plan.index}/{rule_plan.total} "
        f"({safe_label(rule_plan.rule['owner'])}): FAILED: {error_detail(exc)}",
        file=stream,
        flush=True,
    )


def _remove_adopted_entries(repositories, rule_plan, adoptions):
    for adoption in adoptions:
        matches = [
            idx
            for idx, entry in enumerate(repositories)
            if entry.get('discovered_by') == rule_plan.old_rule_url
            and entry.get('source', {}).get('id') == adoption.old_source_id
            and entry.get('source', {}).get('url') == adoption.old_source_url
        ]
        if len(matches) != 1:
            raise ValueError('authoritative configuration differs from checkout')
        del repositories[matches[0]]


def _prevalidate_plans(raw_config, profile, plans, destination_adapter, stream, *, runner='github'):
    allow_forgejo_port = runner == 'forgejo'
    rules_by_url = {plan.rule['url']: plan.rule for plan in plans}
    planned_dests = set()
    projected = copy.deepcopy(raw_config)
    reloc_by_old_url = {}
    for rule_plan in plans:
        if rule_plan.from_groups:
            continue
        if rule_plan.adoptions:
            _remove_adopted_entries(projected['repositories'], rule_plan, rule_plan.adoptions)
        if rule_plan.needs_id_pin:
            projected['auto_discoveries'][rule_plan.raw_index]['id'] = rule_plan.owner_id
        if rule_plan.relocated_to is not None:
            raw_rule = projected['auto_discoveries'][rule_plan.raw_index]
            raw_rule['url'] = rule_plan.rule['url']
            if rule_plan.preserve_prefix is not None:
                raw_rule['destination_name_prefix'] = rule_plan.preserve_prefix
            reloc_by_old_url[rule_plan.old_rule_url] = rule_plan
    for entry in projected['repositories']:
        rule_plan = reloc_by_old_url.get(entry.get('discovered_by'))
        if rule_plan is not None:
            _, _, _, child_name = parse_repo_url(
                entry['source']['url'],
                source_type=entry['source'].get('type'),
                allow_forgejo_port=allow_forgejo_port,
            )
            entry['source']['url'] = f"{rule_plan.rule['base_url']}/{rule_plan.relocated_to}/{child_name}"
            entry['discovered_by'] = rule_plan.rule['url']
    existing_dests = _collect_existing_destination_names(
        projected, profile, rules_by_url, forgejo=allow_forgejo_port
    )
    for rule_plan in plans:
        for item in (*rule_plan.planned, *(adoption.item for adoption in rule_plan.adoptions)):
            key = item.dest_name.lower()
            if key in existing_dests or key in planned_dests:
                exc = ValueError('duplicate destination repository')
                _log_rule_failure(rule_plan, exc, stream)
                raise exc
            planned_dests.add(key)
            projected['repositories'].append(_build_repository_entry(item, profile, 1, from_groups=rule_plan.from_groups))
    try:
        _validate_document(projected, runner)
    except Exception as exc:
        if plans:
            target_plan = next((p for p in plans if p.relocated_to is not None), plans[0])
            _log_rule_failure(target_plan, exc, stream)
        raise

    for rule_plan in plans:
        for item in rule_plan.planned:
            try:
                existing = destination_adapter.get_repository(item.dest_name)
            except Exception as exc:
                _log_rule_failure(rule_plan, exc, stream)
                raise
            if existing is not None:
                exc = ValueError('unpinned existing destination requires manual reconciliation')
                _log_rule_failure(rule_plan, exc, stream)
                raise exc


def _create_destination(item, destination_adapter):
    marker = destination_adapter.marker_for(item)
    private = item.visibility != 'public'
    created_id = destination_adapter.create_repository(item.dest_name, private, marker)
    destination_adapter.disable_actions(item.dest_name)
    destination_adapter.verify_repository(item.dest_name, created_id, private=private, marker=marker)
    return created_id


def _archive_existing_destination(adoption, destination_adapter, stamp):
    existing = destination_adapter.get_repository(adoption.item.dest_name)
    if existing is None:
        return None
    if (
        not isinstance(existing, dict)
        or type(existing.get('id')) is not int
        or existing['id'] != adoption.old_dest_id
        or str(existing.get('full_name', '')).lower()
        != f"{destination_adapter.owner}/{adoption.item.dest_name}".lower()
    ):
        raise ValueError('adoption destination identity mismatch')
    target_archive = archive_name(adoption.item.dest_name, stamp)
    if destination_adapter.get_repository(target_archive) is not None:
        raise ValueError('adoption archive name already exists')
    destination_adapter.rename_repository(
        adoption.item.dest_name,
        target_archive,
        expected_id=adoption.old_dest_id,
    )
    return target_archive


def _publish_rule(
    store,
    config_bytes,
    rule_plan,
    created_entries,
    profile,
    *,
    runner='github',
    removed_adoptions=(),
):
    allow_forgejo_port = runner == 'forgejo'
    current, sha = store.read()
    if current != config_bytes:
        raise ValueError('authoritative configuration differs from checkout')
    doc = json.loads(config_bytes, object_pairs_hook=unique_pairs)
    if rule_plan.from_groups:
        rule_okey = group_origin_key(rule_plan.rule['base_url'])
        if not any(
            isinstance(g, dict)
            and group_origin_key(g.get('base_url', '')) == rule_okey
            and g.get('id') == rule_plan.owner_id
            for g in doc.get('groups', ())
        ):
            raise ValueError('source owner identity mismatch')
    else:
        raw_rule = doc['auto_discoveries'][rule_plan.raw_index]
        if rule_plan.needs_id_pin:
            if 'id' in raw_rule:
                raise ValueError('authoritative configuration differs from checkout')
            raw_rule['id'] = rule_plan.owner_id
        elif raw_rule.get('id') != rule_plan.owner_id:
            raise ValueError('source owner identity mismatch')
        if removed_adoptions:
            _remove_adopted_entries(doc['repositories'], rule_plan, removed_adoptions)
        if rule_plan.relocated_to is not None:
            raw_rule['url'] = rule_plan.rule['url']
            if rule_plan.preserve_prefix is not None:
                raw_rule['destination_name_prefix'] = rule_plan.preserve_prefix
            for entry in doc['repositories']:
                if entry.get('discovered_by') == rule_plan.old_rule_url:
                    _, _, _, child_name = parse_repo_url(
                        entry['source']['url'],
                        source_type=entry['source'].get('type'),
                        allow_forgejo_port=allow_forgejo_port,
                    )
                    entry['source']['url'] = f"{rule_plan.rule['base_url']}/{rule_plan.relocated_to}/{child_name}"
                    entry['discovered_by'] = rule_plan.rule['url']
    doc['repositories'].extend(created_entries)
    _validate_document(doc, runner)
    after = (json.dumps(doc, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    store.publish(sha, after)
    observed, _ = store.read()
    if observed != after:
        raise ValueError('configuration readback mismatch')
    return after


def _find_orphaned_legacy(
    raw_config,
    profile,
    *,
    allow_forgejo_port=False,
    active_group_bindings=frozenset(),
    conflicted_keys=frozenset(),
):
    orphans = []
    for entry in raw_config['repositories']:
        if 'discovered_by' in entry:
            continue
        mirror_name = entry['mirrors'][profile].get('name', '')
        if not isinstance(mirror_name, str) or not _GENERATED.fullmatch(mirror_name):
            continue
        if (entry['source']['url'], entry['source'].get('id'), mirror_name) in active_group_bindings:
            continue
        kind, base, owner, name = parse_repo_url(
            entry['source']['url'],
            source_type=entry['source'].get('type'),
            allow_forgejo_port=allow_forgejo_port,
        )
        if (kind, group_origin_key(base), owner.lower(), name.lower(), entry['source'].get('id')) in conflicted_keys:
            continue
        if kind == 'gitlab':
            orphans.append((owner, name, mirror_name))
    return tuple(orphans)


def onboard_auto_discoveries(
    config_bytes,
    store,
    source_client,
    dst=None,
    *,
    owner=None,
    account_type='personal',
    stream=None,
    destination_adapter=None,
    runner='github',
    emit_group_warnings=True,
    archive_timestamp=None,
):
    """Run fail-closed discovery, destination creation, and per-rule CAS publication."""
    stream = sys.stderr if stream is None else stream
    if destination_adapter is None:
        destination_adapter = GitHubDestinationOnboardingAdapter(dst, owner, account_type)
    current, _ = store.read()
    if current != config_bytes:
        raise ValueError('authoritative configuration differs from checkout')
    raw_config = json.loads(config_bytes, object_pairs_hook=unique_pairs)
    validated = _validate_document(raw_config, runner)
    rule_items = _extract_rules(raw_config, validated, runner, stream=stream, emit_group_warnings=emit_group_warnings)
    profile = next(iter(raw_config['destinations']))

    plans = []
    for index, (raw_index, rule, raw_rule) in enumerate(rule_items, 1):
        try:
            plans.append(
                _plan_rule(
                    index,
                    len(rule_items),
                    rule,
                    raw_rule,
                    profile,
                    raw_config['repositories'],
                    source_client(rule),
                    runner=runner,
                    raw_index=raw_index,
                )
            )
        except Exception as exc:
            print(
                f"auto_discovery {index}/{len(rule_items)} ({safe_label(rule['owner'])}): FAILED: {error_detail(exc)}",
                file=stream,
                flush=True,
            )
            raise

    _prevalidate_plans(raw_config, profile, plans, destination_adapter, stream, runner=runner)

    any_writes = False
    inducted_total = 0
    adopted_total = 0
    all_orphaned_children = []
    all_conflicts = []
    rule_reports = []
    relocated_rules = []
    for rule_plan in plans:
        runtime_conflicts = list(rule_plan.conflicts)
        ready_adoptions = []
        adopted_records = []
        renamed_any = False
        if rule_plan.planned or rule_plan.adoptions or rule_plan.needs_id_pin or rule_plan.relocated_to is not None:
            if store.read()[0] != config_bytes:
                exc = ValueError('authoritative configuration differs from checkout')
                _log_rule_failure(rule_plan, exc, stream)
                raise exc
        if rule_plan.adoptions:
            stamp = archive_timestamp if archive_timestamp is not None else _utc_timestamp()
            for adoption in rule_plan.adoptions:
                try:
                    archived = _archive_existing_destination(adoption, destination_adapter, stamp)
                except Exception:
                    runtime_conflicts.append(
                        IdentityConflict(
                            source_type=adoption.item.source_type,
                            base_url=adoption.item.base_url,
                            owner=adoption.item.owner,
                            name=adoption.item.name,
                            pinned_id=adoption.old_source_id,
                            discovered_id=adoption.item.source_id,
                            reason='failed to archive existing destination',
                        )
                    )
                    continue
                ready_adoptions.append((adoption, archived))
                if archived is not None:
                    renamed_any = True

        if rule_plan.planned or ready_adoptions or rule_plan.needs_id_pin or rule_plan.relocated_to is not None:
            attempted = []
            created_entries = []
            removed_adoptions = [adoption for adoption, _ in ready_adoptions]
            try:
                for item in rule_plan.planned:
                    attempted.append(item.dest_name)
                    dest_id = _create_destination(item, destination_adapter)
                    created_entries.append(_build_repository_entry(item, profile, dest_id, from_groups=rule_plan.from_groups))
                for adoption, _ in ready_adoptions:
                    attempted.append(adoption.item.dest_name)
                    dest_id = _create_destination(adoption.item, destination_adapter)
                    created_entries.append(
                        _build_repository_entry(adoption.item, profile, dest_id, from_groups=rule_plan.from_groups)
                    )
                config_bytes = _publish_rule(
                    store,
                    config_bytes,
                    rule_plan,
                    created_entries,
                    profile,
                    runner=runner,
                    removed_adoptions=removed_adoptions,
                )
            except Exception as exc:
                if renamed_any:
                    wrapped = PartialOnboarding(
                        attempted,
                        message='adoption publication failed; archived destinations left in place; reconcile manually',
                    )
                elif attempted:
                    wrapped = PartialOnboarding(attempted)
                else:
                    wrapped = OnboardingPublicationError('auto-discovery onboarding publication failed; reconcile manually')
                _log_rule_failure(rule_plan, wrapped, stream)
                raise wrapped from exc
            any_writes = True
            inducted_total += len(rule_plan.planned)
            adopted_records = list(ready_adoptions)
            adopted_total += len(adopted_records)

        if rule_plan.relocated_to is not None:
            reloc_line = (
                f"auto_discovery {rule_plan.index}/{rule_plan.total} "
                f"({safe_label(rule_plan.old_owner)}): relocated to {safe_label(rule_plan.relocated_to)}"
            )
            print(reloc_line, file=stream, flush=True)
            rule_reports.append(reloc_line)
            relocated_rules.append((rule_plan.old_rule_url, rule_plan.rule['url']))

        for adoption, archived_name in adopted_records:
            if archived_name is not None:
                adopted_line = (
                    f"auto_discovery {rule_plan.index}/{rule_plan.total} "
                    f"({safe_label(rule_plan.rule['owner'])}): "
                    f"repository {safe_label(adoption.item.name)}: "
                    f"upstream recreated (pinned id {adoption.old_source_id}, now {adoption.item.source_id}); "
                    f"archived as {safe_label(archived_name)}; adopted"
                )
            else:
                adopted_line = (
                    f"auto_discovery {rule_plan.index}/{rule_plan.total} "
                    f"({safe_label(rule_plan.rule['owner'])}): "
                    f"repository {safe_label(adoption.item.name)}: "
                    f"upstream recreated (pinned id {adoption.old_source_id}, now {adoption.item.source_id}); "
                    f"archived previously; adopted"
                )
            print(adopted_line, file=stream, flush=True)
            rule_reports.append(adopted_line)

        for conflict in runtime_conflicts:
            if conflict.reason:
                conflict_line = (
                    f"auto_discovery {rule_plan.index}/{rule_plan.total} "
                    f"({safe_label(rule_plan.rule['owner'])}): "
                    f"repository {safe_label(conflict.name)}: "
                    f"upstream identity changed (pinned id {conflict.pinned_id}, now {conflict.discovered_id}); "
                    f"{conflict.reason}; entry kept, not mirrored"
                )
            else:
                conflict_line = (
                    f"auto_discovery {rule_plan.index}/{rule_plan.total} "
                    f"({safe_label(rule_plan.rule['owner'])}): "
                    f"repository {safe_label(conflict.name)}: "
                    f"upstream identity changed (pinned id {conflict.pinned_id}, now {conflict.discovered_id}); "
                    f"entry kept, not mirrored; remove the entry and re-add it to adopt"
                )
            print(conflict_line, file=stream, flush=True)
            rule_reports.append(conflict_line)
        all_conflicts.extend(runtime_conflicts)

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
        line += f" conflicts={len(runtime_conflicts)} adopted={len(adopted_records)}"
        print(line, file=stream, flush=True)
        rule_reports.append(line)
        all_orphaned_children.extend(rule_plan.orphaned_children)

    if not any_writes:
        observed, _ = store.read()
        if observed != config_bytes:
            raise ValueError('authoritative configuration differs from checkout')

    final_config = json.loads(config_bytes, object_pairs_hook=unique_pairs)
    all_active_group_bindings = frozenset(b for p in plans for b in p.active_bindings)
    orphaned_legacy = _find_orphaned_legacy(
        final_config,
        profile,
        allow_forgejo_port=(runner == 'forgejo'),
        active_group_bindings=all_active_group_bindings,
        conflicted_keys=conflict_identity_keys(all_conflicts),
    )
    return OnboardingResult(
        config_bytes=config_bytes,
        rule_reports=tuple(rule_reports),
        orphaned_children=tuple(all_orphaned_children),
        orphaned_legacy=orphaned_legacy,
        inducted_total=inducted_total,
        conflicts=tuple(all_conflicts),
        adopted_total=adopted_total,
        relocated_rules=tuple(relocated_rules),
        active_group_bindings=all_active_group_bindings,
    )


def onboard_auto_discoveries_forgejo(
    config_bytes,
    cas,
    source_client,
    destination_api,
    *,
    owner,
    organization=False,
    stream=None,
    emit_group_warnings=True,
):
    """Run fail-closed ``auto_discoveries`` onboarding for the Forgejo runner."""
    store = CASConfigStore(cas)
    adapter = ForgejoDestinationOnboardingAdapter(destination_api, owner, organization=organization)
    return onboard_auto_discoveries(
        config_bytes,
        store,
        source_client,
        destination_adapter=adapter,
        runner='forgejo',
        stream=stream,
        emit_group_warnings=emit_group_warnings,
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
