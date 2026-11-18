"""Conditional publication of GitLab group identity bindings.

A failed publication can leave a destination repository behind.  Such a
repository must be manually reconciled; a marker alone is not proof of origin.
"""
import json

from repo_mirror.config_json import unique_pairs


def store_from_environment(environ, config_path):
    """Open the management repository's authoritative config, if configured."""
    import os
    from repo_mirror.github_api import API
    from repo_mirror.github_migration import GitHubConfigFile
    slug = environ.get('MIRROR_MANAGEMENT_REPOSITORY') or environ.get('GITHUB_REPOSITORY')
    token = (environ.get('MIRROR_MANAGEMENT_TOKEN') or environ.get('MANAGEMENT_TOKEN')
             or environ.get('GH_TOKEN'))
    if not slug or not token or os.path.basename(config_path) != 'config.json':
        return None
    parts = slug.split('/')
    if len(parts) != 2:
        raise ValueError('invalid management repository')
    return GitHubConfigFile(API(token), *parts, path='config.json')


class OnboardingPublicationError(RuntimeError):
    """Publication failed or its result is uncertain; reconciliation is required."""


def publish_binding(store, before, source_index, name, source_id, destination_id):
    """Publish one binding against byte-exact authoritative config and verify it."""
    if not isinstance(before, bytes) or not isinstance(name, str) or not name:
        raise ValueError('invalid onboarding binding')
    if any(type(value) is not int or value <= 0 for value in (source_id, destination_id)):
        raise ValueError('invalid onboarding binding identity')
    current, sha = store.read()
    if current != before:
        raise ValueError('authoritative configuration differs from checkout')
    config = json.loads(before, object_pairs_hook=unique_pairs)
    source = config['sources'][source_index]
    if source.get('type') != 'gitlab' or source.get('repos') != 'all':
        raise ValueError('invalid GitLab group source')
    entry = source.setdefault('destinations', {}).setdefault(name.lower(), {})
    if 'source_id' in entry or 'destination_id' in entry:
        raise ValueError('existing binding requires manual reconciliation')
    entry.update(source_id=source_id, destination_id=destination_id)
    after = (json.dumps(config, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    try:
        store.publish(sha, after)
        observed, _ = store.read()
        if observed != after:
            raise ValueError('configuration readback mismatch')
    except Exception as exc:
        raise OnboardingPublicationError(
            'GitLab onboarding publication uncertain; destination requires manual reconciliation'
        ) from exc
    return after
