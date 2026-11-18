"""repository: see README for an overview."""

import base64
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import time
from datetime import UTC, datetime
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse

from repo_mirror.constants import CHUNK_SIZE, GITHUB, NAME
from repo_mirror.forgejo_source_adapter import ForgejoSourceAdapter
from repo_mirror.gitlab_source_adapter import GitLabSourceAdapter
from repo_mirror.git_sync import create_archive, mirror_git, set_archive_default
from repo_mirror.github_api import API as GitHubAPI, SourceAPI as GitHubSourceAPI
from repo_mirror.github_api import not_found, same_file, transient_transfer_error, transfer_retry_delay
from repo_mirror.journal import ReleaseRecoveryError, StarterAssetError, compare_reason
from repo_mirror.logging_utils import error_detail, safe_label, timed, timed_call, timed_repository
from repo_mirror.sources import check_existing, destination_name, get_existing, source_api_base, source_marker, validate_source

# Checksum file names accepted as a fallback source, any letter case:
# SHA256SUM, SHA256SUMS, SHA256SUM.txt, SHA256SUMS.txt.
CHECKSUM_FILE_NAME = re.compile(r"SHA256SUMS?(?:\.txt)?", re.I)
IDENTITY_SENTINEL = "[repo-mirror:draft-identity:v1]"


class _IdentityGuardedDestination:
    """Check repository pins before each destination operation."""

    def __init__(self, destination, check):
        self._destination = destination
        self._check = check

    def __getattr__(self, name):
        return getattr(self._destination, name)

    def request(self, *args, **kwargs):
        self._check()
        return self._destination.request(*args, **kwargs)

    def pages(self, url):
        # Route the standard GitHub paginator through the guarded request so
        # each page is checked, rather than only the first page.
        if getattr(type(self._destination), "pages", None) is GitHubAPI.pages:
            return GitHubAPI.pages(self, url)
        self._check()
        return self._destination.pages(url)

    def download(self, *args, **kwargs):
        self._check()
        return self._destination.download(*args, **kwargs)

    def upload(self, *args, **kwargs):
        self._check()
        return self._destination.upload(*args, **kwargs)


def draft_identity_signature(secret, source, tag, target, release_id):
    payload = json.dumps([source, tag, target, release_id], ensure_ascii=True, separators=(",", ":")).encode()
    return hmac.new(secret.encode(), b"repo-mirror:draft-identity:v1\0" + payload, hashlib.sha256).hexdigest()


def upload_with_reconciliation(dst, upload_url, data, content_type, assets_url, name):
    """Reconcile ambiguous responses before replay; never remove remote assets."""
    position = data.tell()
    for attempt in range(4):
        data.seek(position)
        try:
            dst.upload(upload_url, data, content_type)
            return
        except Exception as exc:
            conflict = isinstance(exc, HTTPError) and exc.code == 422
            if not conflict and not transient_transfer_error(exc):
                raise
            matches = [item for item in dst.pages(assets_url) if item.get("name") == name]
            if matches:
                if len(matches) != 1 or matches[0].get("state") != "uploaded" or not matches[0].get("url"):
                    raise ReleaseRecoveryError("mismatch") from exc
                with tempfile.TemporaryFile() as remote:
                    dst.download(matches[0]["url"], remote)
                    data.seek(position)
                    if not same_file(data, remote):
                        raise ReleaseRecoveryError("mismatch") from exc
                return
            if not (isinstance(exc, HTTPError) and exc.code == 429
                    and getattr(exc, "direct_upload_rejection", False) is True) or attempt == 3:
                raise
            delay = transfer_retry_delay(exc, attempt)
            if delay is None:
                raise
            if isinstance(exc, HTTPError):
                exc.close()
            print(f"asset upload retry {attempt + 1}/3", file=sys.stderr, flush=True)
            time.sleep(delay)


def release_marker_is_pending(body):
    """Any recovery marker denotes work not yet finalized, including BOUND."""
    return isinstance(body, str) and "[repo-mirror:pending:v3]" in body


def validate_source_release_markers(release):
    """Reject release metadata reserved for mirror recovery and identity."""
    body = release.get("body")
    if (body is not None and not isinstance(body, str)) or (
        isinstance(body, str) and ("[repo-mirror:pending:v3]" in body or IDENTITY_SENTINEL in body)
    ):
        raise ReleaseRecoveryError("marker")
    name = release.get("name")
    if isinstance(name, str) and name.startswith(("[repo-mirror:pending:v1]", "[repo-mirror:pending:v2]")):
        raise ValueError("source release name uses reserved marker")


def draft_original_name(release):
    name = release.get("name")
    if isinstance(name, str) and name.startswith(("[repo-mirror:pending:v1]", "[repo-mirror:pending:v2]")):
        try:
            name = json.loads(base64.urlsafe_b64decode(name.split("]", 1)[1]))[0]
        except ValueError, TypeError, IndexError:
            raise ValueError("ambiguous untagged pending draft") from None
    return name


def find_tag_releases(dst, target, tag, source_name=None, *, releases=None, marker=None, recovery_target=None):
    """Find an exact tag or an ID-bound, authenticated recovery draft.

    Names and GitHub's generated untagged-* tags are not identities. If an
    untagged draft cannot be authenticated, creation is unsafe even when its
    title differs or is absent.
    """
    if releases is None:
        releases = list(dst.pages(target + "/releases"))
    exact = [r for r in releases if r.get("tag_name") == tag]
    if exact:
        return exact
    untagged = [r for r in releases if r.get("draft") is True
                and isinstance(r.get("tag_name"), str)
                and r["tag_name"].startswith("untagged-")]
    secret = os.environ.get("MIRROR_TIMESTAMP_SECRET", "")
    verified = []
    for item in untagged:
        body = item.get("body")
        if secret and marker and isinstance(body, str) and body.count(IDENTITY_SENTINEL) == 1:
            try:
                signature = body.split("\n" + IDENTITY_SENTINEL, 1)[1]
                if type(item.get("id")) is int and item["id"] > 0 and hmac.compare_digest(
                    signature, draft_identity_signature(secret, marker, tag, recovery_target or target, item["id"])
                ):
                    verified.append(item)
                    continue
            except (IndexError, TypeError):
                pass
        sentinel = "[repo-mirror:pending:v3]"
        if not secret or not marker or not isinstance(body, str) or body.count(sentinel) != 1:
            continue
        try:
            state = json.loads(body.split("\n" + sentinel, 1)[1])
            signature = state.pop("signature")
            payload = {"version": "v3", "source": marker, "tag": tag,
                       "target": recovery_target or target, "release_id": item["id"], "state": state}
            encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
            expected = hmac.new(secret.encode(), b"repo-mirror:release-recovery:v3\0" + encoded,
                                hashlib.sha256).hexdigest()
            if isinstance(signature, str) and hmac.compare_digest(signature, expected):
                verified.append(item)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            continue
    if len(verified) == 1:
        return verified
    if untagged:
        raise ValueError("ambiguous untagged destination draft; manual reconciliation required")
    return []


def select_existing_releases(releases, destination, failed_tags, marker=None, target=None, known_draft_tags=None, recovery_target=None):
    """Select missing and pending releases without guessing draft identity."""
    by_tag = {}
    untagged = []
    for item in destination:
        by_tag.setdefault(item.get("tag_name"), []).append(item)
        if item.get("draft") is True and isinstance(item.get("tag_name"), str) and item["tag_name"].startswith("untagged-"):
            untagged.append(item)
    selected = []
    for index, release in enumerate(releases):
        tag = release["tag_name"]
        matches = by_tag.get(tag)
        if not matches:
            try:
                candidates = [item for item in untagged if not known_draft_tags
                              or item.get("id") not in known_draft_tags
                              or known_draft_tags[item["id"]] == tag]
                matches = find_tag_releases(None, target, tag, releases=candidates, marker=marker, recovery_target=recovery_target)
            except ValueError:
                # Resolve ambiguity inside the per-release isolation boundary.
                selected.append(release)
                continue
        if len(matches) > 1:
            raise ValueError("ambiguous destination releases for tag")
        existing = matches[0] if matches else None
        title = existing.get("name") if existing else None
        pending = existing and (
            (isinstance(title, str) and title.startswith(("[repo-mirror:pending:v1]", "[repo-mirror:pending:v2]")))
            or release_marker_is_pending(existing.get("body"))
        )
        if index == 0 or release.get("draft") is True or existing is None or existing.get("draft") is True or pending or tag in failed_tags:
            selected.append(release)
    return selected


def validate_source_clone_url(source, name, metadata):
    """Validate that metadata points to the configured source identity."""
    clone_url = metadata.get("clone_url") if isinstance(metadata, dict) else None
    base_url = source["base_url"]
    if not isinstance(clone_url, str) or not isinstance(base_url, str):
        raise ValueError("invalid source clone URL")
    base = urlparse(base_url)
    parsed = urlparse(clone_url)
    allowed_host = (
        "github.com" if source["type"] == "github" and base.hostname == "api.github.com" else base.hostname
    )
    base_path = base.path.rstrip("/")
    segments = base_path.split("/")[1:] if base_path else []
    expected_path = base_path + "/" + source["owner"] + "/" + name + ".git"
    clone_path_matches = (
        parsed.path.lower() == expected_path.lower()
        if source["type"] == "github"
        else parsed.path == expected_path
    )
    if (
        base.scheme != "https"
        or not base.hostname
        or base.username is not None
        or base.password is not None
        or base.port not in (None, 443)
        or parsed.scheme != "https"
        or parsed.hostname != allowed_host
        or base.params
        or parsed.params
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or any(c in base_url or c in clone_url for c in ("?", "#", "\\", "\n", "\r", "\t"))
        or any(not segment or segment in (".", "..") or "%" in segment for segment in segments)
        or any(not NAME.fullmatch(part) or part in (".", "..")
               for part in (source["owner"].split("/") if source["type"] == "gitlab" else [source["owner"]]))
        or not NAME.fullmatch(name)
        or name in (".", "..")
        or not clone_path_matches
    ):
        raise ValueError("source clone URL does not match source identity")
    return clone_url


@timed_repository
def mirror_repository(source, name, owner, src, dst, token, account_type="organization", journal=None):
    """Mirror one source repository to its destination: branches, tags, releases and assets."""
    print(
        f"repository started source_repo={safe_label(source.get('owner'))}/{safe_label(name)} target=destination",
        file=sys.stderr,
        flush=True,
    )
    dest_name = destination_name(source, name)
    settings = source.get("destinations", {}).get(name.lower(), {})
    if "legacy" in settings:
        validate_source(source)
        from repo_mirror.migration_attestation import github_claim, verify
        verify(github_claim(source, name, owner), settings["legacy"].get("signature"), os.environ.get("MIRROR_TIMESTAMP_SECRET", ""))
    if "legacy" in settings:
        validate_source(source)
        legacy = settings["legacy"]
        historical_source = {**source, "owner": legacy["source_owner"]}
        historical_name = legacy["source_name"]
        marker = source_marker(historical_source, historical_name)
    else:
        marker = source_marker(source, name)
    for key in ("source_id", "destination_id"):
        if key in settings and (type(settings[key]) is not int or settings[key] <= 0):
            raise ValueError("invalid repository id pin")
    qowner, qname = quote(owner, safe=""), quote(dest_name, safe="")
    target = f"{GITHUB}/repos/{qowner}/{qname}"
    repo = get_existing(dst, target)
    check_existing(repo, marker)
    initializing = repo is None or repo.get("description") == marker + " [repo-mirror:initializing:v1]"
    srcbase = source_api_base(source)
    configured_name = name
    source_name = name
    upstream = f"{srcbase}/repos/{quote(source['owner'], safe='')}/{quote(source_name, safe='')}"
    if "destination_id" in settings and (not isinstance(repo, dict) or
            type(repo.get("id")) is not int or repo["id"] != settings["destination_id"]):
        raise ValueError("destination repository id mismatch or missing")
    if source["type"] == "gitlab":
        if "source_id" not in settings:
            raise ValueError("GitLab source repository id pin required")
        src = GitLabSourceAdapter(source["base_url"], source["owner"] + "/" + source_name, settings["source_id"],
                                  token=os.environ.get("MIRROR_GITLAB_TOKEN"))
        upstream = src.base
        project = src.project()
        metadata = {**project, "clone_url": project.get("http_url_to_repo")}
    else:
        metadata = src.request("GET", upstream)
    if "source_id" in settings and (type(metadata.get("id")) is not int or metadata["id"] != settings["source_id"]):
        raise ValueError("source repository id mismatch")
    relocation = None
    original_source = source
    try:
        clone_url = validate_source_clone_url(source, source_name, metadata)
    except ValueError:
        if source['type'] != 'github' or not {'source_id', 'destination_id'} <= settings.keys() or source['repos'] == 'all':
            raise
        from repo_mirror.sources import verified_source_identity
        try:
            canonical = verified_source_identity(source, source_name, settings['source_id'], metadata)
        except ValueError:
            raise ValueError('source clone URL does not match verified source identity') from None
        canonical_owner, canonical_name = urlparse(canonical).path.strip('/').split('/')
        moved_source = {**source, 'owner': canonical_owner}
        clone_url = validate_source_clone_url(moved_source, canonical_name, metadata)
        if (canonical_owner.lower(), canonical_name.lower()) == (source['owner'].lower(), source_name.lower()):
            raise ValueError('source clone URL does not match source identity')
        # The pinned source ID still resolves, so the same repository answered
        # under a new canonical name. Mirror into the *existing* destination
        # using the original provenance marker and recovery binding, and report
        # the move so the batch can update configuration afterwards.
        relocation = {
            'source_origin': srcbase.rstrip('/'),
            'source_id': settings['source_id'],
            'original_owner': source['owner'],
            'original_name': source_name,
            'owner': canonical_owner,
            'name': canonical_name,
            'destination': {'profile': source.get('destination_profile'),
                            'owner': owner, 'name': dest_name,
                            'id': settings['destination_id']},
        }
        print(
            f"source relocation detected source_id={settings['source_id']} target=destination",
            file=sys.stderr,
            flush=True,
        )
        source = moved_source
        source_name = canonical_name
        upstream = f"{srcbase}/repos/{quote(source['owner'], safe='')}/{quote(source_name, safe='')}"
    def recheck_pins():
        # These API reads narrow, but cannot eliminate, the TOCTOU window.
        if "source_id" in settings:
            current_source = src.project() if source["type"] == "gitlab" else src.request("GET", upstream)
            if (not isinstance(current_source, dict) or type(current_source.get("id")) is not int
                    or current_source["id"] != settings["source_id"]):
                raise ValueError("source repository id mismatch")
            if source["type"] == "gitlab":
                validate_source_clone_url(source, source_name, {"clone_url": current_source.get("http_url_to_repo")})
        if "destination_id" in settings:
            current = get_existing(dst, target)
            if (not isinstance(current, dict) or type(current.get("id")) is not int
                    or current["id"] != settings["destination_id"]):
                raise ValueError("destination repository id mismatch or missing")
            check_existing(current, marker)

    recheck_pins()
    if repo is None:
        payload = {
            "name": dest_name,
            "private": source.get("destinations", {}).get(name.lower(), {}).get("visibility", "private") != "public",
            "auto_init": False,
            "description": marker + " [repo-mirror:initializing:v1]",
        }
        if account_type == "personal" and isinstance(dst, GitHubAPI):
            repo = dst.create_personal_repository(owner, payload)
        else:
            if account_type == "personal":
                identity = dst.request("GET", GITHUB + "/user")
                login = identity.get("login") if isinstance(identity, dict) else None
                if not isinstance(login, str) or not login or login.lower() != owner.lower():
                    raise RuntimeError("authenticated GitHub user does not match configured destination")
            endpoint = "/user/repos" if account_type == "personal" else f"/orgs/{qowner}/repos"
            repo = dst.request("POST", GITHUB + endpoint, payload)
        if repo.get("full_name", "").lower() != (owner + "/" + dest_name).lower():
            raise RuntimeError("created repository owner does not match configured destination")
    dst.request("PUT", target + "/actions/permissions", {"enabled": False})
    default_branch = metadata.get("default_branch")

    def update_default():
        if not default_branch:
            raise RuntimeError("source default branch missing; refusing to prune")
        if repo.get("default_branch") != default_branch:
            dst.request("PATCH", target, {"default_branch": default_branch})

    expected_default = repo.get("default_branch")

    def switch_default(ref, *, initial_refs=None):
        nonlocal expected_default
        branch = ref.removeprefix("refs/heads/")
        current = dst.request("GET", target)
        actual = current.get("default_branch")
        # Only an observed empty destination can have its default selected by
        # the first push, and the selection must be among those pushed branches.
        selected_on_first_push = (
            initial_refs is not None
            and isinstance(actual, str)
            and "refs/heads/" + actual in initial_refs
        )
        if actual != expected_default and not selected_on_first_push:
            raise RuntimeError("destination default branch changed; refusing to switch")
        if actual != branch:
            dst.request("PATCH", target, {"default_branch": branch})
            if dst.request("GET", target).get("default_branch") != branch:
                raise RuntimeError("destination default branch change not verified")
        expected_default = branch

    def archive_factory():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        archive = create_archive(dst, owner, dest_name, account_type, stamp)
        archive_path = archive.removeprefix("https://github.com/").removesuffix(".git")
        archive_target = GITHUB + "/repos/" + archive_path
        return archive, lambda ref: set_archive_default(dst, archive_target, ref)

    recheck_pins()
    with timed("step git mirror"):
        mirror_git(
            clone_url,
            f"https://github.com/{qowner}/{qname}.git",
            token,
            update_default,
            safety={
                "source_default": default_branch,
                "dest_default": repo.get("default_branch"),
                "archive_factory": archive_factory,
                "switch_default": switch_default,
            },
        )
    recheck_pins()
    if "source_id" in settings or "destination_id" in settings:
        # Keep the raw destination captured by recheck_pins to avoid recursion.
        raw_dst = dst
        def check_release_pins():
            if "source_id" in settings:
                current_source = src.project() if source["type"] == "gitlab" else src.request("GET", upstream)
                if (not isinstance(current_source, dict) or type(current_source.get("id")) is not int
                        or current_source["id"] != settings["source_id"]):
                    raise ValueError("source repository id mismatch")
                if source["type"] == "gitlab":
                    validate_source_clone_url(source, source_name, {"clone_url": current_source.get("http_url_to_repo")})
            if "destination_id" in settings:
                current = get_existing(raw_dst, target)
                if (not isinstance(current, dict) or type(current.get("id")) is not int
                        or current["id"] != settings["destination_id"]):
                    raise ValueError("destination repository id mismatch or missing")
                check_existing(current, marker)
        dst = _IdentityGuardedDestination(raw_dst, check_release_pins)
    if "legacy" in settings:
        historical_target = f"{GITHUB}/repos/{qowner}/{quote(legacy['destination_name'], safe='')}"
        mirror_releases(source, source_name, owner, src, dst, upstream, target,
                        source_marker(historical_source, historical_name), initializing=initializing, journal=journal,
                        recovery_target=historical_target, journal_source=historical_source, journal_name=historical_name,
                        settings_name=configured_name)
    else:
        mirror_releases(source, source_name, owner, src, dst, upstream, target, marker,
                        initializing=initializing, journal=journal, settings_name=configured_name,
                        journal_source=original_source if relocation else None,
                        journal_name=configured_name if relocation else None)
    if initializing:
        dst.request("PATCH", target, {"description": marker})
    return relocation


def valid_source_release_tag(release):
    return (isinstance(release, dict) and isinstance(release.get("tag_name"), str)
            and bool(release["tag_name"]))


def validate_source_draft_tag(release):
    if release.get("draft") is True and release["tag_name"].startswith("untagged-"):
        raise ValueError("source draft has untagged synthetic tag; manual reconciliation required")


def validate_unique_source_tags(releases):
    if len({r["tag_name"] for r in releases}) != len(releases):
        raise ValueError("duplicate source release tag")


def validate_source_release_listing(releases, *, allow_empty_target=False):
    """Strict preflight of the complete source snapshot before destination writes."""
    if not isinstance(releases, list):
        raise ValueError("invalid source release listing")
    for release in releases:
        validate_source_release_metadata(release, allow_empty_target=allow_empty_target)
        if not {"name", "body", "draft", "prerelease"} <= release.keys():
            raise ValueError("incomplete source release metadata")
    validate_unique_source_tags(releases)


def validate_source_release_metadata(release, *, allow_empty_target=False):
    if not valid_source_release_tag(release):
        raise ValueError("invalid source release tag")
    if type(release.get("id")) is not int or release["id"] <= 0:
        raise ValueError("invalid source release id")
    if "name" in release and release["name"] is not None and not isinstance(release["name"], str):
        raise ValueError("invalid source release name")
    if any(key in release and type(release[key]) is not bool for key in ("draft", "prerelease")):
        raise ValueError("invalid source release flags")
    target = release.get("target_commitish")
    if target is not None and (not isinstance(target, str) or (not target and not allow_empty_target)):
        raise ValueError("invalid source release target")
    validate_source_draft_tag(release)
    validate_source_release_markers(release)


def verify_forgejo_empty_target_tag(src, dst, upstream, target, tag):
    """Require identical existing tag objects; never allow a default-branch tag."""
    verify = getattr(dst, "verify_source_tag", None)
    if callable(verify):
        return verify(tag)
    if getattr(dst, "supports_target_commitish", False):
        raise ValueError("cannot verify destination tag for empty source target")
    ref = "refs/tags/" + tag
    source_refs = src.request("GET", upstream + "/git/refs/tags/" + quote(tag, safe=""))
    dest_ref = dst.request("GET", target + "/git/ref/tags/" + quote(tag, safe=""))
    if not isinstance(source_refs, list):
        raise ValueError("source tag verification failed")
    matching = [item for item in source_refs if isinstance(item, dict) and item.get("ref") == ref]
    if len(matching) != 1 or not isinstance(dest_ref, dict) or dest_ref.get("ref") != ref:
        raise ValueError("tag verification failed")
    source_object, dest_object = matching[0].get("object"), dest_ref.get("object")
    if not isinstance(source_object, dict) or not isinstance(dest_object, dict):
        raise ValueError("tag verification failed")
    sha = source_object.get("sha")
    if (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha)
            or source_object.get("type") not in ("tag", "commit")
            or dest_object.get("sha") != sha or dest_object.get("type") != source_object.get("type")):
        raise ValueError("tag verification failed")
    return (source_object["type"], sha)


def fetch_source_latest(src, upstream):
    try:
        latest = src.request("GET", upstream + "/releases/latest")
        if (not isinstance(latest, dict) or not latest.get("tag_name")
                or latest.get("draft") or latest.get("prerelease")):
            raise ValueError("invalid source latest release")
        return latest
    except HTTPError as exc:
        if not not_found(exc):
            raise
        exc.close()
        return None


def select_tag_source_latest(releases):
    """Choose by release time, rejecting absent or ambiguous timestamps."""
    if not releases:
        return None
    timed_releases = []
    for release in releases:
        try:
            stamp = datetime.fromisoformat(release["released_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError()
        except (KeyError, AttributeError, TypeError, ValueError):
            raise ValueError("invalid source release timestamp") from None
        timed_releases.append((stamp.astimezone(UTC), release))
    newest = max(stamp for stamp, _ in timed_releases)
    matches = [release for stamp, release in timed_releases if stamp == newest]
    if len(matches) != 1:
        raise ValueError("ambiguous source latest release")
    return matches[0]


def validate_source_latest(latest, releases, *, allow_empty_target=False):
    if latest is None:
        return
    validate_source_release_metadata(latest, allow_empty_target=allow_empty_target)
    if not {"name", "body", "draft", "prerelease"} <= latest.keys():
        raise ValueError("incomplete source release metadata")
    if latest["draft"] or latest["prerelease"]:
        raise ValueError("invalid source latest release")
    matches = [r for r in releases if r["id"] == latest["id"] and r["tag_name"] == latest["tag_name"]]
    if len(matches) != 1 or matches[0]["draft"] or matches[0]["prerelease"]:
        raise ValueError("source latest release not eligible in listing")


class ReleaseAggregateError(RuntimeError):
    """Failures collected while processing distinct source releases."""

    def __init__(self, errors):
        super().__init__(f"{len(errors)} releases failed")
        self.release_errors = tuple(errors)


def logical_release_body(body):
    """Normalize only cross-provider comparisons, never snapshot evidence."""
    return "" if body is None or body == " " else body


def verify_empty_body_converged(src, dst, upstream, target, release, *, empty_forgejo_target=False):
    """Prove a published empty-body release already matches, using reads only.

    Return the pinned destination ID, or None whenever evidence is insufficient.
    Both sides are downloaded twice to detect byte drift without trusting digests.
    Final reads are not atomic; concurrent changes after a read can still race.
    """
    try:
        def valid_id(value):
            return type(value) is int and value > 0

        def metadata(item):
            if not isinstance(item, dict) or not valid_id(item.get("id")):
                raise ValueError()
            tag, title = item.get("tag_name"), item.get("name")
            if (not {"body", "name", "draft", "prerelease", "target_commitish"} <= item.keys() or
                    not isinstance(tag, str) or not tag or
                    title is not None and not isinstance(title, str) or
                    item.get("body") not in (None, "", " ") or
                    item.get("draft") is not False or type(item.get("prerelease")) is not bool or
                    not isinstance(item.get("target_commitish"), str) or
                    (not item["target_commitish"] and not empty_forgejo_target)):
                raise ValueError()
            return (item["id"], tag, title, item.get("body"), item["draft"],
                    item["prerelease"], item["target_commitish"])

        validate_source_release_markers(release)
        initial = metadata(release)
        tag = release["tag_name"]
        source_url = upstream + "/releases/" + str(release["id"])
        dest = dst.request("GET", target + "/releases/tags/" + quote(tag, safe=""))
        dest_meta = metadata(dest)
        if (dest_meta[1] != tag or
                (dest_meta[2] != initial[2] and not (initial[2] in (None, "") and dest_meta[2] == tag)) or
                logical_release_body(dest_meta[3]) != logical_release_body(initial[3]) or
                dest_meta[4:6] != initial[4:6] or
                (not empty_forgejo_target and dest_meta[6] != initial[6])):
            return None
        dest_url = target + "/releases/" + str(dest["id"])

        def release_lists():
            source_list = list(src.pages(upstream + "/releases"))
            if any(not valid_source_release_tag(item) or not valid_id(item.get("id"))
                   for item in source_list):
                raise ValueError()
            validate_unique_source_tags(source_list)
            if len({item["id"] for item in source_list}) != len(source_list):
                raise ValueError()
            dest_list = list(dst.pages(target + "/releases"))
            if (any(not isinstance(item, dict) or not valid_id(item.get("id")) or
                    not isinstance(item.get("tag_name"), str) or not item["tag_name"]
                    for item in dest_list) or
                    len({item["id"] for item in dest_list}) != len(dest_list) or
                    len({item["tag_name"] for item in dest_list}) != len(dest_list)):
                raise ValueError()
            matches = [item for item in dest_list if isinstance(item, dict) and item.get("tag_name") == tag]
            if (len(matches) != 1 or metadata(matches[0]) != dest_meta or
                    any(not isinstance(item, dict) or
                        (item.get("draft") is True and str(item.get("tag_name", "")).startswith("untagged-"))
                        for item in dest_list)):
                raise ValueError()
            source_matches = [item for item in source_list if isinstance(item, dict) and item.get("id") == initial[0]]
            if len(source_matches) != 1 or metadata(source_matches[0]) != initial:
                raise ValueError()
            def stable_listing(items):
                def fields(item, keys):
                    return tuple((key, key in item, item.get(key)) for key in keys)
                result = {}
                for item in items:
                    nested = item.get("assets")
                    if nested is not None and not isinstance(nested, list):
                        raise ValueError()
                    asset_fields = None
                    if nested is not None:
                        if any(not isinstance(asset, dict) for asset in nested):
                            raise ValueError()
                        asset_fields = sorted((fields(asset, ("id", "name", "size", "state", "digest"))
                                               for asset in nested), key=repr)
                    result[item["id"]] = (fields(item, ("id", "tag_name", "name", "body", "draft",
                                                         "prerelease", "target_commitish")), asset_fields)
                return result
            return stable_listing(source_list), stable_listing(dest_list)

        initial_lists = release_lists()

        def assets(adapter, url):
            result = {}
            ids = set()
            for asset in adapter.pages(url + "/assets"):
                if (not isinstance(asset, dict) or not valid_id(asset.get("id")) or
                        asset["id"] in ids or not isinstance(asset.get("name"), str) or not asset["name"] or
                        asset["name"] in result or asset.get("state") != "uploaded" or
                        type(asset.get("size")) is not int or asset["size"] < 0 or
                        not isinstance(asset.get("url"), str) or not asset["url"]):
                    raise ValueError()
                ids.add(asset["id"])
                result[asset["name"]] = dict(asset)
            return result

        def snapshot(items):
            return {name: (a["id"], a["name"], a["size"], a["state"], a.get("digest"))
                    for name, a in items.items()}

        def digest(adapter, asset):
            with tempfile.TemporaryFile() as data:
                if adapter is src and callable(getattr(adapter, "download_verified_asset", None)):
                    adapter.download_verified_asset(asset, data)
                else:
                    adapter.download(asset["url"], data)
                if data.tell() != asset["size"]:
                    raise ValueError()
                data.seek(0)
                hashed = hashlib.sha256()
                for chunk in iter(lambda: data.read(CHUNK_SIZE), b""):
                    hashed.update(chunk)
                published = asset.get("digest")
                if adapter is src and isinstance(published, str) and published.lower().startswith("sha256:"):
                    if (not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", published) or
                            hashed.hexdigest() != published[7:].lower()):
                        raise ValueError()
                return hashed.digest()

        source_assets, dest_assets = assets(src, source_url), assets(dst, dest_url)
        if source_assets.keys() != dest_assets.keys():
            return None
        hashes = {}
        for name, item in source_assets.items():
            if item["size"] != dest_assets[name]["size"]:
                return None
            hashes[name] = digest(src, item)
            if hashes[name] != digest(dst, dest_assets[name]):
                return None
        if (metadata(src.request("GET", source_url)) != initial or
                metadata(dst.request("GET", dest_url)) != dest_meta or
                metadata(dst.request("GET", target + "/releases/tags/" + quote(tag, safe=""))) != dest_meta):
            return None
        source_again, dest_again = assets(src, source_url), assets(dst, dest_url)
        if snapshot(source_assets) != snapshot(source_again) or snapshot(dest_assets) != snapshot(dest_again):
            return None
        for name in source_assets:
            if digest(src, source_again[name]) != hashes[name] or digest(dst, dest_again[name]) != hashes[name]:
                return None
        if (metadata(src.request("GET", source_url)) != initial or
                metadata(dst.request("GET", dest_url)) != dest_meta or
                metadata(dst.request("GET", target + "/releases/tags/" + quote(tag, safe=""))) != dest_meta or
                snapshot(assets(src, source_url)) != snapshot(source_assets) or
                snapshot(assets(dst, dest_url)) != snapshot(dest_assets) or
                release_lists() != initial_lists):
            return None
        return dest["id"]
    except Exception:
        return None


def mirror_releases(source, name, owner, src, dst, upstream, target, marker, *, initializing, journal=None,
                    validate_snapshot=None, recovery_target=None, journal_source=None, journal_name=None,
                    journal_identity=None, settings_name=None):
    """Apply the release policy independently of repository and Git setup."""
    # Tag-identity providers implement list_releases(),
    # get_release(tag), list_release_assets(tag), and
    # verify_release_tag(tag, destination, target). The last method must compare
    # the actual source and destination tag objects and return their identity.
    tag_identity = getattr(src, "release_identity_kind", None) == "tag"
    if tag_identity:
        scope = getattr(src, "release_scope", None)
        if (not isinstance(scope, tuple) or len(scope) != 2 or
                not isinstance(scope[0], str) or not scope[0] or
                type(scope[1]) is not int or scope[1] <= 0):
            raise ValueError("invalid source release scope")
        marker = json.dumps([marker, scope[0], scope[1]], ensure_ascii=True, separators=(",", ":"))
    recovery_target = recovery_target or target
    journal_source = journal_source or source
    journal_name = journal_name or name
    settings = source.get("destinations", {}).get((settings_name or name).lower(), {})
    if journal_identity is None and {"source_id", "destination_id"} <= settings.keys():
        parsed_target = urlparse(target)
        journal_identity = (source["base_url"], settings["source_id"],
                            f"{parsed_target.scheme}://{parsed_target.netloc}", settings["destination_id"])

    def failure_keys(tag, asset):
        legacy = journal.key(journal_source, journal_name, owner, tag, asset)
        stable = journal.id_key(*journal_identity, tag, asset) if journal_identity else legacy
        return legacy, stable

    def failed_release(tag, failures):
        prefixes = {key[:64] for key in failure_keys(tag, "")}
        return any(key[:64] in prefixes for key in failures)

    def asset_key(tag, asset):
        legacy, stable = failure_keys(tag, asset)
        if legacy != stable:
            journal.migrate_failure(legacy, stable)
        return stable

    if source.get("type") == "github" and isinstance(src, GitHubSourceAPI):
        src = ForgejoSourceAdapter(source["owner"] + "/" + name, src.source_token, api=src)
        if upstream != src.base:
            raise ValueError("source API URL does not match source identity")
    with timed("step release-discovery target=source"):
        releases = src.list_releases() if tag_identity else src.pages(upstream + "/releases")
    if validate_snapshot is not None:
        latest = select_tag_source_latest(releases) if tag_identity else fetch_source_latest(src, upstream)
        validate_snapshot(releases, latest)
    invalid_releases = [
        r
        for r in releases
        if not valid_source_release_tag(r)
    ]
    if invalid_releases:
        print("release failed: invalid source release tag", file=sys.stderr, flush=True)
        releases = [
            r
            for r in releases
            if valid_source_release_tag(r)
        ]
    if validate_snapshot is None:
        latest = select_tag_source_latest(releases) if tag_identity else fetch_source_latest(src, upstream)

    destination_releases = None
    known_draft_tags = {}

    def candidates_for_tag(tag):
        return [item for item in get_destination_releases()
                if item.get("id") not in known_draft_tags or known_draft_tags[item["id"]] == tag]

    def get_destination_releases():
        nonlocal destination_releases
        if destination_releases is None:
            destination_releases = list(dst.pages(target + "/releases"))
            # Signatures do not expose the tag: test each complete source tag
            # against each untagged destination draft exactly once per snapshot.
            for item in destination_releases:
                if not (item.get("draft") is True and isinstance(item.get("tag_name"), str)
                        and item["tag_name"].startswith("untagged-")
                        and type(item.get("id")) is int and item["id"] > 0):
                    continue
                authenticated = []
                for source_release in releases:
                    source_tag = source_release["tag_name"]
                    if source_tag == item["tag_name"]:
                        continue
                    try:
                        if find_tag_releases(None, target, source_tag, releases=[item], marker=marker, recovery_target=recovery_target):
                            authenticated.append(source_tag)
                    except ValueError:
                        pass
                if len(authenticated) == 1:
                    known_draft_tags[item["id"]] = authenticated[0]
        return destination_releases
    validate_unique_source_tags(releases)
    if initializing or len(releases) <= 1:
        selected = list(releases)
    else:
        failures = journal.failures if journal else set()
        failed_tags = {
            r["tag_name"]
            for r in releases
            if journal and failed_release(r["tag_name"], failures)
        }
        selected = select_existing_releases(
            releases, get_destination_releases(), failed_tags, marker, target, known_draft_tags, recovery_target
        )
    if latest:
        selected = [latest if r["tag_name"] == latest["tag_name"] else r for r in selected]
        if not any(r["tag_name"] == latest["tag_name"] for r in selected):
            selected.append(latest)
    if initializing:
        # Both APIs return release lists newest first. Use timestamps when all
        # are present, otherwise preserve the reverse of their list order.
        selected.reverse()
        try:

            def created_at(release):
                value = datetime.fromisoformat(
                    (release.get("published_at") or release["created_at"]).replace("Z", "+00:00")
                )
                if value.tzinfo is None:
                    raise ValueError("missing timezone")
                return value.astimezone(UTC)

            selected.sort(key=created_at)
        except KeyError, AttributeError, TypeError, ValueError:
            pass
    latest_id = None
    latest_read_only = False
    selected_tags = {r["tag_name"] for r in selected}
    skipped = sum(1 for release in releases if release["tag_name"] not in selected_tags)
    print(
        f"releases discovered={len(releases)} selected={len(selected)} skipped={skipped}",
        file=sys.stderr,
        flush=True,
    )
    release_errors: list[Exception] = [ValueError("invalid source release tag")] if invalid_releases else []
    for release_index, release in enumerate(selected, 1):
        tag = release["tag_name"]
        print(
            f"\n\n===== Release {release_index}/{len(selected)}: {safe_label(tag)} =====",
            file=sys.stderr,
            flush=True,
        )
        print(
            f"release {release_index}/{len(selected)} started tag={safe_label(tag)}",
            file=sys.stderr,
            flush=True,
        )
        try:
            with timed(f"release tag={safe_label(tag)}"):
                validate_source_draft_tag(release)
                metadata_keys = (("tag_name", "body", "name", "target_commitish", "draft", "prerelease", "released_at")
                                 if tag_identity else ("id", "tag_name", "body", "name", "target_commitish", "draft", "prerelease"))
                release_id = release.get("id")
                if not tag_identity and (type(release_id) is not int or release_id <= 0):
                    raise ValueError("source release changed during sync")
                baseline = src.get_release(tag) if tag_identity else src.request("GET", upstream + "/releases/" + str(release_id))
                listed = next((item for item in releases if item["tag_name"] == tag), release)
                if (not isinstance(baseline, dict) or
                        any(key not in baseline or type(baseline[key]) is not type(snapshot[key]) or
                            baseline[key] != snapshot[key]
                            for snapshot in (listed, release) for key in metadata_keys if key in snapshot) or
                        (not tag_identity and baseline.get("id") != release_id) or baseline.get("tag_name") != tag):
                    raise ValueError("source release changed during sync")
                allow_empty_target = source.get("type") == "forgejo"
                if tag_identity:
                    if ("id" in baseline or not {"name", "body", "draft", "prerelease", "released_at"} <= baseline.keys()
                            or type(baseline["draft"]) is not bool or type(baseline["prerelease"]) is not bool
                            or (baseline["name"] is not None and not isinstance(baseline["name"], str))
                            or (baseline.get("target_commitish") is not None and
                                (not isinstance(baseline["target_commitish"], str) or not baseline["target_commitish"]))):
                        raise ValueError("invalid tag release metadata")
                    validate_source_release_markers(baseline)
                    original_tag_object = src.verify_release_tag(tag, dst, target)
                    if not original_tag_object:
                        raise ValueError("tag verification failed")
                    from repo_mirror import gitlab_assets
                    from repo_mirror.constants import MAX_ASSET_SIZE
                    from repo_mirror.forgejo_destination_adapter import ForgejoDestinationAdapter
                    links = src.list_release_assets(tag)
                    if not isinstance(links, list):
                        raise ValueError("invalid source link assets")
                    cap = MAX_ASSET_SIZE
                    if links and isinstance(dst, ForgejoDestinationAdapter) and dst.supports_target_commitish:
                        raw_cap = os.environ.get("MIRROR_FORGEJO_ASSET_MAX_BYTES", "")
                        if not re.fullmatch(r"[0-9]+", raw_cap):
                            raise ValueError("Forgejo asset byte limit must be configured")
                        cap = int(raw_cap)
                    # Validate identities before sorting; GitLab does not promise list order.
                    normalized_links = []
                    for link in links:
                        if not isinstance(link, dict) or type(link.get("id")) is not int or link["id"] <= 0:
                            raise ValueError("invalid source link assets")
                        link = dict(link, link_type=link.get("link_type", "other"))
                        rendered = gitlab_assets.render_preserved_link(link)
                        normalized_links.append((link, rendered))
                    normalized_links.sort(key=lambda item: item[0]["id"])
                    link_snapshot = [[link["id"], link["name"], link["url"], link["link_type"]]
                                     for link, _ in normalized_links]
                    if (len({x[0] for x in link_snapshot}) != len(link_snapshot) or
                            len({x[1] for x in link_snapshot}) != len(link_snapshot)):
                        raise ValueError("invalid source link assets")
                    link_assets, preserved_links = [], []
                    for link, rendered in normalized_links:
                        with gitlab_assets.download_asset(link["url"], cap) as downloaded:
                            if downloaded.oversize:
                                preserved_links.append(rendered)
                                link_assets.append((link, None, None))
                            else:
                                h = hashlib.sha256()
                                for chunk in iter(lambda: downloaded.file.read(CHUNK_SIZE), b""):
                                    h.update(chunk)
                                link_assets.append((link, downloaded.size, h.hexdigest()))
                else:
                    validate_source_release_metadata(baseline, allow_empty_target=allow_empty_target)
                release = baseline
                validate_source_draft_tag(release)
                source_metadata = {key: (key in release, release.get(key)) for key in metadata_keys}
                empty_forgejo_target = allow_empty_target and release.get("target_commitish") == ""
                if empty_forgejo_target:
                    original_tag_object = verify_forgejo_empty_target_tag(src, dst, upstream, target, tag)
                validate_release_source = getattr(dst, "validate_release_source", None)
                if callable(validate_release_source):
                    if getattr(dst, "verify_empty_body_read_only", False) and release.get("body") in (None, "", " "):
                        failed = journal and failed_release(tag, journal.failures)
                        converged = None if failed else verify_empty_body_converged(
                            src, dst, upstream, target, release, empty_forgejo_target=empty_forgejo_target)
                        if converged is not None:
                            # The empty target gives no commitish to compare; pin the
                            # actual tag object across the entire read-only proof.
                            if empty_forgejo_target and verify_forgejo_empty_target_tag(
                                    src, dst, upstream, target, tag) != original_tag_object:
                                raise ValueError("source tag changed during sync")
                            if latest and tag == latest["tag_name"]:
                                latest_id = converged
                                latest_read_only = True
                            continue
                    validate_release_source(release)
                try:
                    existing = dst.request("GET", target + "/releases/tags/" + quote(tag, safe=""))
                except HTTPError as e:
                    if not not_found(e):
                        raise
                    e.close()
                    # The tag endpoint can omit drafts, which still need to be found.
                    matches = find_tag_releases(dst, target, tag, release.get("name"), releases=candidates_for_tag(tag), marker=marker, recovery_target=recovery_target)
                    if len(matches) > 1:
                        raise RuntimeError("multiple destination releases for tag")
                    existing = matches[0] if matches else None
                if existing and release.get("draft") is True:
                    # An update can turn an exact-tag release into an untagged
                    # draft; do not trust the discovery snapshot afterwards.
                    if type(existing.get("id")) is int and existing["id"] > 0:
                        known_draft_tags[existing["id"]] = tag
                    cached = dict(existing, draft=True, tag_name="untagged-updated")
                    snapshot = get_destination_releases()
                    if type(existing.get("id")) is int and existing["id"] > 0:
                        snapshot[:] = [item for item in snapshot if item.get("id") != existing["id"]]
                    snapshot.append(cached)
                prefix_v1 = "[repo-mirror:pending:v1]"
                prefix_v2 = "[repo-mirror:pending:v2]"
                current_name = existing.get("name") if existing else None
                pending = isinstance(current_name, str) and current_name.startswith((prefix_v1, prefix_v2))
                if tag_identity and pending:
                    raise ReleaseRecoveryError("marker")
                prefix = prefix_v2 if pending and current_name.startswith(prefix_v2) else prefix_v1
                sentinel = "[repo-mirror:pending:v3]"
                secret = os.environ.get("MIRROR_TIMESTAMP_SECRET", "")
                if not pending and not secret:
                    raise ReleaseRecoveryError("secret")

                def marker_signature(marker_state, release_id):
                    payload = {
                        "version": "v3",
                        "source": marker,
                        "tag": tag,
                        "target": recovery_target,
                        "release_id": release_id,
                        "state": marker_state,
                    }
                    encoded = json.dumps(
                        payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")
                    ).encode()
                    return hmac.new(
                        secret.encode(), b"repo-mirror:release-recovery:v3\0" + encoded, hashlib.sha256
                    ).hexdigest()

                validate_source_release_markers(release)
                # Preserve raw source values; destination body equivalence is not drift equivalence.
                source_body = release.get("body")
                source_name = release.get("name")
                authenticated_draft = False
                body = existing.get("body") if existing else None
                if isinstance(body, str) and IDENTITY_SENTINEL in body:
                    if body.count(IDENTITY_SENTINEL) != 1 or "\n" + IDENTITY_SENTINEL not in body:
                        raise ReleaseRecoveryError("marker")
                    body, signature = body.split("\n" + IDENTITY_SENTINEL, 1)
                    expected = draft_identity_signature(secret, marker, tag, recovery_target, existing["id"])
                    if not hmac.compare_digest(signature, expected):
                        raise ReleaseRecoveryError("marker")
                    authenticated_draft = True
                if body is not None and not isinstance(body, str):
                    raise ReleaseRecoveryError("marker")
                state = None
                if body is not None and sentinel in body:
                    if pending or body.count(sentinel) != 1 or "\n" + sentinel not in body:
                        raise ReleaseRecoveryError("marker")
                    body, encoded_state = body.split("\n" + sentinel, 1)
                    try:

                        def unique_pairs(pairs):
                            result = {}
                            for key, value in pairs:
                                if key in result:
                                    raise ValueError()
                                result[key] = value
                            return result

                        state = json.loads(encoded_state, object_pairs_hook=unique_pairs)
                        if not isinstance(state, dict) or set(state) not in (
                            {"phase", "digest", "replace", "signature"},
                            {"phase", "digest", "replace", "signature", "skipped"},
                        ):
                            raise ValueError()
                        signature = state.pop("signature")
                        if "skipped" in state:
                            skipped = state["skipped"]
                            if (
                                not isinstance(skipped, list)
                                or any(
                                    not isinstance(x, dict)
                                    or set(x) != {"name", "id"}
                                    or not isinstance(x["name"], str)
                                    or not x["name"]
                                    or (x["id"] is not None and (type(x["id"]) is not int or x["id"] <= 0))
                                    for x in skipped
                                )
                                or len({x["name"] for x in skipped}) != len(skipped)
                            ):
                                raise ValueError()
                        if (
                            not isinstance(signature, str)
                            or not re.fullmatch(r"[0-9a-f]{64}", signature)
                            or not (
                                hmac.compare_digest(signature, marker_signature(state, existing["id"]))
                                or (
                                    state["phase"] == "WAITING_SOURCE"
                                    and state["replace"] is False
                                    and hmac.compare_digest(signature, marker_signature(state, None))
                                )
                            )
                            or type(state["replace"]) is not bool
                            or not (
                                (state["phase"] == "WAITING_SOURCE" and state["digest"] is None)
                                or (
                                    state["phase"] == "BOUND"
                                    and isinstance(state["digest"], str)
                                    and re.fullmatch(r"[0-9a-f]{64}", state["digest"])
                                )
                            )
                        ):
                            raise ValueError()
                    except ValueError, TypeError:
                        raise ReleaseRecoveryError("marker") from None
                    authenticated_draft = True

                def marked_body(base, marker_state):
                    signed_state = dict(
                        marker_state,
                        signature=marker_signature(marker_state, existing["id"] if existing else None),
                    )
                    result = (
                        (base or "")
                        + "\n"
                        + sentinel
                        + json.dumps(signed_state, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
                    )
                    if len(result) > 125000:
                        raise ValueError("release body too long for pending marker")
                    return result

                if not pending and state is None:
                    state = {
                        "phase": "WAITING_SOURCE",
                        "digest": None,
                        "replace": existing is not None,
                        "skipped": [],
                    }
                    marked = marked_body(body if existing else source_body, state)
                    if existing:
                        dst.request("PATCH", target + "/releases/" + str(existing["id"]), {"body": marked})
                    else:
                        if empty_forgejo_target:
                            if verify_forgejo_empty_target_tag(src, dst, upstream, target, tag) != original_tag_object:
                                raise ValueError("tag verification failed")
                        fields = {k: release.get(k) for k in ("draft", "prerelease")}
                        fields.update(tag_name=tag, name=source_name, body=marked, make_latest="false")
                        if getattr(dst, "supports_target_commitish", False) and release.get("target_commitish") is not None:
                            fields["target_commitish"] = release["target_commitish"]
                        try:
                            existing = dst.request("POST", target + "/releases", fields)
                            if isinstance(existing, dict):
                                # Track a response-bound identity for the rest of this run.
                                cached = dict(existing)
                                if cached.get("draft") is True or fields.get("draft") is True:
                                    cached.update(draft=True, tag_name="untagged-created")
                                    if type(existing.get("id")) is int and existing["id"] > 0:
                                        known_draft_tags[existing["id"]] = tag
                                get_destination_releases().append(cached)
                        except HTTPError as exc:
                            if getattr(exc, "mirror_operation", None) == ("destination", "API", "POST"):
                                setattr(exc, "mirror_operation", ("destination", "release", "create"))
                            raise
                        body = source_body
                        if (
                            not isinstance(existing, dict)
                            or type(existing.get("id")) is not int
                            or existing["id"] <= 0
                        ):
                            raise ReleaseRecoveryError("marker")
                        authenticated_draft = True
                        # A create response supplies the ID; bind before inspecting or mutating assets.
                        dst.request(
                            "PATCH",
                            target + "/releases/" + str(existing["id"]),
                            {"body": marked_body(body, state)},
                        )
                elif (
                    not pending
                    and state is not None
                    and hmac.compare_digest(signature, marker_signature(state, None))
                ):
                    # Provisional markers never authorize replacement. Bind only an
                    # unambiguous tag lookup to the immutable destination release ID.
                    matches = find_tag_releases(dst, target, tag, release.get("name"), releases=candidates_for_tag(tag), marker=marker, recovery_target=recovery_target)
                    if (
                        type(existing.get("id")) is not int
                        or existing["id"] <= 0
                        or len(matches) != 1
                        or matches[0].get("id") != existing["id"]
                    ):
                        raise ReleaseRecoveryError("marker")
                    dst.request(
                        "PATCH",
                        target + "/releases/" + str(existing["id"]),
                        {"body": marked_body(body, state)},
                    )
                # Legacy name markers are unsigned and cannot authorize deletion.
                may_replace = False if pending else state["replace"]
                asset_url = None if tag_identity else upstream + "/releases/" + str(release["id"]) + "/assets"
                observed_stable_snapshot = None
                all_source_names = set()
                observed_starter_names = set()

                @timed_call(lambda *args, **kwargs: "step source-manifest target=source")
                def fetch_source_assets(check_bytes=False):
                    nonlocal observed_stable_snapshot, all_source_names, state
                    assets = src.list_release_assets(tag) if tag_identity else (
                        src.request("GET", asset_url) if source["type"] == "forgejo" else src.pages(asset_url)
                    )
                    if tag_identity:
                        if not isinstance(assets, list):
                            raise ValueError("invalid source link assets")
                        try:
                            snapshot = [[a["id"], a["name"], a["url"], a.get("link_type", "other")] for a in assets]
                            if any(type(entry[0]) is not int or entry[0] <= 0 for entry in snapshot):
                                raise ValueError("invalid source link identity")
                            snapshot.sort(key=lambda entry: entry[0])
                        except (KeyError, TypeError, AttributeError, ValueError):
                            raise ReleaseRecoveryError("changed") from None
                        if snapshot != link_snapshot:
                            raise ReleaseRecoveryError("changed")
                        # Metadata identity is rechecked before each mutation; byte
                        # streams are rechecked once at the final checkpoint. Each
                        # transferred link is separately checked against its hash.
                        for link, size, expected_hash in (link_assets if check_bytes else []):
                            with gitlab_assets.download_asset(link["url"], cap) as downloaded:
                                if size is None:
                                    if not downloaded.oversize:
                                        raise ReleaseRecoveryError("changed")
                                else:
                                    if downloaded.oversize or downloaded.size != size:
                                        raise ReleaseRecoveryError("changed")
                                    h = hashlib.sha256()
                                    for chunk in iter(lambda: downloaded.file.read(CHUNK_SIZE), b""):
                                        h.update(chunk)
                                    if h.hexdigest() != expected_hash:
                                        raise ReleaseRecoveryError("changed")
                        assets = [dict(id=link["id"], name=link["name"], size=size,
                                       state="uploaded", browser_download_url=link["url"], _link_hash=hash_value)
                                  for link, size, hash_value in link_assets if size is not None]
                    if not isinstance(assets, list):
                        raise ValueError("release assets response is not an array")
                    all_source_names = [a.get("name") for a in assets]
                    if len(set(all_source_names)) != len(all_source_names):
                        raise ReleaseRecoveryError("mismatch")

                    def stable_entry(a):
                        # Whitelist of stable, meaningful fields only. Volatile
                        # provider metadata (download_count, timestamps, URLs,
                        # uploader, labels, content type, the full asset object)
                        # is deliberately excluded so routine provider-side
                        # updates do not look like source changes.
                        published = a.get("digest")
                        if isinstance(published, str) and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", published):
                            published = published.lower()
                        return [a.get("name"), a.get("size"), a.get("id"), a.get("state"), published]

                    def legacy_entry(a, include_created_at=True):
                        # Pre-fix manifest shape, retained only to recognise
                        # digests stored by old pending markers. The historical
                        # main manifest appended created_at (after the
                        # published digest) whenever present — real GitHub
                        # assets always carry it — so it is restored here.
                        # The even older previous-manifest form omitted it;
                        # that variant is recognised as well below. Neither
                        # form is used for new manifests: the stable manifest
                        # governs BOUND releases and the pending snapshot.
                        entry = [
                            a.get(k) for k in ("name", "size", "id", "updated_at", "browser_download_url")
                        ]
                        published = a.get("digest")
                        if published is not None:
                            if isinstance(published, str) and re.fullmatch(
                                r"sha256:[0-9a-fA-F]{64}", published
                            ):
                                published = published.lower()
                            entry.append(published)
                        if include_created_at and a.get("created_at") is not None:
                            entry.append(a["created_at"])
                        return entry

                    if pending:
                        # Only stable fields are compared between fetches. The
                        # full provider asset object is never hashed: it carries
                        # volatile values such as download_count (which our own
                        # downloads increment), timestamps, URLs and uploader
                        # data, so hashing it makes every pending sync fail.
                        snapshot = sorted((stable_entry(a) for a in assets), key=lambda entry: str(entry[0]))
                        if observed_stable_snapshot is not None and snapshot != observed_stable_snapshot:
                            raise ReleaseRecoveryError("changed")
                        observed_stable_snapshot = snapshot
                    incomplete = [a for a in assets if a.get("state") == "starter"]
                    observed_starter_names.update(
                        a["name"] for a in incomplete if isinstance(a.get("name"), str)
                    )
                    if incomplete:
                        print(
                            f"source upstream incomplete assets skipped={len(incomplete)}",
                            file=sys.stderr,
                            flush=True,
                        )
                    snapshot_mode = not pending and ("skipped" in state or state["phase"] == "WAITING_SOURCE")
                    candidate_skipped = None
                    if snapshot_mode:
                        saved = {x["name"]: x["id"] for x in state.get("skipped", [])}
                        candidate_skipped = dict(saved)
                        ids = [a["id"] for a in assets if type(a.get("id")) is int and a["id"] > 0]
                        if len(set(ids)) != len(ids):
                            raise ReleaseRecoveryError("mismatch")
                        saved_ids = {i for i in saved.values() if i is not None}
                        for a in incomplete:
                            name, aid = a.get("name"), a.get("id")
                            if (
                                not isinstance(name, str)
                                or not name
                                or (aid is not None and (type(aid) is not int or aid <= 0))
                            ):
                                raise ReleaseRecoveryError("mismatch")
                            candidate_skipped.setdefault(name, aid)
                        for a in assets:
                            if a.get("name") in saved and a.get("id") != saved[a["name"]]:
                                raise ReleaseRecoveryError("changed")
                            if type(a.get("id")) is int and a["id"] in saved_ids:
                                name = a.get("name")
                                if (
                                    not isinstance(name, str)
                                    or not name
                                    or (name in candidate_skipped and candidate_skipped[name] != a["id"])
                                ):
                                    raise ReleaseRecoveryError("mismatch")
                                candidate_skipped[name] = a["id"]
                        observed_starter_names.update(candidate_skipped)
                        all_source_names = list(set(all_source_names) | set(candidate_skipped))
                        assets = [a for a in assets if a.get("name") not in candidate_skipped]
                    manifest = [stable_entry(a) for a in assets]
                    manifest.sort(key=lambda a: str(a[0]))
                    if tag_identity:
                        # Recovery must bind the release metadata and tag object,
                        # not merely the (possibly empty) asset set.
                        manifest.append(["repo-mirror-tag-release-v1", source_metadata, original_tag_object,
                                         link_snapshot, [[a[0]["id"], a[1], a[2]] for a in link_assets]])
                    previous_manifest = [legacy_entry(a) for a in assets]
                    previous_manifest.sort(key=lambda a: str(a[0]))
                    # Same form without created_at, for markers stored under
                    # the still older previous-manifest scheme.
                    alternate_manifest = [legacy_entry(a, include_created_at=False) for a in assets]
                    alternate_manifest.sort(key=lambda a: str(a[0]))
                    pre_checksum_digest = hashlib.sha256(
                        json.dumps(previous_manifest, ensure_ascii=True, separators=(",", ":")).encode()
                    ).hexdigest()
                    alternate_pre_checksum_digest = hashlib.sha256(
                        json.dumps(alternate_manifest, ensure_ascii=True, separators=(",", ":")).encode()
                    ).hexdigest()
                    checksums = {}
                    checksum_files = [
                        a
                        for a in assets
                        if a.get("state") != "starter"
                        and not tag_identity and isinstance(a.get("name"), str)
                        and CHECKSUM_FILE_NAME.fullmatch(a["name"])
                    ]
                    if len(checksum_files) == 1:
                        checksum_file = checksum_files[0]
                        if (
                            type(checksum_file.get("size")) is int
                            and 0 <= checksum_file["size"] <= 1024 * 1024
                        ):
                            with tempfile.TemporaryFile() as data:
                                src.download(checksum_file["browser_download_url"], data)
                                if data.tell() != checksum_file["size"]:
                                    raise RuntimeError("source asset size mismatch")
                                data.seek(0)
                                content = data.read()
                            published = checksum_file.get("digest")
                            if isinstance(published, str) and re.fullmatch(
                                r"sha256:[0-9a-fA-F]{64}", published
                            ):
                                if hashlib.sha256(content).hexdigest() != published[7:].lower():
                                    raise RuntimeError("source asset digest mismatch")
                            manifest.append(
                                ["repo-mirror-checksum-content", hashlib.sha256(content).hexdigest()]
                            )
                            previous_manifest.append(
                                ["repo-mirror-checksum-content", hashlib.sha256(content).hexdigest()]
                            )
                            alternate_manifest.append(
                                ["repo-mirror-checksum-content", hashlib.sha256(content).hexdigest()]
                            )
                            try:
                                lines = content.decode("utf-8").splitlines()
                                for line in lines:
                                    match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *]([A-Za-z0-9_.-]+)", line)
                                    if not match or match[2] in (".", "..") or match[2] in checksums:
                                        checksums = {}
                                        break
                                    checksums[match[2]] = "sha256:" + match[1].lower()
                            except UnicodeError:
                                checksums = {}
                    digest = hashlib.sha256(
                        json.dumps(manifest, ensure_ascii=True, separators=(",", ":")).encode()
                    ).hexdigest()
                    previous_digest = hashlib.sha256(
                        json.dumps(previous_manifest, ensure_ascii=True, separators=(",", ":")).encode()
                    ).hexdigest()
                    alternate_digest = hashlib.sha256(
                        json.dumps(alternate_manifest, ensure_ascii=True, separators=(",", ":")).encode()
                    ).hexdigest()
                    if candidate_skipped is not None:
                        new_snapshot = [
                            {"name": n, "id": candidate_skipped[n]} for n in sorted(candidate_skipped)
                        ]
                        # A BOUND digest mismatch is not raised here: the caller
                        # may migrate a polluted pre-fix digest after verifying
                        # that every destination asset still matches the source.
                        if state.get("skipped") != new_snapshot:
                            state = dict(state, skipped=new_snapshot)
                            if state["phase"] == "BOUND":
                                dst.request(
                                    "PATCH",
                                    target + "/releases/" + str(existing["id"]),
                                    {"body": marked_body(body, state)},
                                )
                    return (
                        [a for a in assets if a.get("state") != "starter"],
                        digest,
                        checksums,
                        (
                            previous_digest,
                            pre_checksum_digest,
                            alternate_digest,
                            alternate_pre_checksum_digest,
                        ),
                    )

                def _verify_fail(reason):
                    print(f"verify-destination failed reason={reason}", file=sys.stderr, flush=True)
                    return ReleaseRecoveryError("changed")

                def verify_destination_matches_source(assets, strict_names, allow_missing=False):
                    """Byte-verify every source asset against the destination.

                    Raises ReleaseRecoveryError unless every source asset is
                    present at the destination as an uploaded asset of the same
                    size and identical bytes (and any published source digest
                    matches those bytes). Used before migrating a marker whose
                    stored digest predates the stable manifest scheme; nothing
                    is deleted or uploaded here.
                    """
                    old_assets = dst.pages(target + "/releases/" + str(existing["id"]) + "/assets")
                    old_by_name = {a["name"]: a for a in old_assets}
                    if len(old_by_name) != len(old_assets) or len({a["name"] for a in assets}) != len(assets):
                        raise ReleaseRecoveryError("mismatch")
                    if strict_names:
                        # Destination assets absent from the source are only
                        # tolerated when they are this mirror's own auxiliary
                        # cache assets; any other extra asset means the source
                        # asset set changed.
                        extras = set(old_by_name) - {a["name"] for a in assets}
                        cache_names = {
                            "REPO-MIRROR-MANIFEST.json", "REPO-MIRROR-MANIFEST.json.repo-mirror-staging",
                        }
                        secret = os.environ.get("MIRROR_TIMESTAMP_SECRET", "")
                        for extra in extras:
                            item = old_by_name[extra]
                            if (extra not in cache_names or not secret or item.get("state") != "uploaded"
                                    or type(item.get("size")) is not int or not 0 <= item["size"] <= 1024 * 1024):
                                raise _verify_fail("extra-destination-asset")
                            with tempfile.TemporaryFile() as f:
                                dst.download(item["url"], f)
                                if f.tell() != item["size"]:
                                    raise _verify_fail("extra-destination-asset")
                                f.seek(0)
                                try:
                                    document = json.load(f)
                                    if not isinstance(document, dict):
                                        raise ValueError()
                                    signature = document.pop("signature", None)
                                    if (document.get("owner") != "repo-mirror:timestamps:v1"
                                            or document.get("source") != marker or document.get("tag") != tag
                                            or not isinstance(document.get("assets"), dict)):
                                        raise ValueError()
                                    payload = json.dumps(document, sort_keys=True, ensure_ascii=True,
                                                         separators=(",", ":")).encode()
                                    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
                                    if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
                                        raise ValueError()
                                except (ValueError, TypeError, UnicodeError):
                                    raise _verify_fail("extra-destination-asset") from None
                    for asset in assets:
                        previous = old_by_name.get(asset["name"])
                        if previous is None and allow_missing:
                            # Interrupted upload: check only the source copy (size and
                            # published digest); the normal asset loop uploads it afterwards.
                            with tempfile.TemporaryFile() as source_data:
                                src.download(asset["browser_download_url"], source_data)
                                if source_data.tell() != asset["size"]:
                                    raise _verify_fail("missing-source-size-mismatch")
                                published = asset.get("digest")
                                if isinstance(published, str) and re.fullmatch(
                                    r"sha256:[0-9a-fA-F]{64}", published
                                ):
                                    source_data.seek(0)
                                    content_hash = hashlib.sha256()
                                    for chunk in iter(lambda: source_data.read(CHUNK_SIZE), b""):
                                        content_hash.update(chunk)
                                    if content_hash.hexdigest() != published[7:].lower():
                                        raise _verify_fail("missing-source-digest-mismatch")
                            continue
                        if (
                            previous is None
                            or previous.get("state") != "uploaded"
                            or isinstance(asset.get("size"), bool)
                            or type(asset.get("size")) is not int
                            or asset["size"] < 0
                            or type(previous.get("size")) is not int
                            or previous["size"] != asset["size"]
                        ):
                            raise _verify_fail("not-uploaded-or-size-mismatch")
                        with tempfile.TemporaryFile() as source_data, tempfile.TemporaryFile() as dest_data:
                            src.download(asset["browser_download_url"], source_data)
                            dst.download(previous["url"], dest_data)
                            if source_data.tell() != asset["size"] or not same_file(source_data, dest_data):
                                raise _verify_fail("byte-mismatch")
                            published = asset.get("digest")
                            if isinstance(published, str) and re.fullmatch(
                                r"sha256:[0-9a-fA-F]{64}", published
                            ):
                                source_data.seek(0)
                                content_hash = hashlib.sha256()
                                for chunk in iter(lambda: source_data.read(CHUNK_SIZE), b""):
                                    content_hash.update(chunk)
                                if content_hash.hexdigest() != published[7:].lower():
                                    raise _verify_fail("published-digest-mismatch")

                source_assets, digest, checksums, previous_digest = fetch_source_assets()
                print(
                    f"release {release_index}/{len(selected)} assets={len(source_assets)}",
                    file=sys.stderr,
                    flush=True,
                )
                if existing and pending:
                    print("release resuming pending update", file=sys.stderr, flush=True)
                    pending_name = existing["name"]
                    try:
                        original_name, expected_digest = json.loads(
                            base64.urlsafe_b64decode(pending_name[len(prefix) :].encode()).decode()
                        )
                        if (
                            original_name is not None and not isinstance(original_name, str)
                        ) or not isinstance(expected_digest, str):
                            raise ValueError()
                    except ValueError, UnicodeError, TypeError:
                        raise ReleaseRecoveryError("marker") from None
                    if digest != expected_digest:
                        legacy_manifest = [
                            [a.get(k) for k in ("name", "size", "id", "updated_at", "browser_download_url")]
                            for a in source_assets
                        ]
                        legacy_manifest.sort(key=lambda a: str(a[0]))
                        legacy_digest = hashlib.sha256(
                            json.dumps(legacy_manifest, ensure_ascii=True, separators=(",", ":")).encode()
                        ).hexdigest()
                        if expected_digest not in (legacy_digest, *previous_digest):
                            raise ReleaseRecoveryError("changed")
                        # A marker whose digest is exactly the plain legacy
                        # manifest of the current assets (no published digest,
                        # no created_at, no checksum file) was an exact match
                        # under the old scheme; upgrade it directly, as before
                        # the stable scheme. Missing or differing destination
                        # assets are still handled (and replacements refused)
                        # by the asset loop below, since pending releases may
                        # not replace.
                        direct_upgrade = (
                            expected_digest == legacy_digest
                            and all(
                                a.get("digest") is None and a.get("created_at") is None for a in source_assets
                            )
                            and not any(
                                isinstance(a.get("name"), str) and CHECKSUM_FILE_NAME.fullmatch(a["name"])
                                for a in source_assets
                            )
                        )
                        if not direct_upgrade:
                            # Old markers cannot attest to the original content digest. Only
                            # migrate when every current asset already matches the destination;
                            # otherwise an interrupted replacement cannot be distinguished
                            # safely from an unobserved source-content change.
                            verify_destination_matches_source(source_assets, strict_names=False)
                        _, checked_digest, _, _ = fetch_source_assets()
                        if checked_digest != digest:
                            raise ReleaseRecoveryError("changed")
                        if not direct_upgrade:
                            encoded = base64.urlsafe_b64encode(
                                json.dumps([original_name, digest], ensure_ascii=True).encode()
                            ).decode()
                            pending_name = prefix + encoded
                            if len(pending_name) > 255:
                                raise ValueError("release name too long for pending marker")
                            dst.request(
                                "PATCH", target + "/releases/" + str(existing["id"]), {"name": pending_name}
                            )
                        # For a direct legacy upgrade the pending name is left
                        # untouched (as under the old scheme, where the digest
                        # already matched); the asset loop runs first, so a
                        # truncated or starter destination still stops the run
                        # without any release mutation.
                else:
                    if state["phase"] == "BOUND" and state["digest"] != digest:
                        if tag_identity:
                            # Never migrate an unauthenticated tag-source snapshot:
                            # byte equality cannot attest to historical metadata.
                            raise ReleaseRecoveryError("changed")
                        # Pre-fix BOUND markers hashed the full provider asset
                        # object (including download_count), so their digest can
                        # never match the stable digest again. Migrate only
                        # after byte-verifying that every destination asset
                        # still matches the current source; anything else stays
                        # a manual-review stop. No release or asset is deleted.
                        # An empty current source set never migrates: a BOUND
                        # release whose assets all vanished needs review.
                        if not source_assets:
                            raise ReleaseRecoveryError("changed")
                        verify_destination_matches_source(
                            source_assets, strict_names=True, allow_missing=True
                        )
                        _, checked_digest, _, _ = fetch_source_assets()
                        if checked_digest != digest:
                            raise ReleaseRecoveryError("changed")
                        state = dict(state, digest=digest)
                        dst.request(
                            "PATCH",
                            target + "/releases/" + str(existing["id"]),
                            {"body": marked_body(body, state)},
                        )
                    if state["phase"] == "WAITING_SOURCE":
                        state = dict(state, phase="BOUND", digest=digest)
                        dst.request(
                            "PATCH",
                            target + "/releases/" + str(existing["id"]),
                            {"body": marked_body(body, state)},
                        )
                assets = dst.pages(target + "/releases/" + str(existing["id"]) + "/assets")
                if any(asset.get("state") == "starter" for asset in assets):
                    raise StarterAssetError()
                by_name = {asset["name"]: asset for asset in assets}
                if tag_identity and any(link["name"] in by_name for link, size, _ in link_assets if size is None):
                    # A same-name file could be destination-owned; oversized links
                    # cannot establish byte identity or authorize its deletion.
                    raise ReleaseRecoveryError("mismatch")
                if len(by_name) != len(assets) or len({a["name"] for a in source_assets}) != len(
                    source_assets
                ):
                    raise ReleaseRecoveryError("mismatch")
                # Source-owned current cache names disable cache management.
                new_name = "REPO-MIRROR-MANIFEST.json"
                new_stage = new_name + ".repo-mirror-staging"
                cache_name, stage_name = new_name, new_stage
                secret = os.environ.get("MIRROR_TIMESTAMP_SECRET", "")
                cache_enabled = (
                    bool(secret) and not tag_identity
                    and not pending
                    and not ({new_name, new_stage, "MIRROR-REPO-MANIFEST.json"} & set(all_source_names))
                )
                cache_owner = {"owner": "repo-mirror:timestamps:v1", "source": marker, "tag": tag}
                cached = {}
                old_cache = by_name.get(cache_name) if cache_enabled else None
                old_content = None
                if old_cache:
                    with tempfile.TemporaryFile() as f:
                        dst.download(old_cache["url"], f)
                        if f.tell() <= 1024 * 1024:
                            f.seek(0)
                            old_content = f.read()
                    try:
                        parsed = json.loads(old_content) if old_content is not None else None
                        if (
                            not isinstance(parsed, dict)
                            or any(parsed.get(k) != v for k, v in cache_owner.items())
                            or not isinstance(parsed.get("assets"), dict)
                        ):
                            raise ValueError()
                        signature = parsed.pop("signature", None)
                        payload = json.dumps(
                            parsed, sort_keys=True, ensure_ascii=True, separators=(",", ":")
                        ).encode()
                        expected_signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
                        if not isinstance(signature, str) or not hmac.compare_digest(
                            signature, expected_signature
                        ):
                            raise ValueError()
                        cached = parsed["assets"]
                    except ValueError, TypeError, UnicodeError:
                        # Unowned or malformed assets are never overwritten.
                        cache_enabled = False
                records = {}
                fresh_hashes = {}
                shortcut_hashes = {}

                def identity(asset):
                    stamp = asset.get("updated_at") or asset.get("created_at")
                    aid, size = asset.get("id"), asset.get("size")
                    if (
                        type(aid) is not int
                        or aid <= 0
                        or not isinstance(stamp, str)
                        or not stamp
                        or type(size) is not int
                        or size < 0
                        or not isinstance(asset.get("name"), str)
                    ):
                        return None
                    return {"id": aid, "name": asset["name"], "timestamp": stamp, "size": size}

                upload_failures = 0
                first_upload_status = None
                uploaded = updated = skipped = retried = 0
                if journal:
                    keys = {asset["name"]: asset_key(tag, asset["name"]) for asset in source_assets}
                    failures = journal.failures
                    ordered_assets = sorted(source_assets, key=lambda asset: keys[asset["name"]] not in failures)
                else:
                    ordered_assets = source_assets
                for asset_index, asset in enumerate(ordered_assets, 1):
                    print(
                        f"asset {asset_index}/{len(ordered_assets)} started asset_name={safe_label(asset.get('name'))}",
                        file=sys.stderr,
                        flush=True,
                    )
                    with timed(f"asset asset_name={safe_label(asset.get('name'))}"):
                        key = keys[asset["name"]] if journal else None
                        was_journaled = journal is not None and key in journal.failures
                        previous = by_name.get(asset["name"])
                        source_hash = asset.get("digest")
                        if not (
                            isinstance(source_hash, str)
                            and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", source_hash)
                        ):
                            source_hash = checksums.get(asset["name"])
                        target_hash = previous.get("digest") if previous else None
                        ident = identity(asset) if cache_enabled else None
                        entry = cached.get(asset["name"]) if ident else None
                        # Only the concrete authenticated GitHub adapter may attest
                        # to the digest in the listing; a checksum-file fallback or
                        # an arbitrary adapter cannot opt into this shortcut.
                        trusted_listing = type(src) is ForgejoSourceAdapter
                        published = asset.get("digest")
                        if (
                            trusted_listing
                            and isinstance(published, str)
                            and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", published)
                            and type(asset.get("size")) is int and asset["size"] >= 0
                            and previous is not None and previous.get("state") == "uploaded"
                            and type(previous.get("id")) is int and previous["id"] > 0
                            and type(previous.get("size")) is int
                            and previous["size"] == asset["size"]
                        ):
                            with tempfile.TemporaryFile() as dest_data:
                                dst.download(previous["url"], dest_data)
                                if dest_data.tell() == asset["size"]:
                                    dest_data.seek(0)
                                    actual = hashlib.sha256()
                                    for chunk in iter(lambda: dest_data.read(CHUNK_SIZE), b""):
                                        actual.update(chunk)
                                    if actual.hexdigest() == published[7:].lower():
                                        shortcut_hashes[asset["name"]] = (previous["id"], actual.hexdigest())
                                        if ident:
                                            records[asset["name"]] = dict(ident, sha256=actual.hexdigest())
                                        print("asset unchanged reason=authenticated-source-digest", file=sys.stderr, flush=True)
                                        skipped += 1
                                        continue
                        if (
                            not trusted_listing
                            and type(asset.get("size")) is int
                            and asset["size"] >= 0
                            and previous is not None
                            and type(previous.get("size")) is int
                            and previous["size"] >= 0
                            and asset["size"] == previous["size"]
                            and isinstance(source_hash, str)
                            and isinstance(target_hash, str)
                            and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", source_hash)
                            and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", target_hash)
                            and source_hash.lower() == target_hash.lower()
                        ):
                            print(
                                f"asset {asset_index}/{len(ordered_assets)} unchanged (published SHA-256) reason=matching-published-digests",
                                file=sys.stderr,
                                flush=True,
                            )
                            if ident:
                                records[asset["name"]] = dict(ident, sha256=source_hash[7:].lower())
                            skipped += 1
                            continue
                        if (
                            not trusted_listing
                            and isinstance(entry, dict)
                            and all(entry.get(k) == v for k, v in ident.items())
                            and isinstance(entry.get("sha256"), str)
                            and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                            and previous
                            and previous.get("state") == "uploaded"
                            and type(previous.get("size")) is int
                            and previous["size"] == ident["size"]
                            and isinstance(target_hash, str)
                            and target_hash.lower() == "sha256:" + entry["sha256"]
                            and (not source_hash or source_hash.lower() == target_hash.lower())
                        ):
                            print(
                                "asset unchanged reason=verified-timestamp-manifest",
                                file=sys.stderr,
                                flush=True,
                            )
                            records[asset["name"]] = dict(ident, sha256=entry["sha256"])
                            skipped += 1
                            continue
                        print(
                            f"asset {asset_index}/{len(ordered_assets)} comparing downloaded bytes reason={compare_reason(previous, source_hash, target_hash, ident, entry)}",
                            file=sys.stderr,
                            flush=True,
                        )
                        with tempfile.TemporaryFile() as data:
                            if tag_identity:
                                with gitlab_assets.download_asset(asset["browser_download_url"], cap) as downloaded:
                                    if downloaded.oversize or downloaded.size != asset["size"]:
                                        raise ReleaseRecoveryError("changed")
                                    for chunk in iter(lambda: downloaded.file.read(CHUNK_SIZE), b""):
                                        data.write(chunk)
                            else:
                                src.download(asset["browser_download_url"], data)
                            if (
                                not isinstance(asset.get("size"), int)
                                or (trusted_listing and (type(asset.get("size")) is not int or asset["size"] < 0))
                                or data.tell() != asset["size"]
                            ):
                                raise RuntimeError("source asset size mismatch")
                            if isinstance(source_hash, str) and re.fullmatch(
                                r"sha256:[0-9a-fA-F]{64}", source_hash
                            ):
                                data.seek(0)
                                actual_hash = hashlib.sha256()
                                for chunk in iter(lambda: data.read(CHUNK_SIZE), b""):
                                    actual_hash.update(chunk)
                                if actual_hash.hexdigest() != source_hash[7:].lower():
                                    raise RuntimeError("source asset digest mismatch")
                            data.seek(0)
                            calculated = hashlib.sha256()
                            for chunk in iter(lambda: data.read(CHUNK_SIZE), b""):
                                calculated.update(chunk)
                            if tag_identity and calculated.hexdigest() != asset["_link_hash"]:
                                raise ReleaseRecoveryError("changed")
                            if ident:
                                records[asset["name"]] = dict(ident, sha256=calculated.hexdigest())
                            changed = False
                            if previous:
                                with tempfile.TemporaryFile() as existing_data:
                                    dst.download(previous["url"], existing_data)
                                    if isinstance(target_hash, str) and re.fullmatch(
                                        r"sha256:[0-9a-fA-F]{64}", target_hash
                                    ):
                                        existing_data.seek(0)
                                        actual_target_hash = hashlib.sha256()
                                        for chunk in iter(lambda: existing_data.read(CHUNK_SIZE), b""):
                                            actual_target_hash.update(chunk)
                                        if actual_target_hash.hexdigest() != target_hash[7:].lower():
                                            raise RuntimeError("destination asset digest mismatch")
                                    with timed("step asset-byte-compare"):
                                        changed = not same_file(data, existing_data)
                                    if changed and not may_replace:
                                        raise ReleaseRecoveryError("mismatch")
                            if changed:
                                _, current_digest, _, _ = fetch_source_assets()
                                if current_digest != digest:
                                    raise ReleaseRecoveryError("changed")
                                asset_id = previous.get("id")
                                if (
                                    not isinstance(asset_id, int)
                                    or isinstance(asset_id, bool)
                                    or asset_id <= 0
                                ):
                                    raise ReleaseRecoveryError("mismatch")
                                if journal:
                                    journal.set(key, True)
                                dst.request("DELETE", target + "/releases/assets/" + str(asset_id))
                            if not previous or changed:
                                _, current_digest, _, _ = fetch_source_assets()
                                if current_digest != digest:
                                    raise ReleaseRecoveryError("changed")
                                data.seek(0)
                                upload_url = (
                                    existing["upload_url"].split("{", 1)[0]
                                    + "?"
                                    + urlencode({"name": asset["name"]})
                                )
                                if journal:
                                    journal.set(key, True)
                                try:
                                    if was_journaled:
                                        retried += 1
                                    upload_with_reconciliation(
                                        dst, upload_url, data,
                                        asset.get("content_type") or "application/octet-stream",
                                        target + "/releases/" + str(existing["id"]) + "/assets", asset["name"],
                                    )
                                except Exception as exc:
                                    detail = error_detail(exc)
                                    if (
                                        isinstance(exc, HTTPError)
                                        and first_upload_status is None
                                        and type(exc.code) is int
                                        and 100 <= exc.code <= 599
                                    ):
                                        first_upload_status = exc.code
                                    if isinstance(exc, HTTPError):
                                        exc.close()
                                    print(
                                        f"asset upload failed for asset {asset_index}/{len(ordered_assets)}: {detail}",
                                        file=sys.stderr,
                                        flush=True,
                                    )
                                    if journal is None:
                                        raise
                                    upload_failures += 1
                                    continue
                                fresh_hashes[asset["name"]] = calculated.hexdigest()
                                if changed:
                                    updated += 1
                                else:
                                    uploaded += 1
                            else:
                                if tag_identity:
                                    shortcut_hashes[asset["name"]] = (previous["id"], calculated.hexdigest())
                                skipped += 1

                print(
                    f"assets uploaded={uploaded} updated={updated} skipped={skipped} retried={retried} upload_failed={upload_failures}",
                    file=sys.stderr,
                    flush=True,
                )
                if upload_failures:
                    exc = RuntimeError("release asset uploads failed")
                    exc.mirror_upload_status = first_upload_status
                    raise exc
                _, final_digest, _, _ = fetch_source_assets()
                if final_digest != digest:
                    raise ReleaseRecoveryError("changed")
                final_assets = dst.pages(target + "/releases/" + str(existing["id"]) + "/assets")
                if any(asset.get("state") == "starter" for asset in final_assets):
                    raise StarterAssetError()
                expected = {asset["name"]: asset["size"] for asset in source_assets}
                actual = {asset["name"]: asset for asset in final_assets}
                if tag_identity and any(link["name"] in actual for link, size, _ in link_assets if size is None):
                    raise ReleaseRecoveryError("mismatch")
                if (
                    len(expected) != len(source_assets)
                    or len(actual) != len(final_assets)
                    or not expected.keys() <= actual.keys()
                    or any(
                        actual[name].get("state") != "uploaded" or actual[name].get("size") != size
                        for name, size in expected.items()
                    )
                ):
                    raise ReleaseRecoveryError("mismatch")
                for asset_name, (pinned_id, expected_hash) in shortcut_hashes.items():
                    dest_asset = actual[asset_name]
                    if (
                        type(dest_asset.get("id")) is not int or dest_asset["id"] != pinned_id
                        or type(dest_asset.get("size")) is not int
                        or dest_asset["size"] != expected[asset_name]
                    ):
                        raise ReleaseRecoveryError("mismatch")
                    with tempfile.TemporaryFile() as f:
                        dst.download(dest_asset["url"], f)
                        if f.tell() != expected[asset_name]:
                            raise RuntimeError("destination asset digest mismatch")
                        f.seek(0)
                        h = hashlib.sha256()
                        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
                            h.update(chunk)
                        if h.hexdigest() != expected_hash:
                            raise RuntimeError("destination asset digest mismatch")
                if shortcut_hashes:
                    _, checked_digest, _, _ = fetch_source_assets()
                    if checked_digest != digest:
                        raise ReleaseRecoveryError("changed")
                for asset_name, expected_hash in fresh_hashes.items():
                    dest_asset = actual[asset_name]
                    dh = dest_asset.get("digest")
                    if tag_identity or not (isinstance(dh, str) and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", dh)):
                        with tempfile.TemporaryFile() as f:
                            dst.download(dest_asset["url"], f)
                            if f.tell() != expected[asset_name]:
                                raise RuntimeError("destination asset digest mismatch")
                            f.seek(0)
                            h = hashlib.sha256()
                            for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
                                h.update(chunk)
                            dh = "sha256:" + h.hexdigest()
                    if dh.lower() != "sha256:" + expected_hash:
                        raise RuntimeError("destination asset digest mismatch")
                if (
                    cache_enabled
                    and not ({cache_name, stage_name} & observed_starter_names)
                    and (records or old_cache)
                ):
                    # Verify every recorded destination byte string, including uploads.
                    for asset_name, record in records.items():
                        if asset_name in fresh_hashes or asset_name in shortcut_hashes:
                            continue
                        dest_asset = actual[asset_name]
                        dh = dest_asset.get("digest")
                        if not (isinstance(dh, str) and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", dh)):
                            with tempfile.TemporaryFile() as f:
                                dst.download(dest_asset["url"], f)
                                if f.tell() != record["size"]:
                                    raise RuntimeError("destination asset digest mismatch")
                                f.seek(0)
                                h = hashlib.sha256()
                                for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
                                    h.update(chunk)
                                dh = "sha256:" + h.hexdigest()
                        if dh.lower() != "sha256:" + record["sha256"]:
                            raise RuntimeError("destination asset digest mismatch")
                    document = dict(cache_owner, assets=records)
                    payload = json.dumps(
                        document, sort_keys=True, ensure_ascii=True, separators=(",", ":")
                    ).encode()
                    document["signature"] = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
                    content = (
                        json.dumps(document, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n"
                    ).encode()
                    if content != old_content:
                        stage = actual.get(stage_name)
                        if stage:
                            # Never remove or reuse a foreign or stale staging asset.
                            with tempfile.TemporaryFile() as f:
                                dst.download(stage["url"], f)
                                f.seek(0)
                                if f.read() != content or stage.get("state") != "uploaded":
                                    raise ReleaseRecoveryError("mismatch")
                        elif old_cache:
                            with tempfile.TemporaryFile() as f:
                                f.write(content)
                                f.seek(0)
                                u = (
                                    existing["upload_url"].split("{", 1)[0]
                                    + "?"
                                    + urlencode({"name": stage_name})
                                )
                                upload_with_reconciliation(dst, u, f, "application/json", target + "/releases/" + str(existing["id"]) + "/assets", stage_name)
                            staged_assets = dst.pages(target + "/releases/" + str(existing["id"]) + "/assets")
                            stages = [a for a in staged_assets if a.get("name") == stage_name]
                            if len(stages) != 1 or stages[0].get("state") != "uploaded":
                                raise ReleaseRecoveryError("mismatch")
                            stage = stages[0]
                            with tempfile.TemporaryFile() as f:
                                dst.download(stage["url"], f)
                                f.seek(0)
                                if f.read() != content:
                                    raise ReleaseRecoveryError("mismatch")
                        if stage:
                            stage_id = stage.get("id")
                            if type(stage_id) is not int or stage_id <= 0:
                                raise ReleaseRecoveryError("mismatch")
                        if old_cache:
                            asset_id = old_cache.get("id")
                            if type(asset_id) is not int or asset_id <= 0:
                                raise ReleaseRecoveryError("mismatch")
                            dst.request("DELETE", target + "/releases/assets/" + str(asset_id))
                        if stage:
                            dst.request(
                                "PATCH", target + "/releases/assets/" + str(stage_id), {"name": cache_name}
                            )
                        else:
                            with tempfile.TemporaryFile() as f:
                                f.write(content)
                                f.seek(0)
                                u = (
                                    existing["upload_url"].split("{", 1)[0]
                                    + "?"
                                    + urlencode({"name": cache_name})
                                )
                                upload_with_reconciliation(dst, u, f, "application/json", target + "/releases/" + str(existing["id"]) + "/assets", cache_name)
                        verified = [
                            a
                            for a in dst.pages(target + "/releases/" + str(existing["id"]) + "/assets")
                            if a.get("name") == cache_name
                        ]
                        if len(verified) != 1 or verified[0].get("state") != "uploaded":
                            raise ReleaseRecoveryError("mismatch")
                        with tempfile.TemporaryFile() as f:
                            dst.download(verified[0]["url"], f)
                            f.seek(0)
                            if f.read() != content:
                                raise ReleaseRecoveryError("mismatch")
                fresh_release = src.get_release(tag) if tag_identity else src.request("GET", upstream + "/releases/" + str(release["id"]))
                if not isinstance(fresh_release, dict) or any(
                    (key in fresh_release, fresh_release.get(key)) != source_metadata[key]
                    or type(fresh_release.get(key)) is not type(source_metadata[key][1])
                    for key in metadata_keys
                ):
                    raise ValueError("source release changed during sync")
                if empty_forgejo_target:
                    if verify_forgejo_empty_target_tag(src, dst, upstream, target, tag) != original_tag_object:
                        raise ValueError("tag verification failed")
                if tag_identity and src.verify_release_tag(tag, dst, target) != original_tag_object:
                    raise ValueError("tag verification failed")
                if tag_identity:
                    _, checked_digest, _, _ = fetch_source_assets(check_bytes=True)
                    if checked_digest != digest:
                        raise ReleaseRecoveryError("changed")
                # GitHub ignores a null body in PATCH; explicitly clear the
                # recovery marker. Forgejo's adapter maps empty to one space.
                final_body = release.get("body")
                if final_body is None:
                    final_body = ""
                if tag_identity and preserved_links:
                    final_body += "\n\nOriginal oversized release asset links:\n" + "\n".join(preserved_links)

                if release.get("draft") is True:
                    final_body = logical_release_body(final_body) + "\n" + IDENTITY_SENTINEL + draft_identity_signature(
                        secret, marker, tag, recovery_target, existing["id"]
                    )
                    if len(final_body) > 125000:
                        raise ValueError("release body too long for draft identity")
                final_fields = {
                        "name": release.get("name"),
                        "body": final_body,
                        "make_latest": "false",
                        **({"tag_name": tag} if existing.get("draft") is True and release.get("draft") is False else {}),
                        **{k: release[k] for k in ("draft", "prerelease") if k in release},
                    }
                if getattr(dst, "supports_target_commitish", False) and release.get("target_commitish") is not None:
                    final_fields["target_commitish"] = release["target_commitish"]
                old_tag = existing.get("tag_name")
                if (authenticated_draft and isinstance(old_tag, str) and old_tag.startswith("untagged-")
                        and existing.get("draft") is True and release.get("draft") is False
                        and callable(getattr(dst, "publish_authenticated_draft", None))):
                    dst.publish_authenticated_draft(existing["id"], old_tag, final_fields)
                else:
                    dst.request("PATCH", target + "/releases/" + str(existing["id"]), final_fields)
                # GitHub may accept a clearing PATCH without persisting it. Verify
                # the exact published release before reporting success or clearing
                # the failure journal; drafts retain a signed identity marker.
                if tag_identity or (not getattr(dst, "supports_target_commitish", False)
                        and source_body is None and release.get("draft") is not True):
                    verified_release = dst.request("GET", target + "/releases/" + str(existing["id"]))
                    if (not isinstance(verified_release, dict)
                            or type(verified_release.get("id")) is not int
                            or verified_release["id"] != existing["id"]
                            or verified_release.get("tag_name") != tag
                            or verified_release.get("draft") is not (release.get("draft") if tag_identity else False)
                            or (tag_identity and (verified_release.get("name") != final_fields["name"]
                                or type(verified_release.get("prerelease")) is not bool
                                or verified_release["prerelease"] is not release["prerelease"]))
                            or verified_release.get("body") != (
                                (" " if isinstance(dst, ForgejoDestinationAdapter)
                                 and dst.supports_target_commitish and final_body == "" else final_body)
                                if tag_identity else "")):
                        raise ReleaseRecoveryError("mismatch")
                if latest and tag == latest["tag_name"]:
                    latest_id = existing["id"]
                    latest_read_only = False
                if journal:
                    for asset_name in {asset["name"] for asset in source_assets} | observed_starter_names:
                        key = asset_key(tag, asset_name)
                        if key in journal.failures:
                            journal.set(key, False)
                print(f"release {release_index}/{len(selected)} completed", file=sys.stderr, flush=True)
        except Exception as exc:
            release_errors.append(exc)
            print(f"release failed tag={safe_label(tag)}: {error_detail(exc)}", file=sys.stderr, flush=True)
    if release_errors:
        if len(release_errors) == 1:
            raise release_errors[0]
        raise ReleaseAggregateError(release_errors) from release_errors[0]
    if latest_id is not None:
        if latest_read_only:
            try:
                current = dst.request("GET", target + "/releases/latest")
                matches = isinstance(current, dict) and type(current.get("id")) is int and current["id"] == latest_id
            except Exception:
                matches = False
            if not matches:
                raise RuntimeError("destination latest release cannot be selected")
        else:
            dst.request("PATCH", target + "/releases/" + str(latest_id), {"make_latest": "true"})
