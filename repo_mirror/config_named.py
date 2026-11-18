"""Translate named GitHub configuration to the existing execution model."""


def normalize_config(config):
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    if 'destinations' not in config and 'repositories' not in config:
        return config
    if set(config) != {'destinations', 'repositories'}:
        raise ValueError('invalid named config fields')
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
    from repo_mirror.sources import validate_source
    sources, seen = [], set()
    for entry in repositories:
        if not isinstance(entry, dict) or set(entry) != {'source', 'mirrors'}:
            raise ValueError('invalid repository entry')
        source, mirrors = entry['source'], entry['mirrors']
        if not isinstance(source, dict) or not isinstance(source.get('url'), str) or set(source) - {'url', 'id', 'type'}:
            raise ValueError('invalid repository source')
        if not isinstance(mirrors, dict) or set(mirrors) != {profile}:
            raise ValueError('exactly one mirror for the configured destination is required')
        mirror = mirrors[profile]
        if not isinstance(mirror, dict) or set(mirror) - {'name', 'id', 'visibility', 'legacy'}:
            raise ValueError('invalid mirror settings')
        kind, base, owner, name = parse_repo_url(source['url'], source_type=source.get('type'))
        identity = (base.lower(), owner.lower(), name.lower())
        if identity in seen:
            raise ValueError('duplicate source repository')
        seen.add(identity)
        mapping = {k: v for k, v in mirror.items() if k in ('name', 'visibility', 'legacy')}
        if 'id' in source:
            mapping['source_id'] = source['id']
        if 'id' in mirror:
            mapping['destination_id'] = mirror['id']
        item = {'type': kind, 'base_url': base, 'owner': owner, 'repos': [name]}
        if mapping:
            item['destinations'] = {name.lower(): mapping}
        validate_source(item)
        sources.append(item)
    result = {'destination_account_type': 'personal' if explicit else settings['account_type'], 'sources': sources}
    if explicit:
        result['destination_profile'] = settings.copy()
    return result
