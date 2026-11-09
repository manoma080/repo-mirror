"""git_sync: see README for an overview."""

import base64
import os
import re
import subprocess
import tempfile
from urllib.parse import quote

from repo_mirror.constants import GITHUB, NAME
from repo_mirror.logging_utils import timed
from repo_mirror.snapshot_push import _default_branch, archive_snapshot, capture_snapshot, git_phase, push_snapshot
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
    if not desired:
        return False
    new_ref = "refs/heads/" + source_default if isinstance(source_default, str) else ""
    if new_ref not in desired:
        raise RuntimeError("source default branch missing; refusing to prune")
    with timed("step git destination-snapshot"):
        observed = capture_snapshot(dest_run, store, destination)
    old_ref = "refs/heads/" + dest_default if isinstance(dest_default, str) else ""
    if observed:
        if old_ref not in observed or _default_branch(dest_run, destination) != old_ref:
            raise RuntimeError("destination default branch missing or changed; refusing to prune")
        # Build local source refs only from the pinned object IDs, not remote names.
        for ref, oid in desired.items():
            source_run(["-C", store, "update-ref", ref, oid])
        with timed("step git wipe-detection"):
            result = detect_wipe(
                lambda args: source_run(["-C", store, *args]), observed[old_ref], desired[new_ref], desired
            )
        if result.is_wipe:
            with timed("step git wipe-archive"):
                archive = archive_factory()
                archive_snapshot(dest_run, store, archive, observed)
    with timed("step git snapshot-push"):
        if observed and _default_branch(dest_run, destination) != old_ref:
            raise RuntimeError("destination default branch changed; refusing to push")
        push_snapshot(dest_run, store, destination, desired, observed, switch_default)
        with git_phase("default switch"):
            if observed:
                switch_default(new_ref)
            else:
                # GitHub may pick any branch from the first push as its default.
                switch_default(new_ref, initial_refs=desired)
    return True


def create_archive(api, owner, mirror_name, account_type, timestamp):
    """Create an empty private archive, and verify Actions is disabled."""
    if (
        not isinstance(owner, str)
        or not NAME.fullmatch(owner)
        or owner in (".", "..")
        or not isinstance(mirror_name, str)
        or not NAME.fullmatch(mirror_name)
        or mirror_name in (".", "..")
        or account_type not in ("personal", "organization")
        or not isinstance(timestamp, str)
        or not re.fullmatch(r"\d{8}T\d{6}Z", timestamp)
    ):
        raise ValueError("invalid archive identity")
    suffix = "-wipe-" + timestamp
    archive_name = mirror_name[: 100 - len(suffix)] + suffix
    qowner, qname = quote(owner, safe=""), quote(archive_name, safe="")
    target = f"{GITHUB}/repos/{qowner}/{qname}"
    endpoint = "/user/repos" if account_type == "personal" else f"/orgs/{qowner}/repos"
    created = api.request(
        "POST", GITHUB + endpoint, {"name": archive_name, "private": True, "auto_init": False}
    )
    expected = (owner + "/" + archive_name).lower()
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


def is_wipe(old_files, new_files, old_commits, reachable_commits):
    """Detect a net file count drop and old commit loss of at least 75%."""
    return bool(old_files and old_commits) and (
        (len(old_files) - len(new_files)) * 4 >= len(old_files) * 3
        and len(old_commits - reachable_commits) * 4 >= len(old_commits) * 3
    )


def mirror_git(source_url, destination_url, token, after_push=None, *, safety=None):
    # Git is isolated from user/system configuration and never receives the token
    # while contacting the source. Hooks are disabled and no checkout occurs.
    """Copy every branch and tag from the source Git repo to the destination, using a token-safe push."""
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

        run(["init", "--bare", tmp])
        return synchronize_snapshots(
            run,
            lambda args: run(args, safe_credentials()),
            tmp,
            source_url,
            destination_url,
            safety["source_default"],
            safety["dest_default"],
            safety["archive_factory"],
            safety["switch_default"],
        )
