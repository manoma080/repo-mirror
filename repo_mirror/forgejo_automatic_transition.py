"""Build a candidate Forgejo manifest transition; never publish or mutate a forge.

The caller must resolve metadata with the configured origin-restricted client and
coordinate publication with all writers. A signed candidate is not a lock or a
freshness guarantee.
"""
from copy import deepcopy

from repo_mirror.forgejo_sync import parse_manifest, parse_source, verify_source_pin
from repo_mirror.migration_attestation import forgejo_claim, sign, verify
from repo_mirror.sources import verified_source_identity


def prepare_source_transition(config, index, metadata, port, owner, secret):
    """Return a validated candidate preserving the original recovery binding."""
    if not isinstance(config, dict) or type(index) is not int or index < 0:
        raise ValueError('named manifest and repository index required')
    entries = parse_manifest(config)
    if index >= len(entries):
        raise ValueError('repository index out of range')
    entry = entries[index]
    if 'source_id' not in entry or 'destination_id' not in entry:
        raise ValueError('both original repository IDs must be pinned')
    if 'legacy_identity' in entry:
        verify(forgejo_claim(entry, port, owner), entry['signature'], secret)
    old_url = entry['url']
    base, old_owner, old_name = old_url.rsplit('/', 2)
    source = {'type': 'github' if base.lower() == 'https://github.com' else 'forgejo',
              'base_url': base, 'owner': old_owner}
    new_url = verified_source_identity(source, old_name, entry['source_id'], metadata)
    identity = parse_source(new_url)
    verify_source_pin(identity, metadata, entry['source_id'])
    if new_url == old_url:
        return deepcopy(config)
    candidate = deepcopy(config)
    repository = candidate['repositories'][index]
    profile = next(iter(candidate['destinations']))
    mirror = repository['mirrors'][profile]
    original_url = entry.get('legacy_url', old_url)
    original_name = entry.get('legacy_destination_name', entry['destination_name'])
    repository['source']['url'] = new_url
    # One naming policy for both providers: a destination name equal to the
    # name derived for the *old* source follows the move; anything else is an
    # explicit custom name and is preserved verbatim. The Forgejo manifest
    # derives a missing destination name from the source basename.
    new_base, new_owner, new_name = new_url.rsplit('/', 2)
    configured = mirror.get('name')
    if 'discovered_by' in repository:
        if old_owner.lower() != new_owner.lower():
            repository.pop('discovered_by', None)
            if configured is None or configured.lower() == old_name.lower():
                mirror['name'] = new_name
        else:
            from repo_mirror.config_named import _normalize_auto_discoveries
            rules = _normalize_auto_discoveries(candidate.get('auto_discoveries', []))
            rule = next((r for r in rules if r['url'] == repository['discovered_by']), None)
            prefix = rule['destination_name_prefix'] if rule is not None else f'mirror-{old_owner}'
            if configured is not None and configured.lower() == f'{prefix}-{old_name}'.lower():
                mirror['name'] = f'{prefix}-{new_name}'
    elif configured is None or configured.lower() == old_name.lower():
        mirror['name'] = new_name
    mirror['legacy'] = {'url': original_url, 'name': original_name, 'signature': '0' * 64}
    candidate_entry = parse_manifest(candidate)[index]
    mirror['legacy']['signature'] = sign(forgejo_claim(candidate_entry, port, owner), secret)
    parse_manifest(candidate)
    return candidate
