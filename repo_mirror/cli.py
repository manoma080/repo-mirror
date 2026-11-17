"""cli: see README for an overview."""

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import quote

from repo_mirror.adhoc import add_to_config, one_time_sources
from repo_mirror.config_named import normalize_config
from repo_mirror.constants import GITHUB, NAME, REPOSITORY_TIME
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


class _ThreadStream:
    """Route worker output to a per-repository temporary file."""

    def __init__(self, fallback):
        self.fallback = fallback
        self.local = threading.local()

    def write(self, text):
        return getattr(self.local, "stream", self.fallback).write(text)

    def flush(self):
        return getattr(self.local, "stream", self.fallback).flush()

    def __getattr__(self, name):
        return getattr(self.fallback, name)


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
        from repo_mirror.config_json import unique_pairs
        config = json.load(f, object_pairs_hook=unique_pairs)
    named_config = isinstance(config, dict) and ('destinations' in config or 'repositories' in config)
    config = normalize_config(config)
    if 'destination_profile' in config:
        from repo_mirror.destination_profile import validate_runtime
        validate_runtime(config['destination_profile'], 'github', 'https://github.com', owner, 'mirrorbot')
        if not app_id or not private_key:
            raise ValueError('mirrorbot app credentials required')
    environment_type = os.environ.get("DESTINATION_ACCOUNT_TYPE") or None
    if named_config and environment_type is not None and environment_type != config['destination_account_type']:
        raise ValueError("destination account type mismatch")
    account_type = environment_type or config.get("destination_account_type")
    if account_type not in ("personal", "organization"):
        raise ValueError("destination_account_type must be personal or organization")
    if app_id or private_key:
        token = AppTokenProvider(app_id or "", private_key, owner, account_type)
    sources = config.get("sources")
    if not isinstance(sources, list):
        raise ValueError("sources must be a list")
    selected_urls = os.environ.get("MIRROR_SOURCE_URLS")
    custom = os.environ.get("MIRROR_DESTINATION_NAME", "")
    visibility = os.environ.get("MIRROR_VISIBILITY") or "unchanged"
    if selected_urls and selected_urls.strip():
        add_to_config(config, selected_urls, custom, None if visibility == "unchanged" else visibility, protect_pins=False)
    elif custom or visibility not in ("private", "unchanged"):
        raise ValueError("destination settings require source URLs")
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
                uncertain_names.update(destination_name(source, name).lower() for name in source.get("destinations", {}))
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
    # URLs not in config.json become one-time sources: mirrored now, never saved.
    for source in one_time_sources(selected_urls, plan, sources):
        sources.append(source)
        name = source["repos"][0]
        target = destination_name(source, name).lower()
        if target in seen:
            raise ValueError("duplicate destination repository")
        seen.add(target)
        plan.append((len(sources), source, name))
    plan = filter_plan(plan, selected_urls, case_insensitive=True)
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
                settings = source.get("destinations", {}).get(name.lower(), {})
                legacy = settings.get("legacy")
                if legacy is not None:
                    from repo_mirror.migration_attestation import github_claim, verify
                    verify(github_claim(source, name, owner), legacy.get("signature"), os.environ.get("MIRROR_TIMESTAMP_SECRET", ""))
                marker = (source_marker({**source, "owner": legacy["source_owner"]}, legacy["source_name"])
                          if legacy is not None else source_marker(source, name))
                check_existing(get_existing(dst, target), marker)
            ready.append((repo_index, source_index, source, name))
        except Exception as exc:
            failures.append((repo_index, error_detail(exc)))
    succeeded = 0
    original_stdout, original_stderr = sys.stdout, sys.stderr
    stdout_router, stderr_router = _ThreadStream(original_stdout), _ThreadStream(original_stderr)

    def run_repository(item):
        repo_index, source_index, source, name = item
        log = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        stdout_router.local.stream = stderr_router.local.stream = log
        src = github_src if source["type"] == "github" else other_src
        try:
            print(
                f"\n\n===== Repository {repo_index}/{len(plan)}: {safe_label(source.get('owner'))}/{safe_label(name)} =====",
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
                detail = None
                print(f"repository {repo_index}/{len(plan)} completed", file=sys.stderr, flush=True)
            except Exception as exc:
                detail = error_detail(exc)
                print(f"repository {repo_index}/{len(plan)} failed: {detail}", file=sys.stderr, flush=True)
        except Exception:
            try:
                log.close()
            except Exception:
                pass
            raise
        finally:
            del stdout_router.local.stream, stderr_router.local.stream
        return repo_index, detail, log

    sys.stdout, sys.stderr = stdout_router, stderr_router
    processing_started = time.monotonic()
    try:
        max_workers = 8
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            items = iter(ready)
            pending = {pool.submit(run_repository, item): item for item in (next(items, None) for _ in range(max_workers)) if item}
            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    item = pending.pop(future)
                    try:
                        repo_index, detail, log = future.result()
                    except Exception as exc:
                        failures.append((item[0], error_detail(exc)))
                        item = next(items, None)
                        if item is not None:
                            pending[pool.submit(run_repository, item)] = item
                        continue
                    try:
                        log.seek(0)
                        while chunk := log.read(65536):
                            original_stderr.write(chunk)
                        original_stderr.flush()
                    except Exception as exc:
                        detail = detail or error_detail(exc)
                    finally:
                        try:
                            log.close()
                        except Exception as exc:
                            detail = detail or error_detail(exc)
                    if detail is None:
                        succeeded += 1
                    else:
                        failures.append((repo_index, detail))
                    item = next(items, None)
                    if item is not None:
                        pending[pool.submit(run_repository, item)] = item
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr
        counter = REPOSITORY_TIME.get()
        if counter is not None and ready:
            counter[0] += time.monotonic() - processing_started
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
