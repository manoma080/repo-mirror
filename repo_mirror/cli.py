"""cli: see README for an overview."""

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import quote

from repo_mirror.adhoc import classify_source_url, one_time_sources, _matches_owner
from repo_mirror.config_named import normalize_config
from repo_mirror.constants import GITHUB, NAME, REPOSITORY_TIME
from repo_mirror.github_api import API, AppTokenProvider, SourceAPI
from repo_mirror.host_limits import CONFIG_KEY, HostAdmission, HostLimits, source_host, take_admissible
from repo_mirror.journal import FailureJournal, ReleaseRecoveryError
from repo_mirror.logging_utils import error_detail, safe_label, timed, timed_main
from repo_mirror.repository import mirror_repository
from repo_mirror.source_filter import filter_plan
from repo_mirror.sources import (
    check_existing,
    destination_name,
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
    max_workers = 16
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
    config = normalize_config(config)
    if 'destination_profile' in config:
        from repo_mirror.destination_profile import validate_runtime
        validate_runtime(config['destination_profile'], 'github', 'https://github.com', owner, 'mirrorbot')
        if not app_id or not private_key:
            raise ValueError('mirrorbot app credentials required')
    environment_type = os.environ.get("DESTINATION_ACCOUNT_TYPE") or None
    if environment_type is not None and environment_type != config['account_type']:
        raise ValueError("destination account type mismatch")
    account_type = environment_type or config['account_type']
    if account_type not in ("personal", "organization"):
        raise ValueError("account_type must be personal or organization")
    if app_id or private_key:
        token = AppTokenProvider(app_id or "", private_key, owner, account_type)
    entries = config["repositories"]
    host_limits = HostLimits.from_config(config.get(CONFIG_KEY), default=max_workers)
    selected_urls = os.environ.get("MIRROR_SOURCE_URLS")
    custom = os.environ.get("MIRROR_DESTINATION_NAME", "")
    visibility = os.environ.get("MIRROR_VISIBILITY") or "unchanged"
    ignore_override = os.environ.get("MIRROR_IGNORE_RELEASES") or "keep"
    if ignore_override not in ("keep", "true", "false"):
        raise ValueError("invalid ignore-releases override")
    if ignore_override != "keep" and not (selected_urls and selected_urls.strip()):
        raise ValueError("ignore-releases override requires source URLs")
    if not (selected_urls and selected_urls.strip()) and (custom or visibility not in ("private", "unchanged")):
        raise ValueError("destination settings require source URLs")
    github_src, other_src, dst = SourceAPI(token), API(), API(token)
    if callable(getattr(github_src, 'restrict_origin', None)):
        github_src.restrict_origin('https://api.github.com')
    # Public GitLab and Forgejo sources must never inherit destination
    # credentials. Keep a separate, read-only client per origin so redirects
    # and metadata reads cannot escape to another configured source host.
    gitlab_clients = {}
    forgejo_clients = {}

    def source_client(source):
        if source["type"] == "github":
            return github_src
        if source["type"] == "gitlab":
            origin = source["base_url"].rstrip("/")
            if origin not in gitlab_clients:
                client = SourceAPI(None)
                if callable(getattr(client, 'restrict_origin', None)):
                    client.restrict_origin(origin)
                gitlab_clients[origin] = client
            return gitlab_clients[origin]
        if type(other_src) is API:
            origin = source["base_url"].rstrip("/")
            if origin not in forgejo_clients:
                client = SourceAPI(None)
                if callable(getattr(client, 'restrict_origin', None)):
                    client.restrict_origin(origin)
                forgejo_clients[origin] = client
            return forgejo_clients[origin]
        return other_src

    pre_transaction = None
    onboarding_result = None
    if config.get('auto_discoveries'):
        from repo_mirror.auto_discoveries_onboarding import onboard_auto_discoveries
        from repo_mirror.github_migration import store_from_environment, transaction_from_environment

        pre_transaction = transaction_from_environment(os.environ, args.config, owner, destination_api=dst)
        if pre_transaction is not None:
            resumed = pre_transaction.recover()
            if resumed or pre_transaction.store.pending():
                raise ValueError('pending migration requires reconciliation')
        onboarding_store = store_from_environment(os.environ, args.config)
        if onboarding_store is None:
            raise ValueError('auto-discovery onboarding requires authoritative management configuration')
        onboarding_result = onboard_auto_discoveries(
            config_bytes,
            onboarding_store,
            source_client,
            dst,
            owner=owner,
            account_type=account_type,
        )
        config_bytes = onboarding_result.config_bytes
        config = normalize_config(json.loads(config_bytes, object_pairs_hook=unique_pairs))
        entries = config["repositories"]
        if selected_urls and selected_urls.strip() and onboarding_result.relocated_rules:
            relocated_map = {old.lower(): new for old, new in onboarding_result.relocated_rules}
            selected_urls = ','.join(
                relocated_map.get(part.strip().lower(), part.strip())
                for part in selected_urls.split(',')
            )

    if visibility not in ("unchanged", "private", "public"):
        raise ValueError("invalid visibility")

    # Only URLs absent from the original configuration are one-time additions.
    # Explicit configured GitLab repositories still require historical pins.
    gitlab_probe = None
    selected_rule_urls = set()
    selected_repo_identities = set()
    if selected_urls and selected_urls.strip():
        gitlab_probe = source_client({'type': 'gitlab', 'base_url': 'https://gitlab.com'})
        lookup_cfg = json.loads(config_bytes, object_pairs_hook=unique_pairs)
        classified_selected = [
            classify_source_url(part.strip(), lookup_cfg, gitlab_api=gitlab_probe)
            for part in selected_urls.split(',')
        ]
        if custom and any(item.kind == 'rule' for item in classified_selected):
            raise ValueError('destination name is derived per repository for a discovery rule')
        repo_items = [item for item in classified_selected if item.kind == 'repository']
        if custom and (len(repo_items) != 1 or len(custom) > 100 or not NAME.fullmatch(custom) or custom in ('.', '..')):
            raise ValueError('custom destination requires one URL and a valid repository name')
        repo_identities = [(item.source_type, item.base_url.lower(), item.owner.lower(), item.name.lower()) for item in repo_items]
        if len(set(repo_identities)) != len(repo_identities):
            raise ValueError('duplicate source repository')
        for item in classified_selected:
            if item.kind == 'rule':
                if not item.existing:
                    raise ValueError('one-time mirroring is not supported for owner URLs; enable add_to_config')
                selected_rule_urls.add(item.url.lower())
                continue
            selected_repo_identities.add((item.base_url.lower().rstrip('/'), item.owner.lower(), item.name.lower()))
            matched_entry = next(
                (e for e in entries if _matches_owner(e, item.source_type, item.base_url, item.owner) and e['name'].lower() == item.name.lower()),
                None,
            )
            if matched_entry is not None:
                if custom or visibility != "unchanged":
                    mirror = matched_entry.setdefault('mirror', {})
                    if custom:
                        mirror['name'] = custom
                    if visibility != "unchanged":
                        mirror['visibility'] = visibility
            elif item.source_type == 'gitlab':
                raise ValueError('durable GitLab onboarding unavailable for one-time sources')

    seen = set()
    plan = []
    for index, entry in enumerate(entries, 1):
        validate_source(entry)
        name = entry['name']
        target = destination_name(entry, name).lower()
        if target in seen:
            raise ValueError("duplicate destination repository")
        seen.add(target)
        plan.append((index, entry, name))
    # URLs not in config.json become one-time sources: mirrored now, never saved.
    for entry in one_time_sources(
        selected_urls,
        plan,
        entries,
        auto_discoveries=config.get('auto_discoveries', ()),
        gitlab_api=gitlab_probe,
    ):
        if custom or visibility != "unchanged":
            mirror = entry.setdefault('mirror', {})
            if custom:
                mirror['name'] = custom
            if visibility != "unchanged":
                mirror['visibility'] = visibility
        validate_source(entry)
        entries.append(entry)
        name = entry["name"]
        target = destination_name(entry, name).lower()
        if target in seen:
            raise ValueError("duplicate destination repository")
        seen.add(target)
        plan.append((len(entries), entry, name))
    known_rules = {rule['url'] for rule in config.get('auto_discoveries', ())}
    plan = filter_plan(plan, selected_urls, case_insensitive=True, known_rules=known_rules)
    if ignore_override != "keep":
        # A one-run override covers exactly the sources named in this run and is
        # never written back to the configuration.
        override = ignore_override == "true"
        for _, source, name in plan:
            identity = (source['base_url'].lower().rstrip('/'), source['owner'].lower(), name.lower())
            discovered_by = str(source.get('discovered_by', '')).lower()
            if identity in selected_repo_identities or (discovered_by and discovered_by in selected_rule_urls):
                source.setdefault('mirror', {})['ignore_releases'] = override
    # Include one-time sources before workers start; no concurrent cache writes.
    for _, source, _ in plan:
        source_client(source)
    failures = []
    inactive = []
    ready = []
    for repo_index, (source_index, source, name) in enumerate(plan, 1):
        dest = destination_name(source, name)
        target = f"{GITHUB}/repos/{quote(owner, safe='')}/{quote(dest, safe='')}"
        try:
            with timed(
                f"step destination-preflight source_repo={safe_label(source.get('owner'))}/{safe_label(name)} target=destination"
            ):
                settings = source.get("mirror", {})
                legacy = settings.get("legacy")
                if legacy is not None:
                    from repo_mirror.migration_attestation import github_claim, verify
                    verify(github_claim(source, name, owner), legacy.get("signature"), os.environ.get("MIRROR_TIMESTAMP_SECRET", ""))
                marker = (source_marker({**source, "owner": legacy["source_owner"]}, legacy["source_name"])
                          if legacy is not None else source_marker(source, name))
                existing = get_existing(dst, target)
                check_existing(existing, marker)
                if source['type'] == 'gitlab' and not {'source_id', 'destination_id'} <= settings.keys():
                    raise ValueError('explicit GitLab source requires both durable identity pins')
                if source['type'] == 'gitlab' and existing is not None and 'destination_id' not in settings:
                    raise ValueError('unpinned existing destination requires manual reconciliation')
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
                f"repository {repo_index}/{len(plan)} started (source {source_index}/{len(entries)})",
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
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            queue = deque(ready)
            admission = HostAdmission(host_limits)

            def item_host(item):
                return source_host(item[2]["base_url"])

            def fill():
                # Never exceed a source host's cap, but keep other hosts busy
                # while one of them is saturated.
                while queue and len(pending) < max_workers:
                    item = take_admissible(queue, admission, item_host)
                    if item is None:
                        return
                    admission.acquire(item_host(item))
                    pending[pool.submit(run_repository, item)] = item

            pending = {}
            fill()
            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    item = pending.pop(future)
                    admission.release(item_host(item))
                    try:
                        repo_index, detail, log, relocation, activity = future.result()
                    except Exception as exc:
                        failures.append((item[0], error_detail(exc)))
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
                fill()
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
    try:
        if pre_transaction is not None:
            transaction = pre_transaction
        elif relocations:
            from repo_mirror.github_migration import transaction_from_environment
            transaction = transaction_from_environment(os.environ, args.config, owner, destination_api=dst)
    except Exception as exc:
        if relocations:
            migration_failures.append(f"migration store unavailable: {error_detail(exc)}")
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
        f"summary sources={len(entries)} discovery_failed=0 repositories={len(plan)} succeeded={succeeded} failed={len(failures)}",
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
            ignored = source.get('mirror', {}).get('ignore_releases') is True
            print(
                f"inactive repository {repo_index} (source {source_index}/{len(entries)}, "
                f"{safe_label(source.get('owner'))}/{safe_label(name)}): "
                f"last activity {stamp_date(stamp)} ({days} days)"
                + (" (releases ignored; commit activity only)" if ignored else ""),
                file=sys.stderr,
                flush=True,
            )
    if onboarding_result is not None:
        from repo_mirror.auto_discoveries_onboarding import report_orphans
        report_orphans(onboarding_result)
    if failures or migration_failures:
        for repo_index, kind in failures:
            source_index, source, name = plan[repo_index - 1]
            print(
                f"mirror failed for repository {repo_index} (source {source_index}/{len(entries)}, {safe_label(source.get('owner'))}/{safe_label(name)}): {kind}",
                file=sys.stderr,
                flush=True,
            )
        for detail in migration_failures:
            print(f"migration failed: {detail}", file=sys.stderr, flush=True)
        raise RuntimeError(
            f"0 sources and {len(failures)} repositories failed"
            + (f" and {len(migration_failures)} migrations failed" if migration_failures else "")
        )
    return sorted(relocations, key=lambda item: item[0])
