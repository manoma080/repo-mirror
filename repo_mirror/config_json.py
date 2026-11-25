"""Strict JSON decoding for security-sensitive configuration files."""

LEGACY_CONFIG_ERROR = 'legacy configuration format is no longer supported'
_LEGACY_KEYS = frozenset({'sources', 'destination_account_type'})


def is_legacy_config(config):
    """Return True if ``config`` is a dict using removed legacy top-level keys."""
    return isinstance(config, dict) and bool(_LEGACY_KEYS & config.keys())


def reject_legacy_config(config):
    """Raise ``ValueError`` with the fixed safe message if ``config`` is legacy."""
    if is_legacy_config(config):
        raise ValueError(LEGACY_CONFIG_ERROR)


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result
