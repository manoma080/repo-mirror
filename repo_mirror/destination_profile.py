"""Validation and runtime binding for explicit destination profiles."""
import re
from urllib.parse import urlsplit


def validate_profile(settings, provider):
    if not isinstance(settings, dict) or set(settings) != {'type', 'url', 'owner', 'auth'}:
        raise ValueError('invalid destination profile')
    if settings['type'] != provider:
        raise ValueError('destination provider mismatch')
    if any(not isinstance(settings[k], str) or not settings[k] for k in ('url', 'owner', 'auth')):
        raise ValueError('invalid destination profile')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', settings['owner']) or settings['owner'] in ('.', '..'):
        raise ValueError('invalid destination owner')
    if not re.fullmatch(r'[a-z][a-z0-9-]*', settings['auth']):
        raise ValueError('invalid destination auth alias')
    try:
        url = urlsplit(settings['url'])
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.path or url.query or url.fragment or url.port == 0:
            raise ValueError('invalid destination URL')
    except ValueError:
        raise ValueError('invalid destination URL') from None
    return settings


def validate_runtime(settings, provider, url, owner, auth):
    validate_profile(settings, provider)
    if settings['url'] != url or settings['owner'].lower() != owner.lower() or settings['auth'] != auth:
        raise ValueError('destination profile runtime mismatch')
