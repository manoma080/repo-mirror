"""Select exact source URLs from a fully discovered, collision-checked plan.

The caller must complete discovery and destination collision checks first, and
abort filtered runs if discovery failed. No I/O or journal changes occur here.
"""

from urllib.parse import urlsplit


def filter_plan(plan, source_urls):
    """Return selected (source_index, source, name) entries in plan order.

    ``source_urls`` is a comma-separated string; None or whitespace selects all.
    URLs must match the configured base URL, owner and discovered name exactly.
    Errors deliberately do not echo input, which may contain credentials.
    """
    plan = list(plan)
    if source_urls is None or not source_urls.strip():
        return plan

    requested = set()
    for part in source_urls.split(','):
        url = part.strip()
        try:
            parsed = urlsplit(url)
            valid = (url and all(ord(c) > 32 and ord(c) != 127 for c in url)
                     and parsed.scheme == 'https' and parsed.hostname
                     and parsed.username is None and parsed.password is None
                     and not parsed.query and not parsed.fragment
                     and '?' not in url and '#' not in url
                     and parsed.port != 0)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('invalid source repository URL')
        if url in requested:
            raise ValueError('duplicate source repository URL')
        requested.add(url)

    matches = {}
    for entry in plan:
        _, source, name = entry
        url = f"{source['base_url'].rstrip('/')}/{source['owner']}/{name}"
        urls = {url}
        if source.get('type') == 'github' and source['base_url'].rstrip('/') == 'https://api.github.com':
            urls.add(f"https://github.com/{source['owner']}/{name}")
        if len(urls & requested) > 1:
            raise ValueError('duplicate source repository URL')
        for candidate in urls & requested:
            if candidate in matches:
                raise ValueError('ambiguous source repository URL')
            matches[candidate] = entry
    if requested - matches.keys():
        raise ValueError('unknown or unsupported source repository URL')
    return [entry for entry in plan if entry in matches.values()]
