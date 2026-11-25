"""sources: see README for an overview."""

import re
from urllib.error import HTTPError
from urllib.parse import urlparse

from repo_mirror.constants import GITHUB, NAME
from repo_mirror.github_api import not_found


def validate_source(source):
    """Check that a normalized source entry has the required fields and valid values."""
    if not isinstance(source, dict) or source.get("type") not in ("github", "forgejo", "gitlab"):
        raise ValueError("source type must be github, forgejo or gitlab")
    if set(source) - {"type", "base_url", "owner", "name", "mirror", "discovered_by", "destination_profile"}:
        raise ValueError("invalid source entry")
    u = urlparse(source.get("base_url", ""))
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError("source base_url must be credential-free HTTPS")
    owner = source.get('owner')
    owners = owner.split('/') if isinstance(owner, str) and source['type'] == 'gitlab' else [owner]
    for value in owners:
        if not isinstance(value, str) or not NAME.fullmatch(value) or value in (".", ".."):
            raise ValueError("invalid owner")
    name = source.get("name")
    if not isinstance(name, str) or not NAME.fullmatch(name) or name in (".", ".."):
        raise ValueError("invalid repository name")
    settings = source.get("mirror", {})
    if (
        not isinstance(settings, dict)
        or set(settings) - {"name", "visibility", "source_id", "destination_id", "legacy", "ignore_releases"}
        or ("ignore_releases" in settings and not isinstance(settings["ignore_releases"], bool))
        or any(type(settings[k]) is not int or settings[k] <= 0 for k in ("source_id", "destination_id") if k in settings)
        or settings.get("visibility", "private") not in ("private", "public")
    ):
        raise ValueError("invalid mirror settings")
    legacy = settings.get("legacy")
    if "legacy" in settings:
        if (
            not isinstance(legacy, dict)
            or set(legacy) != {"source_owner", "source_name", "destination_name", "signature"}
            or not {"source_id", "destination_id"} <= settings.keys()
            or any(
                not isinstance(v, str) or not NAME.fullmatch(v) or v in (".", "..") or len(v) > 100
                for k, v in legacy.items()
                if k != "signature"
            )
            or not isinstance(legacy.get("signature"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", legacy["signature"])
        ):
            raise ValueError("invalid legacy repository binding")
    if "name" in settings and (
        not isinstance(settings["name"], str)
        or len(settings["name"]) > 100
        or not NAME.fullmatch(settings["name"])
        or settings["name"] in (".", "..")
    ):
        raise ValueError("invalid mirror settings")


def source_api_base(source):
    """Return the API base URL for a source (GitHub or Forgejo host)."""
    base = source["base_url"].rstrip("/")
    if source["type"] == "gitlab":
        return base + "/api/v4"
    if source["type"] == "forgejo":
        return base + "/api/v1"
    return GITHUB if base == "https://github.com" else base


def verified_source_identity(source, name, expected_id, metadata):
    """Return a canonical source URL only when the pinned identity is proven.

    The caller must obtain metadata from the configured source using an
    origin-restricted client. This function does not follow redirects itself.
    """
    from repo_mirror.adhoc import parse_repo_url

    if (type(expected_id) is not int or expected_id <= 0 or not isinstance(metadata, dict)
            or type(metadata.get('id')) is not int or metadata['id'] != expected_id):
        raise ValueError('source repository id mismatch or missing')
    url = metadata.get('web_url' if source['type'] == 'gitlab' else 'html_url')
    try:
        kind, base, owner, repo = parse_repo_url(url, allow_forgejo_port=source['type'] in ('forgejo', 'gitlab'), source_type=source['type'])
        original_base = source['base_url'].rstrip('/')
        if source['type'] == 'github' and original_base == GITHUB:
            original_base = 'https://github.com'
        full_name = metadata.get('path_with_namespace' if source['type'] == 'gitlab' else 'full_name')
        if (kind != source['type'] or base.lower() != original_base.lower()
                or not isinstance(full_name, str) or full_name != owner + '/' + repo):
            raise ValueError('source repository identity is not verified')
    except (TypeError, KeyError, ValueError):
        raise ValueError('source repository identity is not verified') from None
    return base + '/' + owner + '/' + repo


def destination_name(source, name):
    """Return the destination repository name for a source repository."""
    if not isinstance(name, str) or not NAME.fullmatch(name) or name in (".", ".."):
        raise ValueError("invalid discovered repository name")
    owner = source['owner'].replace('/', '-') if source['type'] == 'gitlab' else source['owner']
    result = source.get("mirror", {}).get("name", f"mirror-{owner}-{name}")
    if len(result) > 100 or not NAME.fullmatch(result) or result in (".", ".."):
        raise ValueError("invalid destination repository name")
    return result


def source_marker(source, name):
    """Return the signed marker text stored in a destination repository description."""
    return (
        "[repo-mirror: "
        + source["type"]
        + ":"
        + source["base_url"].rstrip("/").lower()
        + "/"
        + source["owner"]
        + "/"
        + name
        + "]"
    )


def check_existing(repo, marker):
    """Look up the destination repository and confirm it is ours before writing to it."""
    if repo is not None and repo.get("description") not in (
        marker,
        marker + " [repo-mirror:initializing:v1]",
    ):
        raise RuntimeError("destination collision: repository has no matching source marker")


def get_existing(dst, target):
    """Fetch an existing destination repository, or None if it does not exist."""
    try:
        return dst.request("GET", target)
    except HTTPError as exc:
        if not not_found(exc):
            raise
        exc.close()
        return None
