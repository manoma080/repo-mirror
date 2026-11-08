"""cli: see README for an overview."""

import argparse
import json
import os
import sys
from urllib.parse import quote

from repo_mirror.constants import GITHUB, NAME
from repo_mirror.github_api import API, AppTokenProvider, SourceAPI
from repo_mirror.journal import FailureJournal, ReleaseRecoveryError
from repo_mirror.logging_utils import error_detail, safe_label, timed, timed_main
from repo_mirror.repository import mirror_repository
from repo_mirror.source_filter import filter_plan
from repo_mirror.sources import (
    check_existing,
    destination_name,
    discover,
    get_existing,
    source_marker,
    validate_source,
)


@timed_main
def main(argv=None):
    """Command-line entry point: load the config, mirror each source, and report failures."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--failures")
    args = parser.parse_args(argv)
    if not os.environ.get("MIRROR_TIMESTAMP_SECRET"):
        raise ReleaseRecoveryError("secret")
    journal = FailureJournal(args.failures) if args.failures else None
    token, owner = os.environ.get("GH_TOKEN"), os.environ.get("DESTINATION_OWNER")
    app_id = os.environ.get("MIRROR_APP_ID")
    private_key = os.environ.pop("MIRROR_APP_PRIVATE_KEY", None)
    if (
        (not token and not (app_id and private_key))
        or not owner
        or not NAME.fullmatch(owner)
        or owner in (".", "..")
    ):
        raise ValueError("GH_TOKEN and valid DESTINATION_OWNER are required")
    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    account_type = os.environ.get("DESTINATION_ACCOUNT_TYPE", config.get("destination_account_type"))
    if account_type not in ("personal", "organization"):
        raise ValueError("destination_account_type must be personal or organization")
    if app_id or private_key:
        token = AppTokenProvider(app_id or "", private_key, owner, account_type)
    sources = config.get("sources")
    if not isinstance(sources, list):
        raise ValueError("sources must be a list")
    for source in sources:
        validate_source(source)
    github_src, other_src, dst = SourceAPI(token), API(), API(token)
    seen = set()
    plan = []
    discovery_failures = 0
    uncertain_prefixes = []
    uncertain_names = set()
    for index, source in enumerate(sources, 1):
        src = github_src if source["type"] == "github" else other_src
        print(
            f"\n===== Source discovery {index}/{len(sources)}: {safe_label(source.get('owner'))} =====",
            file=sys.stderr,
            flush=True,
        )
        print(f"source {index}/{len(sources)} discovery started", file=sys.stderr, flush=True)
        try:
            with timed(f"step source-discovery source={index}"):
                names = discover(source, src)
        except Exception as exc:
            discovery_failures += 1
            if source["repos"] == "all":
                uncertain_prefixes.append(source.get("prefix", "mirror-" + source["owner"] + "-").lower())
            else:
                uncertain_names.update(destination_name(source, name).lower() for name in source["repos"])
            print(
                f"source discovery failed for source {index}: {error_detail(exc)}",
                file=sys.stderr,
                flush=True,
            )
            continue
        print(
            f"source {index}/{len(sources)} discovery completed discovered={len(names)}",
            file=sys.stderr,
            flush=True,
        )
        for name in names:
            target = destination_name(source, name).lower()
            if target in seen:
                raise ValueError("duplicate destination repository")
            seen.add(target)
            plan.append((index, source, name))
    selected_urls = os.environ.get("MIRROR_SOURCE_URLS")
    if selected_urls and selected_urls.strip() and discovery_failures:
        raise RuntimeError("filtered run requires successful discovery of every source")
    plan = filter_plan(plan, selected_urls)
    failures = []
    ready = []
    for repo_index, (source_index, source, name) in enumerate(plan, 1):
        dest = destination_name(source, name)
        if dest.lower() in uncertain_names or any(
            dest.lower().startswith(prefix) for prefix in uncertain_prefixes
        ):
            failures.append(
                (
                    repo_index,
                    "possible collision with undiscovered source; retry discovery or use distinct prefixes",
                )
            )
            continue
        target = f"{GITHUB}/repos/{quote(owner, safe='')}/{quote(dest, safe='')}"
        try:
            with timed(
                f"step destination-preflight source_repo={safe_label(source.get('owner'))}/{safe_label(name)} target=destination"
            ):
                check_existing(get_existing(dst, target), source_marker(source, name))
            ready.append((repo_index, source_index, source, name))
        except Exception as exc:
            failures.append((repo_index, error_detail(exc)))
    succeeded = 0
    for repo_index, source_index, source, name in ready:
        src = github_src if source["type"] == "github" else other_src
        print(
            f"\n===== Repository {repo_index}/{len(plan)}: {safe_label(source.get('owner'))}/{safe_label(name)} =====",
            file=sys.stderr,
            flush=True,
        )
        print(
            f"repository {repo_index}/{len(plan)} started (source {source_index}/{len(sources)})",
            file=sys.stderr,
            flush=True,
        )
        try:
            if journal is None:
                mirror_repository(source, name, owner, src, dst, token, account_type)
            else:
                mirror_repository(source, name, owner, src, dst, token, account_type, journal=journal)
            succeeded += 1
            print(f"repository {repo_index}/{len(plan)} completed", file=sys.stderr, flush=True)
        except Exception as exc:
            detail = error_detail(exc)
            failures.append((repo_index, detail))
            print(f"repository {repo_index}/{len(plan)} failed: {detail}", file=sys.stderr, flush=True)
    print("\n===== Summary =====", file=sys.stderr, flush=True)
    print(
        f"summary sources={len(sources)} discovery_failed={discovery_failures} repositories={len(plan)} succeeded={succeeded} failed={len(failures)}",
        file=sys.stderr,
        flush=True,
    )
    if failures or discovery_failures:
        for repo_index, kind in failures:
            source_index, source, name = plan[repo_index - 1]
            print(
                f"mirror failed for repository {repo_index} (source {source_index}/{len(sources)}, {safe_label(source.get('owner'))}/{safe_label(name)}): {kind}",
                file=sys.stderr,
                flush=True,
            )
        raise RuntimeError(f"{discovery_failures} sources and {len(failures)} repositories failed")
