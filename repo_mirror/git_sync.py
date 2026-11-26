"""git_sync: see README for an overview."""

import base64
import hashlib
import os
import re
import subprocess
import tempfile
from urllib.parse import quote

from repo_mirror.constants import GITHUB, NAME
from repo_mirror.logging_utils import timed
from repo_mirror.snapshot_push import (
    _default_branch,
    archive_snapshot,
    capture_snapshot,
    git_phase,
    is_seed_preimage,
    push_snapshot,
    seed_in_progress,
)
from repo_mirror.staleness import observe
from repo_mirror.wipe_detection import detect_wipe


def synchronize_snapshots(
    source_run,
    dest_run,
    store,
    source,
    destination,
    source_default,
    dest_default,
    archive_factory,
    switch_default,
):
    """Safely compare, archive when necessary, and reconcile two Git snapshots."""
    with timed("step git source-snapshot"):
        desired = capture_snapshot(source_run, store, source)
    observe("last_commit", getattr(desired, "latest", None))
    if not desired:
        return False
    new_ref = "refs/heads/" + source_default if isinstance(source_default, str) else ""
    if new_ref not in desired:
        raise RuntimeError("source default branch missing; refusing to prune")
    with timed("step git destination-snapshot"):
        observed = capture_snapshot(dest_run, store, destination)
    old_ref = "refs/heads/" + dest_default if isinstance(dest_default, str) else ""
    resume = False
    if observed:
        marker = seed_in_progress(dest_run, destination)
        default_ok = old_ref in observed and _default_branch(dest_run, destination) == old_ref
        if default_ok and not marker:
            # Build local source refs only from the pinned object IDs, not remote names.
            for ref, oid in desired.items():
                source_run(["-C", store, "update-ref", ref, oid])
            with timed("step git wipe-detection"):
                result = detect_wipe(
                    lambda args: source_run(["-C", store, *args]), observed[old_ref], desired[new_ref], desired
                )
            if result.is_wipe:
                with timed("step git wipe-archive"):
                    archive, set_archive_default = archive_factory()
                    archive_snapshot(dest_run, store, archive, observed)
                    set_archive_default(old_ref)
                    if _default_branch(dest_run, archive) != old_ref:
                        raise RuntimeError("archive default branch could not be verified")
        elif (marker or old_ref not in observed) and is_seed_preimage(source_run, store, observed, desired):
            # An interrupted first seed: every destination ref is at or behind
            # its snapshot ref, so completing it only fast-forwards and no wipe
            # decision is needed. The marker tells this apart from a routine
            # incremental run or a destination whose HEAD was changed elsewhere.
            resume = True
        else:
            raise RuntimeError("destination default branch missing or changed; refusing to prune")
    with timed("step git snapshot-push"):
        if observed and not resume and _default_branch(dest_run, destination) != old_ref:
            raise RuntimeError("destination default branch changed; refusing to push")
        push_snapshot(dest_run, store, destination, desired, observed, switch_default, resume=resume)
        with git_phase("default switch"):
            if observed:
                switch_default(new_ref)
            else:
                # GitHub may pick any branch from the first push as its default.
                switch_default(new_ref, initial_refs=desired)
    return True


def archive_name(mirror_name, timestamp):
    """Return the archive repository name for ``mirror_name`` at ``timestamp``."""
    if (
        not isinstance(mirror_name, str)
        or not NAME.fullmatch(mirror_name)
        or mirror_name in (".", "..")
        or not isinstance(timestamp, str)
        or not re.fullmatch(r"\d{8}T\d{6}Z", timestamp)
    ):
        raise ValueError("invalid archive identity")
    suffix = "-wipe-" + timestamp
    if len(mirror_name) + len(suffix) > 100:
        suffix = "-" + hashlib.sha256(mirror_name.lower().encode()).hexdigest()[:16] + suffix
    return mirror_name[: 100 - len(suffix)] + suffix


def create_archive(api, owner, mirror_name, account_type, timestamp):
    """Create an empty private archive, and verify Actions is disabled."""
    if (
        not isinstance(owner, str)
        or not NAME.fullmatch(owner)
        or owner in (".", "..")
        or account_type not in ("personal", "organization")
    ):
        raise ValueError("invalid archive identity")
    target_name = archive_name(mirror_name, timestamp)
    qowner, qname = quote(owner, safe=""), quote(target_name, safe="")
    target = f"{GITHUB}/repos/{qowner}/{qname}"
    endpoint = "/user/repos" if account_type == "personal" else f"/orgs/{qowner}/repos"
    created = api.request(
        "POST", GITHUB + endpoint, {"name": target_name, "private": True, "auto_init": False}
    )
    expected = (owner + "/" + target_name).lower()
    if (
        not isinstance(created, dict)
        or created.get("full_name", "").lower() != expected
        or created.get("private") is not True
    ):
        raise RuntimeError("archive creation identity or privacy could not be verified")
    api.request("PUT", target + "/actions/permissions", {"enabled": False})
    verified = api.request("GET", target)
    actions = api.request("GET", target + "/actions/permissions")
    if (
        not isinstance(verified, dict)
        or verified.get("full_name", "").lower() != expected
        or verified.get("private") is not True
        or not isinstance(actions, dict)
        or actions.get("enabled") is not False
    ):
        raise RuntimeError("archive privacy or Actions state could not be verified")
    return f"https://github.com/{qowner}/{qname}.git"


def set_archive_default(api, target, ref):
    """Set and read back an archive's default branch after refs exist."""
    if not isinstance(ref, str) or not ref.startswith("refs/heads/") or not ref[11:]:
        raise RuntimeError("archive default branch missing")
    branch = ref[11:]
    current = api.request("GET", target)
    if not isinstance(current, dict) or not isinstance(current.get("default_branch"), str) or not current["default_branch"]:
        raise RuntimeError("archive default branch missing")
    if current["default_branch"] != branch:
        api.request("PATCH", target, {"default_branch": branch})
        current = api.request("GET", target)
    if not isinstance(current, dict) or current.get("default_branch") != branch:
        raise RuntimeError("archive default branch could not be verified")


def is_wipe(old_files, new_files, old_commits, reachable_commits):
    """Detect a net file count drop and old commit loss of at least 75%."""
    return bool(old_files and old_commits) and (
        (len(old_files) - len(new_files)) * 4 >= len(old_files) * 3
        and len(old_commits - reachable_commits) * 4 >= len(old_commits) * 3
    )


def mirror_git(source_url, destination_url, token, after_push=None, *, safety=None):
    """Copy branches and tags using isolated, host-scoped Git credentials."""
    # Hooks are disabled, user/system Git configuration is isolated, and no checkout occurs.
    if safety is None:
        raise RuntimeError("safety context required for git mirroring")
    with tempfile.TemporaryDirectory(prefix="repo-mirror-") as tmp:
        env = os.environ.copy()
        env.pop("GH_TOKEN", None)
        env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "",
                "GIT_LFS_SKIP_SMUDGE": "1",
            }
        )
        for key in list(env):
            if key.startswith("GIT_") and key not in (
                "GIT_CONFIG_NOSYSTEM",
                "GIT_CONFIG_GLOBAL",
                "GIT_TERMINAL_PROMPT",
                "GIT_ASKPASS",
                "GIT_LFS_SKIP_SMUDGE",
            ):
                del env[key]

        def run(args, config_env=None):
            e = env.copy()
            if config_env:
                e.update(config_env)
            result = subprocess.run(
                ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "protocol.file.allow=never", *args],
                env=e,
                check=True,
                capture_output=True,
            )
            return result.stdout


        def safe_credentials():
            current = token() if callable(token) else token
            auth = base64.b64encode(("x-access-token:" + current).encode()).decode()
            return {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + auth,
            }

        github_source = bool(
            isinstance(source_url, str)
            and re.fullmatch(r"https://github\.com(?::443)?/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", source_url, re.IGNORECASE)
            and all(part not in (".", "..") for part in source_url.split("/")[-2:])
        )

        def source_run(args):
            remote_operation = source_url in args and ("ls-remote" in args or "fetch" in args)
            return run(args, safe_credentials() if github_source and remote_operation else None)

        run(["init", "--bare", tmp])
        return synchronize_snapshots(
            source_run,
            lambda args: run(args, safe_credentials()),
            tmp,
            source_url,
            destination_url,
            safety["source_default"],
            safety["dest_default"],
            safety["archive_factory"],
            safety["switch_default"],
        )
