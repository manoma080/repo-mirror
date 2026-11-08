"""adhoc: turn repository URLs typed into a manual run into sources.

Two uses:
- ``add_to_config``: write URLs into config.json (the "add to config" checkbox).
- ``one_time_sources``: build in-memory sources for URLs that config.json does
  not already cover, so a one-off mirror works without changing the config.

URLs must be exact ``https://host/owner/name`` links. A ``github.com`` host is
treated as a GitHub source; any other host is treated as a Forgejo source.
Errors never echo the input, which may contain credentials.
"""

import json
import sys
from urllib.parse import urlsplit

from repo_mirror.constants import NAME
from repo_mirror.source_filter import entry_urls


def parse_repo_url(url):
    """Return (source_type, base_url, owner, name) for an exact repository URL."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        parts = parsed.path.split("/")
        valid = (
            bool(url)
            and all(ord(c) > 32 and ord(c) != 127 for c in url)
            and parsed.scheme == "https"
            and bool(host)
            and parsed.username is None
            and parsed.password is None
            and parsed.port is None
            and not parsed.query
            and not parsed.fragment
            and "?" not in url
            and "#" not in url
            and len(parts) == 3
            and parts[0] == ""
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("invalid source repository URL")
    owner, name = parts[1], parts[2]
    if not NAME.fullmatch(owner) or not NAME.fullmatch(name) or owner in (".", "..") or name in (".", ".."):
        raise ValueError("invalid source repository URL")
    if host == "github.com":
        return "github", "https://github.com", owner, name
    return "forgejo", "https://" + host, owner, name


def _source_for(source_type, base_url, owner, name):
    return {"type": source_type, "base_url": base_url, "owner": owner, "repos": [name]}


def add_to_config(config, source_urls):
    """Add each URL to config["sources"]. Return True if the config changed."""
    changed = False
    for part in source_urls.split(","):
        source_type, base_url, owner, name = parse_repo_url(part.strip())
        match = next(
            (
                s
                for s in config["sources"]
                if s.get("type") == source_type
                and str(s.get("base_url", "")).rstrip("/").lower() == base_url
                and str(s.get("owner", "")).lower() == owner.lower()
            ),
            None,
        )
        if match is None:
            config["sources"].append(_source_for(source_type, base_url, owner, name))
            changed = True
        elif match["repos"] == "all":
            continue  # already covered by the discovered list
        elif name.lower() not in {r.lower() for r in match["repos"]}:
            match["repos"].append(name)
            changed = True
    return changed


def one_time_sources(source_urls, plan):
    """Return sources for URLs not already covered by the config-discovered plan.

    Each returned source lists exactly one repository and is never written to config.
    """
    if source_urls is None or not source_urls.strip():
        return []
    covered = set()
    for _, source, name in plan:
        covered |= entry_urls(source, name)
    sources = []
    for part in source_urls.split(","):
        url = part.strip()
        source_type, base_url, owner, name = parse_repo_url(url)
        if url not in covered:
            sources.append(_source_for(source_type, base_url, owner, name))
    return sources


def main(argv=None):
    """Command line: ``python -m repo_mirror.adhoc --config config.json --urls "..."``."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--urls", required=True)
    args = parser.parse_args(argv)
    if not args.urls.strip():
        print("no repositories requested; config unchanged", file=sys.stderr)
        return
    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)
    if add_to_config(config, args.urls):
        with open(args.config, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print("config changed", file=sys.stderr)
    else:
        print("config unchanged", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        print("add to config failed: " + str(exc), file=sys.stderr)
        sys.exit(1)
