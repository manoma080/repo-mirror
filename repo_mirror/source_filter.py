"""Select exact source URLs from a fully discovered, collision-checked plan.

The caller must complete discovery and destination collision checks first, and
abort filtered runs if discovery failed. No I/O or journal changes occur here.
"""

from urllib.parse import urlsplit


def entry_urls(source, name):
    """Return the set of URLs that select one planned repository."""
    url = f"{source['base_url'].rstrip('/')}/{source['owner']}/{name}"
    urls = {url}
    if source.get("type") == "github" and source["base_url"].rstrip("/").lower() == "https://api.github.com":
        urls.add(f"https://github.com/{source['owner']}/{name}")
    discovered_by = source.get("discovered_by")
    if isinstance(discovered_by, str) and discovered_by:
        urls.add(discovered_by)
    return urls


def filter_plan(plan, source_urls, *, case_insensitive=False, known_rules=()):
    """Return selected (source_index, source, name) entries in plan order.

    ``source_urls`` is a comma-separated string; None or whitespace selects all.
    URLs must match the configured base URL, owner and discovered name exactly,
    or a declared ``discovered_by`` rule URL, unless case_insensitive is enabled
    for workflow input matching. Errors deliberately do not echo input, which
    may contain credentials.
    """
    plan = list(plan)
    if source_urls is None or not source_urls.strip():
        return plan

    requested = set()
    for part in source_urls.split(","):
        url = part.strip()
        try:
            parsed = urlsplit(url)
            valid = (
                url
                and all(ord(c) > 32 and ord(c) != 127 for c in url)
                and parsed.scheme == "https"
                and parsed.hostname
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and "?" not in url
                and "#" not in url
                and parsed.port != 0
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("invalid source repository URL")
        if case_insensitive:
            url = url.lower()
        if url in requested:
            raise ValueError("duplicate source repository URL")
        requested.add(url)

    matches = {}
    rule_matches = {}
    for idx, entry in enumerate(plan):
        _, source, name = entry
        urls = entry_urls(source, name)
        discovered_by = source.get("discovered_by")
        rule_url = discovered_by if isinstance(discovered_by, str) and discovered_by else None
        if case_insensitive:
            urls = {url.lower() for url in urls}
            rule_key = rule_url.lower() if rule_url else None
        else:
            rule_key = rule_url
        repo_urls = urls - ({rule_key} if rule_key else set())
        repo_hits = repo_urls & requested
        rule_hit = rule_key is not None and rule_key in requested
        if len(repo_hits) > 1:
            raise ValueError("duplicate source repository URL")
        if repo_hits and rule_hit:
            raise ValueError("overlapping auto-discovery rule and child repository URL")
        for candidate in repo_hits:
            if candidate in matches or candidate in rule_matches:
                raise ValueError("ambiguous source repository URL")
            matches[candidate] = idx
        if rule_hit:
            if rule_key in matches:
                raise ValueError("ambiguous source repository URL")
            rule_matches.setdefault(rule_key, []).append(idx)

    known = {rule.lower() if case_insensitive else rule for rule in known_rules}
    matched_keys = set(matches) | set(rule_matches)
    if requested - matched_keys - known:
        raise ValueError("unknown or unsupported source repository URL")

    selected_indices = set(matches.values())
    for indices in rule_matches.values():
        selected_indices.update(indices)
    return [entry for idx, entry in enumerate(plan) if idx in selected_indices]
