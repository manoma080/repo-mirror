"""sources: see README for an overview."""

from urllib.error import HTTPError
from urllib.parse import quote, urlparse

from repo_mirror.constants import GITHUB, NAME
from repo_mirror.github_api import not_found


def validate_source(source):
    """Check that a source entry from the config has the required fields and valid values."""
    if not isinstance(source, dict) or source.get("type") not in ("github", "forgejo"):
        raise ValueError("source type must be github or forgejo")
    u = urlparse(source.get("base_url", ""))
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError("source base_url must be credential-free HTTPS")
    for value in [source.get("owner"), source.get("prefix", "") if source.get("prefix", "") != "" else "x"]:
        if not isinstance(value, str) or not NAME.fullmatch(value) or value in (".", ".."):
            raise ValueError("invalid owner or prefix")
    mappings = source.get("destinations", {})
    if not isinstance(mappings, dict):
        raise ValueError("invalid destination mappings")
    for key, settings in mappings.items():
        if (not isinstance(key, str) or key != key.lower() or not NAME.fullmatch(key) or key in (".", "..")
                or not isinstance(settings, dict) or set(settings) - {"name", "visibility", "source_id", "destination_id"}
                or any(type(settings[k]) is not int or settings[k] <= 0
                       for k in ("source_id", "destination_id") if k in settings)
                or settings.get("visibility", "private") not in ("private", "public")):
            raise ValueError("invalid destination mappings")
        if "name" in settings and (not isinstance(settings["name"], str) or len(settings["name"]) > 100
                or not NAME.fullmatch(settings["name"]) or settings["name"] in (".", "..")):
            raise ValueError("invalid destination mappings")
    repos = source.get("repos")
    if repos != "all" and (
        not isinstance(repos, list)
        or any(not isinstance(x, str) or not NAME.fullmatch(x) or x in (".", "..") for x in repos)
    ):
        raise ValueError("repos must be all or a list of repository names")


def source_api_base(source):
    """Return the API base URL for a source (GitHub or Forgejo host)."""
    base = source["base_url"].rstrip("/")
    if source["type"] == "forgejo":
        return base + "/api/v1"
    return GITHUB if base == "https://github.com" else base


def discover(source, api):
    """List the source repositories that a source entry expands to."""
    if source["repos"] != "all":
        return source["repos"]
    return [
        repo["name"]
        for repo in api.pages(f"{source_api_base(source)}/users/{quote(source['owner'], safe='')}/repos")
    ]


def destination_name(source, name):
    """Return the destination repository name for a source repository."""
    if not isinstance(name, str) or not NAME.fullmatch(name) or name in (".", ".."):
        raise ValueError("invalid discovered repository name")
    result = source.get("destinations", {}).get(name.lower(), {}).get("name", source.get("prefix", "mirror-" + source["owner"] + "-") + name)
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
