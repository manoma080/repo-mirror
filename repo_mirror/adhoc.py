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
import os
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


def _matches_owner(source, kind, base, owner):
    configured = str(source.get("base_url", "")).rstrip("/").lower()
    if kind == "github" and configured == "https://api.github.com":
        configured = "https://github.com"
    return (source.get("type") == kind and configured == base
            and str(source.get("owner", "")).lower() == owner.lower())


def add_to_config(config, source_urls, destination="", visibility=None):
    """Add URLs and optional per-repository destination settings."""
    parts = source_urls.split(",")
    parsed = [parse_repo_url(part.strip()) for part in parts]
    identities = [tuple(part.lower() for part in item) for item in parsed]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate source repository")
    if visibility not in (None, "private", "public"):
        raise ValueError("invalid visibility")
    if destination and (len(parsed) != 1 or len(destination) > 100 or not NAME.fullmatch(destination) or destination in (".", "..")):
        raise ValueError("custom destination requires one URL and a valid repository name")
    changed = False
    for source_type, base_url, owner, name in parsed:
        match = next(
            (
                s
                for s in config["sources"]
                if _matches_owner(s, source_type, base_url, owner)
            ),
            None,
        )
        if match is None:
            match = _source_for(source_type, base_url, owner, name)
            config["sources"].append(match)
            changed = True
        elif match["repos"] != "all" and name.lower() not in {r.lower() for r in match["repos"]}:
            match["repos"].append(name)
            changed = True
        if destination or visibility is not None:
            mappings = match.setdefault("destinations", {})
            old = mappings.get(name.lower(), {})
            new = dict(old)
            if visibility is not None:
                new["visibility"] = visibility
            if destination:
                new["name"] = destination
            if old != new:
                mappings[name.lower()] = new
                changed = True
    return changed


def remove_from_config(config, source_urls):
    """Remove explicit entries; refuse exclusions from dynamically discovered owners."""
    parsed = [parse_repo_url(part.strip()) for part in source_urls.split(",")]
    for kind, base, owner, name in parsed:
        for source in config["sources"]:
            if _matches_owner(source, kind, base, owner) and source.get("repos") == "all":
                raise ValueError("cannot remove individual repositories from an all source")
    changed = False
    for kind, base, owner, name in parsed:
        for source in list(config["sources"]):
            if _matches_owner(source, kind, base, owner):
                repos = source["repos"]
                kept = [repo for repo in repos if repo.lower() != name.lower()]
                if len(kept) != len(repos):
                    source["repos"] = kept
                    source.get("destinations", {}).pop(name.lower(), None)
                    changed = True
                    if not kept:
                        config["sources"].remove(source)
    return changed


def one_time_sources(source_urls, plan, configured_sources=()):
    """Return sources for URLs not already covered by the config-discovered plan.

    Each returned source lists exactly one repository and is never written to config.
    A selected repository under a configured all source must have been discovered.
    """
    if source_urls is None or not source_urls.strip():
        return []
    covered = set()
    for _, source, name in plan:
        covered |= {url.lower() for url in entry_urls(source, name)}
    sources = []
    for part in source_urls.split(","):
        url = part.strip()
        source_type, base_url, owner, name = parse_repo_url(url)
        if url.lower() not in covered:
            if any(source.get("repos") == "all" and _matches_owner(source, source_type, base_url, owner)
                   for source in configured_sources):
                raise ValueError("selected repository not discovered in all source")
            sources.append(_source_for(source_type, base_url, owner, name))
    return sources


def main(argv=None):
    """Command line: ``python -m repo_mirror.adhoc --config config.json --urls "..."``."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--urls", required=True)
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--destination", default="")
    parser.add_argument("--visibility", choices=("private", "public", "unchanged"), default="unchanged")
    args = parser.parse_args(argv)
    if not args.urls.strip():
        raise ValueError("at least one repository URL is required")
    if args.remove and (args.destination or args.visibility not in ("private", "unchanged")):
        raise ValueError("remove cannot be combined with destination settings")
    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)
    changed = (remove_from_config(config, args.urls) if args.remove else
               add_to_config(config, args.urls, args.destination, None if args.visibility == "unchanged" else args.visibility))
    if changed and not args.remove:
        from repo_mirror.sources import destination_name, validate_source, discover
        from repo_mirror.github_api import API, SourceAPI
        seen = set()
        requested = [parse_repo_url(part.strip()) for part in args.urls.split(",")]
        github_api, other_api = SourceAPI(os.environ.get("GH_TOKEN")), API()
        for source in config["sources"]:
            validate_source(source)
            names = discover(source, github_api if source["type"] == "github" else other_api)
            if source["repos"] == "all":
                discovered = {name.lower() for name in names}
                for kind, base, owner, name in requested:
                    if _matches_owner(source, kind, base, owner) and name.lower() not in discovered:
                        raise ValueError("selected repository not discovered in all source")
            for name in names:
                target = destination_name(source, name).lower()
                if target in seen:
                    raise ValueError("duplicate destination repository")
                seen.add(target)
    if changed:
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
