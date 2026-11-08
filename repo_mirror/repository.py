"""repository: see README for an overview."""

import base64
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse

from repo_mirror.constants import CHUNK_SIZE, GITHUB, NAME
from repo_mirror.git_sync import mirror_git
from repo_mirror.github_api import not_found, same_file
from repo_mirror.journal import ReleaseRecoveryError, StarterAssetError, compare_reason
from repo_mirror.logging_utils import error_detail, safe_label, timed, timed_call, timed_repository
from repo_mirror.sources import check_existing, destination_name, get_existing, source_api_base, source_marker

# Checksum file names accepted as a fallback source, any letter case:
# SHA256SUM, SHA256SUMS, SHA256SUM.txt, SHA256SUMS.txt.
CHECKSUM_FILE_NAME = re.compile(r"SHA256SUMS?(?:\.txt)?", re.I)


def release_marker_is_pending(body):
    """Return True only for v3 markers still waiting for the source (phase WAITING_SOURCE).

    A BOUND marker means the release finished and is recorded; it must not make
    later runs recheck that release.
    """
    sentinel = "[repo-mirror:pending:v3]"
    if not isinstance(body, str) or sentinel not in body:
        return False
    try:
        state = json.loads(body.split("\n" + sentinel, 1)[1])
    except IndexError, ValueError:
        return True  # unreadable marker: keep it selected so the run can report it
    return isinstance(state, dict) and state.get("phase") != "BOUND"


def find_tag_releases(dst, target, tag, source_name):
    """Return destination releases for a tag, including unpublished drafts.

    GitHub renames a draft that has no published tag to ``untagged-<hash>``, so an
    exact ``tag_name`` match can miss it and the mirror creates a duplicate draft.
    Fallback: drafts with that exact name and an ``untagged-`` tag name.
    """
    releases = list(dst.pages(target + "/releases"))
    exact = [r for r in releases if r.get("tag_name") == tag]
    if exact:
        return exact
    return [
        r
        for r in releases
        if r.get("draft") is True
        and isinstance(r.get("tag_name"), str)
        and r["tag_name"].startswith("untagged-")
        and r.get("name") == source_name
    ]


@timed_repository
def mirror_repository(source, name, owner, src, dst, token, account_type="organization", journal=None):
    """Mirror one source repository to its destination: branches, tags, releases and assets."""
    print(
        f"repository started source_repo={safe_label(source.get('owner'))}/{safe_label(name)} target=destination",
        file=sys.stderr,
        flush=True,
    )
    dest_name = destination_name(source, name)
    marker = source_marker(source, name)
    qowner, qname = quote(owner, safe=""), quote(dest_name, safe="")
    target = f"{GITHUB}/repos/{qowner}/{qname}"
    repo = get_existing(dst, target)
    check_existing(repo, marker)
    initializing = repo is None or repo.get("description") == marker + " [repo-mirror:initializing:v1]"
    srcbase = source_api_base(source)
    upstream = f"{srcbase}/repos/{quote(source['owner'], safe='')}/{quote(name, safe='')}"
    metadata = src.request("GET", upstream)
    clone_url = metadata["clone_url"]
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
        or not NAME.fullmatch(source["owner"])
        or source["owner"] in (".", "..")
        or not NAME.fullmatch(name)
        or name in (".", "..")
        or not clone_path_matches
    ):
        raise ValueError("source clone URL does not match source identity")
    if repo is None:
        endpoint = "/user/repos" if account_type == "personal" else f"/orgs/{qowner}/repos"
        repo = dst.request(
            "POST",
            GITHUB + endpoint,
            {
                "name": dest_name,
                "private": True,
                "auto_init": False,
                "description": marker + " [repo-mirror:initializing:v1]",
            },
        )
        if repo.get("full_name", "").lower() != (owner + "/" + dest_name).lower():
            raise RuntimeError("created repository owner does not match configured destination")
    dst.request("PUT", target + "/actions/permissions", {"enabled": False})
    default_branch = metadata.get("default_branch")

    def update_default():
        if not default_branch:
            raise RuntimeError("source default branch missing; refusing to prune")
        if repo.get("default_branch") != default_branch:
            dst.request("PATCH", target, {"default_branch": default_branch})

    with timed("step git mirror"):
        mirror_git(clone_url, f"https://github.com/{qowner}/{qname}.git", token, update_default)
    with timed("step release-discovery target=source"):
        releases = src.pages(upstream + "/releases")
    invalid_releases = [
        r
        for r in releases
        if not isinstance(r, dict) or not isinstance(r.get("tag_name"), str) or not r.get("tag_name")
    ]
    if invalid_releases:
        print("release failed: invalid source release tag", file=sys.stderr, flush=True)
        releases = [
            r
            for r in releases
            if isinstance(r, dict) and isinstance(r.get("tag_name"), str) and r.get("tag_name")
        ]
    try:
        latest = src.request("GET", upstream + "/releases/latest")
        if (
            not isinstance(latest, dict)
            or not latest.get("tag_name")
            or latest.get("draft")
            or latest.get("prerelease")
        ):
            raise ValueError("invalid source latest release")
    except HTTPError as exc:
        if not not_found(exc):
            raise
        exc.close()
        latest = None

    if initializing or len(releases) <= 1:
        selected = list(releases)
    else:
        pending_tags = {
            r.get("tag_name")
            for r in dst.pages(target + "/releases")
            if (
                isinstance(r.get("name"), str)
                and r["name"].startswith(("[repo-mirror:pending:v1]", "[repo-mirror:pending:v2]"))
            )
            or release_marker_is_pending(r.get("body"))
        }
        failed_releases = {key.split(":", 1)[0] for key in journal.failures} if journal else set()
        selected = releases[:1] + [
            r
            for r in releases[1:]
            if r["tag_name"] in pending_tags
            or (journal and journal.key(source, name, owner, r["tag_name"], "")[:64] in failed_releases)
        ]
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
            f"\n===== Release {release_index}/{len(selected)}: {safe_label(tag)} =====",
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
                try:
                    existing = dst.request("GET", target + "/releases/tags/" + quote(tag, safe=""))
                except HTTPError as e:
                    if not not_found(e):
                        raise
                    e.close()
                    # The tag endpoint can omit drafts, which still need to be found.
                    matches = find_tag_releases(dst, target, tag, release.get("name"))
                    if len(matches) > 1:
                        raise RuntimeError("multiple destination releases for tag")
                    existing = matches[0] if matches else None
                prefix_v1 = "[repo-mirror:pending:v1]"
                prefix_v2 = "[repo-mirror:pending:v2]"
                current_name = existing.get("name") if existing else None
                pending = isinstance(current_name, str) and current_name.startswith((prefix_v1, prefix_v2))
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
                        "target": target,
                        "release_id": release_id,
                        "state": marker_state,
                    }
                    encoded = json.dumps(
                        payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")
                    ).encode()
                    return hmac.new(
                        secret.encode(), b"repo-mirror:release-recovery:v3\0" + encoded, hashlib.sha256
                    ).hexdigest()

                source_body = release.get("body")
                if (source_body is not None and not isinstance(source_body, str)) or (
                    isinstance(source_body, str) and sentinel in source_body
                ):
                    raise ReleaseRecoveryError("marker")
                source_name = release.get("name")
                if isinstance(source_name, str) and source_name.startswith((prefix_v1, prefix_v2)):
                    raise ValueError("source release name uses reserved marker")
                body = existing.get("body") if existing else None
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
                        fields = {k: release.get(k) for k in ("draft", "prerelease")}
                        fields.update(tag_name=tag, name=source_name, body=marked, make_latest="false")
                        try:
                            existing = dst.request("POST", target + "/releases", fields)
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
                    matches = find_tag_releases(dst, target, tag, release.get("name"))
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
                asset_url = upstream + "/releases/" + str(release["id"]) + "/assets"
                observed_stable_snapshot = None
                all_source_names = set()
                observed_starter_names = set()

                @timed_call(lambda: "step source-manifest target=source")
                def fetch_source_assets():
                    nonlocal observed_stable_snapshot, all_source_names, state
                    assets = (
                        src.request("GET", asset_url) if source["type"] == "forgejo" else src.pages(asset_url)
                    )
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
                        and isinstance(a.get("name"), str)
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
                    print(f"verify-destination failed reason={reason} tag={tag}", file=sys.stderr, flush=True)
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
                        extras = (
                            set(old_by_name)
                            - {a["name"] for a in assets}
                            - {"TIMESTAMPS.txt", "TIMESTAMPS.txt.repo-mirror-staging"}
                        )
                        if extras:
                            raise _verify_fail("extra-destination-asset")
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
                if len(by_name) != len(assets) or len({a["name"] for a in source_assets}) != len(
                    source_assets
                ):
                    raise ReleaseRecoveryError("mismatch")
                # A source asset with either reserved name always wins; never treat it as
                # our metadata or attempt to manage the auxiliary names in that release.
                cache_name = "TIMESTAMPS.txt"
                stage_name = "TIMESTAMPS.txt.repo-mirror-staging"
                secret = os.environ.get("MIRROR_TIMESTAMP_SECRET", "")
                cache_enabled = (
                    bool(secret)
                    and not pending
                    and not any(name in (cache_name, stage_name) for name in all_source_names)
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
                ordered_assets = (
                    journal.retry_order(source, name, owner, tag, source_assets) if journal else source_assets
                )
                for asset_index, asset in enumerate(ordered_assets, 1):
                    print(
                        f"asset {asset_index}/{len(ordered_assets)} started asset_name={safe_label(asset.get('name'))}",
                        file=sys.stderr,
                        flush=True,
                    )
                    with timed(f"asset asset_name={safe_label(asset.get('name'))}"):
                        key = journal.key(source, name, owner, tag, asset["name"]) if journal else None
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
                        if (
                            type(asset.get("size")) is int
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
                            isinstance(entry, dict)
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
                            src.download(asset["browser_download_url"], data)
                            if not isinstance(asset.get("size"), int) or data.tell() != asset["size"]:
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
                                    dst.upload(
                                        upload_url,
                                        data,
                                        asset.get("content_type") or "application/octet-stream",
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
                for asset_name, expected_hash in fresh_hashes.items():
                    dest_asset = actual[asset_name]
                    dh = dest_asset.get("digest")
                    if not (isinstance(dh, str) and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", dh)):
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
                        if asset_name in fresh_hashes:
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
                                dst.upload(u, f, "application/json")
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
                                dst.upload(u, f, "application/json")
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
                dst.request(
                    "PATCH",
                    target + "/releases/" + str(existing["id"]),
                    {
                        "name": release.get("name"),
                        "body": release.get("body"),
                        "make_latest": "false",
                        **{k: release[k] for k in ("draft", "prerelease") if k in release},
                    },
                )
                if latest and tag == latest["tag_name"]:
                    latest_id = existing["id"]
                if journal:
                    for asset_name in {asset["name"] for asset in source_assets} | observed_starter_names:
                        key = journal.key(source, name, owner, tag, asset_name)
                        if key in journal.failures:
                            journal.set(key, False)
                print(f"release {release_index}/{len(selected)} completed", file=sys.stderr, flush=True)
        except Exception as exc:
            release_errors.append(exc)
            print(f"release failed tag={safe_label(tag)}: {error_detail(exc)}", file=sys.stderr, flush=True)
    if release_errors:
        if len(release_errors) == 1:
            raise release_errors[0]
        summary = RuntimeError(f"{len(release_errors)} releases failed")
        summary.release_errors = list(release_errors)  # read by error_detail for the log line
        raise summary from release_errors[0]
    if latest_id is not None:
        dst.request("PATCH", target + "/releases/" + str(latest_id), {"make_latest": "true"})
    if initializing:
        dst.request("PATCH", target, {"description": marker})
