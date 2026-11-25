"""Translate named GitHub configuration to the existing execution model."""

import re

_AUTO_DISCOVERY_KEYS = frozenset(
    {
        'url',
        'type',
        'id',
        'visibility',
        'ignore_releases',
        'destination_name_prefix',
        'exclude',
        'overrides',
    }
)
_OVERRIDE_KEYS = frozenset({'name', 'visibility', 'ignore_releases'})
_GENERATED = re.compile(r'gitlab-[0-9a-f]{24}', re.IGNORECASE)


def _normalize_auto_discoveries(raw_rules):
    """Validate and normalize top-level ``auto_discoveries`` rules."""
    from repo_mirror.adhoc import parse_owner_url
    from repo_mirror.constants import NAME

    if not isinstance(raw_rules, list):
        raise ValueError('auto_discoveries must be a list')
    normalized = []
    seen_owners = set()
    seen_ids = set()
    for rule in raw_rules:
        if not isinstance(rule, dict) or 'url' not in rule or set(rule) - _AUTO_DISCOVERY_KEYS:
            raise ValueError('invalid auto_discoveries rule')
        source_type = None
        if 'type' in rule:
            if not isinstance(rule['type'], str) or rule['type'] not in ('github', 'gitlab', 'forgejo'):
                raise ValueError('invalid auto_discoveries type')
            source_type = rule['type']
        kind, base, segments = parse_owner_url(rule['url'], source_type=source_type)
        owner = '/'.join(segments)
        canonical_url = f'{base}/{owner}'
        if 'id' in rule:
            if type(rule['id']) is not int or rule['id'] <= 0:
                raise ValueError('invalid auto_discoveries id')
        elif kind == 'gitlab':
            raise ValueError('GitLab auto_discoveries rule requires id')
        visibility = 'private'
        if 'visibility' in rule:
            if not isinstance(rule['visibility'], str) or rule['visibility'] not in ('private', 'public'):
                raise ValueError('invalid auto_discoveries visibility')
            visibility = rule['visibility']
        ignore_releases = False
        if 'ignore_releases' in rule:
            if not isinstance(rule['ignore_releases'], bool):
                raise ValueError('invalid auto_discoveries ignore_releases')
            ignore_releases = rule['ignore_releases']
        if 'destination_name_prefix' in rule:
            prefix = rule['destination_name_prefix']
            if (
                not isinstance(prefix, str)
                or not prefix
                or not NAME.fullmatch(prefix)
                or prefix in ('.', '..')
                or _GENERATED.fullmatch(prefix)
            ):
                raise ValueError('invalid destination_name_prefix')
        else:
            prefix = 'mirror-' + owner.replace('/', '-')
        exclude = []
        exclude_lower = set()
        if 'exclude' in rule:
            if not isinstance(rule['exclude'], list):
                raise ValueError('invalid auto_discoveries exclude')
            for item in rule['exclude']:
                if not isinstance(item, str) or not NAME.fullmatch(item) or item in ('.', '..'):
                    raise ValueError('invalid auto_discoveries exclude')
                if item.lower() in exclude_lower:
                    raise ValueError('duplicate exclude repository')
                exclude_lower.add(item.lower())
            exclude = list(rule['exclude'])
        overrides = {}
        override_keys_lower = set()
        if 'overrides' in rule:
            if not isinstance(rule['overrides'], dict):
                raise ValueError('invalid auto_discoveries overrides')
            for repo_name, ov in rule['overrides'].items():
                if not isinstance(repo_name, str) or not NAME.fullmatch(repo_name) or repo_name in ('.', '..'):
                    raise ValueError('invalid auto_discoveries overrides')
                if repo_name.lower() in override_keys_lower:
                    raise ValueError('duplicate override repository')
                if repo_name.lower() in exclude_lower:
                    raise ValueError('override repository also in exclude')
                override_keys_lower.add(repo_name.lower())
                if not isinstance(ov, dict) or not ov or set(ov) - _OVERRIDE_KEYS:
                    raise ValueError('invalid auto_discoveries overrides')
                if 'name' in ov:
                    ov_name = ov['name']
                    if (
                        not isinstance(ov_name, str)
                        or not (1 <= len(ov_name) <= 100)
                        or not NAME.fullmatch(ov_name)
                        or ov_name in ('.', '..')
                        or _GENERATED.fullmatch(ov_name)
                    ):
                        raise ValueError('invalid auto_discoveries override name')
                if 'visibility' in ov:
                    if not isinstance(ov['visibility'], str) or ov['visibility'] not in ('private', 'public'):
                        raise ValueError('invalid auto_discoveries override visibility')
                if 'ignore_releases' in ov:
                    if not isinstance(ov['ignore_releases'], bool):
                        raise ValueError('invalid auto_discoveries override ignore_releases')
                planned = ov['name'] if 'name' in ov else f'{prefix}-{repo_name}'
                if len(planned) > 100 or not NAME.fullmatch(planned) or planned in ('.', '..') or _GENERATED.fullmatch(planned):
                    raise ValueError('invalid destination repository name')
                overrides[repo_name] = dict(ov)
        owner_key = (base.lower(), owner.lower())
        if owner_key in seen_owners:
            raise ValueError('duplicate auto-discovery rule')
        seen_owners.add(owner_key)
        if 'id' in rule:
            id_key = (base.lower(), rule['id'])
            if id_key in seen_ids:
                raise ValueError('duplicate auto-discovery rule')
            seen_ids.add(id_key)
        entry = {
            'url': canonical_url,
            'type': kind,
            'base_url': base,
            'owner': owner,
            'visibility': visibility,
            'ignore_releases': ignore_releases,
            'destination_name_prefix': prefix,
            'exclude': exclude,
            'overrides': overrides,
        }
        if 'id' in rule:
            entry['id'] = rule['id']
        normalized.append(entry)
    return normalized


def normalize_config(config):
    from repo_mirror.config_json import reject_legacy_config
    from repo_mirror.host_limits import CONFIG_KEY, HostLimits

    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    reject_legacy_config(config)
    if set(config) - {CONFIG_KEY, 'auto_discoveries'} != {'destinations', 'repositories'}:
        raise ValueError('invalid named config fields')
    HostLimits.from_config(config.get(CONFIG_KEY), default=1)
    normalized_rules = _normalize_auto_discoveries(config['auto_discoveries']) if 'auto_discoveries' in config else []
    rules_by_url = {r['url']: r for r in normalized_rules}
    explicit_prefix_rules = [
        norm for raw, norm in zip(config.get('auto_discoveries', []), normalized_rules)
        if 'destination_name_prefix' in raw
    ]
    explicit_prefix_urls = {r['url'] for r in explicit_prefix_rules}
    destinations = config['destinations']
    if not isinstance(destinations, dict) or len(destinations) != 1:
        raise ValueError('exactly one destination profile is supported')
    profile, settings = next(iter(destinations.items()))
    if not isinstance(profile, str) or not profile or not isinstance(settings, dict):
        raise ValueError('invalid destination profile')
    explicit = 'type' in settings
    if explicit:
        from repo_mirror.destination_profile import validate_profile
        validate_profile(settings, 'github')
    elif set(settings) != {'account_type'} or settings['account_type'] not in ('personal', 'organization'):
        raise ValueError('invalid destination profile')
    repositories = config['repositories']
    if not isinstance(repositories, list):
        raise ValueError('repositories must be a list')
    from repo_mirror.adhoc import parse_repo_url
    from repo_mirror.constants import NAME
    from repo_mirror.gitlab_onboarding_forgejo import PREFIX_WITH_GENERATED_MESSAGE, group_origin_key
    from repo_mirror.sources import destination_name, validate_source
    entries, seen, seen_destinations = [], set(), {}
    for entry in repositories:
        if (
            not isinstance(entry, dict)
            or not {'source', 'mirrors'} <= set(entry)
            or set(entry) - {'source', 'mirrors', 'discovered_by'}
        ):
            raise ValueError('invalid repository entry')
        source, mirrors = entry['source'], entry['mirrors']
        if not isinstance(source, dict) or not isinstance(source.get('url'), str) or set(source) - {'url', 'id', 'type'}:
            raise ValueError('invalid repository source')
        if not isinstance(mirrors, dict) or set(mirrors) != {profile}:
            raise ValueError('exactly one mirror for the configured destination is required')
        mirror = mirrors[profile]
        if not isinstance(mirror, dict) or set(mirror) - {'name', 'id', 'visibility', 'legacy', 'ignore_releases'}:
            raise ValueError('invalid mirror settings')
        if 'ignore_releases' in mirror and not isinstance(mirror['ignore_releases'], bool):
            raise ValueError('invalid mirror settings')
        kind, base, owner, name = parse_repo_url(source['url'], source_type=source.get('type'))
        identity = (base.lower(), owner.lower(), name.lower())
        if identity in seen:
            raise ValueError('duplicate source repository')
        seen.add(identity)
        mapping = {k: v for k, v in mirror.items() if k in ('name', 'visibility', 'legacy', 'ignore_releases')}
        if 'id' in source:
            mapping['source_id'] = source['id']
        if 'id' in mirror:
            mapping['destination_id'] = mirror['id']
        item = {'type': kind, 'base_url': base, 'owner': owner, 'name': name}
        if mapping:
            item['mirror'] = mapping
        validate_source(item)
        if 'discovered_by' in entry:
            discovered_by = entry['discovered_by']
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
                dest_name = planned_name
            item['discovered_by'] = discovered_by
        else:
            dest_name = destination_name(item, name)
        if 'name' in mirror and isinstance(mirror['name'], str) and _GENERATED.fullmatch(mirror['name']):
            if entry.get('discovered_by') in explicit_prefix_urls or (
                kind == 'gitlab'
                and any(
                    r['type'] == 'gitlab'
                    and group_origin_key(r['base_url']) == group_origin_key(base)
                    and r['owner'].lower() == owner.lower()
                    for r in explicit_prefix_rules
                )
            ):
                raise ValueError(PREFIX_WITH_GENERATED_MESSAGE)
        dest_key = dest_name.lower()
        if dest_key in seen_destinations:
            raise ValueError('duplicate destination repository')
        seen_destinations[dest_key] = identity
        entries.append(item)
    for rule in normalized_rules:
        for repo_key, ov in rule['overrides'].items():
            ov_identity = (rule['base_url'].lower(), rule['owner'].lower(), repo_key.lower())
            planned = ov['name'] if 'name' in ov else f"{rule['destination_name_prefix']}-{repo_key}"
            dest_key = planned.lower()
            existing_identity = seen_destinations.get(dest_key)
            if existing_identity is not None and existing_identity != ov_identity:
                raise ValueError('duplicate destination repository')
            seen_destinations[dest_key] = ov_identity
    result = {'account_type': 'personal' if explicit else settings['account_type'], 'repositories': entries}
    if CONFIG_KEY in config:
        result[CONFIG_KEY] = dict(config[CONFIG_KEY])
    if explicit:
        result['destination_profile'] = settings.copy()
    if 'auto_discoveries' in config:
        result['auto_discoveries'] = normalized_rules
    return result
