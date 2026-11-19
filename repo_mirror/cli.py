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

from repo_mirror.adhoc import add_to_config, add_to_config_pinned, one_time_sources, parse_selected_url, _matches_owner
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
from repo_mirror.staleness import DEFAULT_INACTIVE_DAYS, observing, parse_inactive_days, stale, stamp_date


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
    parser.add_argument(
        "--inactive-after-days",
        default=str(DEFAULT_INACTIVE_DAYS),
        help="warn about repositories with no commit or release activity for this many days; 0 disables",
    )
    args = parser.parse_args(argv)
    inactive_days = parse_inactive_days(args.inactive_after_days)
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
    with open(args.config, 'rb') as f:
        from repo_mirror.config_json import unique_pairs
        config_bytes = f.read()
        config = json.loads(config_bytes, object_pairs_hook=unique_pairs)
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
    # Only URLs absent from the original configuration are one-time additions.
    # Explicit configured GitLab repositories still require historical pins.
    ephemeral_urls = set()
    if selected_urls and selected_urls.strip():
        for part in selected_urls.split(','):
            kind, base, source_owner, name = parse_selected_url(part.strip(), sources)
            if kind == 'gitlab' and not any(
                _matches_owner(s, kind, base, source_owner) and
                (s.get('repos') == 'all' or name.lower() in {n.lower() for n in s.get('repos', [])})
                for s in sources
            ):
                ephemeral_urls.add((base.lower(), source_owner.lower(), name.lower()))
    custom = os.environ.get("MIRROR_DESTINATION_NAME", "")
    visibility = os.environ.get("MIRROR_VISIBILITY") or "unchanged"
    if selected_urls and selected_urls.strip():
        add_to_config(config, selected_urls, custom, None if visibility == "unchanged" else visibility, protect_pins=False)
    elif custom or visibility not in ("private", "unchanged"):
        raise ValueError("destination settings require source URLs")
    for source in sources:
        validate_source(source)
    github_src, other_src, dst = SourceAPI(token), API(), API(token)
    if callable(getattr(github_src, 'restrict_origin', None)):
        github_src.restrict_origin('https://api.github.com')
    # Public GitLab sources must never inherit destination credentials. Keep a
    # separate, read-only client per origin so redirects and metadata reads
    # cannot escape to another configured source host.
    gitlab_clients = {}

    def source_client(source):
        if source["type"] == "github":
            return github_src
        if source["type"] == "gitlab":
            origin = source["base_url"].rstrip("/")
            if origin not in gitlab_clients:
                client = SourceAPI(None)
                client.restrict_origin(origin)
                gitlab_clients[origin] = client
            return gitlab_clients[origin]
        return other_src

    seen = set()
    plan = []
    discovery_failures = 0
    uncertain_prefixes = []
    uncertain_names = set()
    discovered_gitlab_ids = {}
    for index, source in enumerate(sources, 1):
        src = source_client(source)
        print(
            f"\n===== Source discovery {index}/{len(sources)}: {safe_label(source.get('owner'))} =====",
            file=sys.stderr,
            flush=True,
        )
        print(f"source {index}/{len(sources)} discovery started", file=sys.stderr, flush=True)
        try:
            with timed(f"step source-discovery source={index}"):
                if source['type'] == 'gitlab' and source['repos'] == 'all':
                    from repo_mirror.sources import discover_gitlab_projects
                    projects = discover_gitlab_projects(source, src)
                    names = list(projects)
                    discovered_gitlab_ids[index] = projects
                else:
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
    onboarding_store = None
    for source_index, source, name in plan:
        settings = source.get('destinations', {}).get(name.lower(), {})
        identity = (source['base_url'].lower().rstrip('/'), source['owner'].lower(), name.lower())
        if source['type'] == 'gitlab' and identity in ephemeral_urls and not {'source_id', 'destination_id'} <= settings.keys():
            raise ValueError('durable GitLab onboarding unavailable for one-time sources')
        if source['type'] == 'gitlab' and source['repos'] == 'all' and not {'source_id', 'destination_id'} <= settings.keys():
            if named_config or discovery_failures or ('source_id' in settings or 'destination_id' in settings):
                raise ValueError('GitLab group onboarding requires complete discovery and unbound configuration')
            from repo_mirror.config_json import unique_pairs
            authoritative_source = json.loads(config_bytes, object_pairs_hook=unique_pairs)['sources'][source_index - 1]
            if source != authoritative_source:
                raise ValueError('runtime source differs from authoritative configuration')
            if onboarding_store is None:
                from repo_mirror.gitlab_onboarding_github import store_from_environment
                onboarding_store = store_from_environment(os.environ, args.config)
                if onboarding_store is None:
                    raise ValueError('durable GitLab onboarding unavailable: management configuration required')
                if onboarding_store.read()[0] != config_bytes:
                    raise ValueError('authoritative configuration differs from checkout')
    # Include one-time sources before workers start; no concurrent cache writes.
    for _, source, _ in plan:
        source_client(source)
    failures = []
    inactive = []
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
                existing = get_existing(dst, target)
                check_existing(existing, marker)
                if source['type'] == 'gitlab' and source['repos'] != 'all' and not {'source_id', 'destination_id'} <= settings.keys():
                    raise ValueError('explicit GitLab source requires both durable identity pins')
                if source['type'] == 'gitlab' and existing is not None and 'destination_id' not in settings:
                    raise ValueError('unpinned existing destination requires manual reconciliation')
                identity = (source['base_url'].lower().rstrip('/'), source['owner'].lower(), name.lower())
                onboard = source['type'] == 'gitlab' and (
                    source['repos'] == 'all' or identity in ephemeral_urls
                ) and not {'source_id', 'destination_id'} <= settings.keys()
                if onboard:
                    import copy
                    from repo_mirror.repository import validate_source_clone_url
                    from repo_mirror.sources import source_api_base
                    ephemeral = copy.deepcopy(source)
                    ephemeral['repos'] = [name]
                    entry = ephemeral.setdefault('destinations', {}).setdefault(name.lower(), {})
                    if source['repos'] == 'all':
                        sid = discovered_gitlab_ids[source_index][name]
                    else:
                        upstream = source_api_base(source) + '/projects/' + quote(source['owner'] + '/' + name, safe='')
                        project = source_client(source).request('GET', upstream)
                        if (not isinstance(project, dict) or project.get('visibility') != 'public'
                                or project.get('path_with_namespace') != source['owner'] + '/' + name
                                or type(project.get('id')) is not int or project['id'] <= 0):
                            raise ValueError('invalid GitLab project identity')
                        validate_source_clone_url(source, name, {'clone_url': project.get('http_url_to_repo')})
                        sid = project['id']
                    if 'source_id' in entry and entry['source_id'] != sid:
                        raise ValueError('source repository id mismatch')
                    entry['source_id'] = sid
                    scratch = {'destination_account_type': account_type, 'sources': [ephemeral]}
                    url = source['base_url'].rstrip('/') + '/' + source['owner'] + '/' + name
                    try:
                        if onboarding_store is None:
                            raise ValueError('authoritative configuration unavailable')
                        if onboarding_store.read()[0] != config_bytes:
                            raise ValueError('authoritative configuration changed before preparation')
                        from repo_mirror.config_json import unique_pairs
                        authoritative_source = json.loads(config_bytes, object_pairs_hook=unique_pairs)['sources'][source_index - 1]
                        if source != authoritative_source:
                            raise ValueError('runtime source differs from authoritative configuration')
                        add_to_config_pinned(scratch, url, github_src, source_client(source), dst,
                                             owner=owner, account_type=account_type)
                        prepared = scratch['sources'][0]['destinations'][name.lower()]
                        from repo_mirror.gitlab_onboarding_github import publish_binding
                        config_bytes = publish_binding(onboarding_store, config_bytes, source_index - 1,
                                                       name, sid, prepared['destination_id'])
                    except Exception as exc:
                        print('GitLab onboarding failed; any created destination requires manual reconciliation',
                              file=sys.stderr, flush=True)
                        raise RuntimeError('GitLab onboarding failed; any created destination requires manual reconciliation') from exc
                    source.setdefault('destinations', {}).setdefault(name.lower(), {}).update(
                        source_id=sid, destination_id=prepared['destination_id'])
            ready.append((repo_index, source_index, source, name))
        except Exception as exc:
            failures.append((repo_index, error_detail(exc)))
    succeeded = 0
    relocations = []
    original_stdout, original_stderr = sys.stdout, sys.stderr
    stdout_router, stderr_router = _ThreadStream(original_stdout), _ThreadStream(original_stderr)

    def run_repository(item):
        repo_index, source_index, source, name = item
        log = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        stdout_router.local.stream = stderr_router.local.stream = log
        src = source_client(source)
        relocation = None
        activity = {}
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
                # The worker thread owns this collector; nothing crosses threads.
                with observing() as activity:
                    if journal is None:
                        relocation = mirror_repository(source, name, owner, src, dst, token, account_type)
                    else:
                        relocation = mirror_repository(source, name, owner, src, dst, token, account_type,
                                                       journal=journal)
                if isinstance(relocation, dict):
                    print(f"repository {repo_index} source relocation detected; migration deferred", file=sys.stderr, flush=True)
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
        return repo_index, detail, log, relocation, activity

    sys.stdout, sys.stderr = stdout_router, stderr_router
    processing_started = time.monotonic()
    try:
        max_workers = 16
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            items = iter(ready)
            pending = {pool.submit(run_repository, item): item for item in (next(items, None) for _ in range(max_workers)) if item}
            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    item = pending.pop(future)
                    try:
                        repo_index, detail, log, relocation, activity = future.result()
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
                        idle = stale(activity, inactive_days)
                        if idle is not None:
                            days, stamp = idle
                            inactive.append((repo_index, item[1], item[2], item[3], days, stamp))
                    if detail is None and isinstance(relocation, dict):
                        # The copy succeeded; the configuration change is a
                        # separate, deferred transaction that must be recorded
                        # durably before any destination rename.
                        relocations.append((repo_index, relocation))
                        succeeded += 1
                    elif detail is None:
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
    relocations.sort(key=lambda item: item[0])
    # A detected move is only a plan: renaming the destination and publishing
    # the updated configuration is a separate, journaled transaction that must
    # be resumable after a crash. Without a durable management store nothing is
    # renamed and the relocation stays reported.
    migration_failures = []
    deferral = 'management migration store unavailable'
    transaction = None
    if named_config:
        try:
            from repo_mirror.github_migration import transaction_from_environment
            transaction = transaction_from_environment(os.environ, args.config, owner, destination_api=dst)
        except Exception as exc:
            if relocations:
                migration_failures.append(f"migration store unavailable: {error_detail(exc)}")
    else:
        deferral = 'configuration format does not support automatic migration'
    if transaction is None:
        for repo_index, _ in relocations:
            print(
                f"repository {repo_index} migration deferred: {deferral}",
                file=sys.stderr,
                flush=True,
            )
    else:
        try:
            resumed = transaction.recover()
            if resumed:
                print(f"migration recovery resumed={resumed}", file=sys.stderr, flush=True)
        except Exception as exc:
            detail = error_detail(exc)
            migration_failures.append(f"migration recovery failed: {detail}")
            print(f"migration recovery failed: {detail}", file=sys.stderr, flush=True)
        for repo_index, relocation in relocations:
            try:
                with timed(f"step migration source={repo_index}"):
                    entry = transaction.migrate(relocation)
                if entry is None:
                    print(f"repository {repo_index} migration already applied", file=sys.stderr, flush=True)
                else:
                    print(f"repository {repo_index} migration completed", file=sys.stderr, flush=True)
            except Exception as exc:
                detail = error_detail(exc)
                migration_failures.append(f"repository {repo_index} migration failed: {detail}")
                print(f"repository {repo_index} migration failed: {detail}", file=sys.stderr, flush=True)
    print("\n===== Summary =====", file=sys.stderr, flush=True)
    print(
        f"summary sources={len(sources)} discovery_failed={discovery_failures} repositories={len(plan)} succeeded={succeeded} failed={len(failures)}",
        file=sys.stderr,
        flush=True,
    )
    if inactive:
        inactive.sort()
        print(
            f"inactive: {len(inactive)} of {len(plan)} repositories had no commit or release activity "
            f"in the last {inactive_days} days",
            file=sys.stderr,
            flush=True,
        )
        for repo_index, source_index, source, name, days, stamp in inactive:
            print(
                f"inactive repository {repo_index} (source {source_index}/{len(sources)}, "
                f"{safe_label(source.get('owner'))}/{safe_label(name)}): "
                f"last activity {stamp_date(stamp)} ({days} days)",
                file=sys.stderr,
                flush=True,
            )
    if failures or discovery_failures or migration_failures:
        for repo_index, kind in failures:
            source_index, source, name = plan[repo_index - 1]
            print(
                f"mirror failed for repository {repo_index} (source {source_index}/{len(sources)}, {safe_label(source.get('owner'))}/{safe_label(name)}): {kind}",
                file=sys.stderr,
                flush=True,
            )
        for detail in migration_failures:
            print(f"migration failed: {detail}", file=sys.stderr, flush=True)
        raise RuntimeError(
            f"{discovery_failures} sources and {len(failures)} repositories failed"
            + (f" and {len(migration_failures)} migrations failed" if migration_failures else "")
        )
    return sorted(relocations, key=lambda item: item[0])
