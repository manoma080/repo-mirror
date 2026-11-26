"""Run a Forgejo mirror batch under one lock, refreshing Manobot per source."""
import argparse
import fcntl
import json
import math
import os
import re
import signal
import time
import stat
import subprocess
import sys
import tempfile
from collections import deque
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from repo_mirror import forgejo_migration
from repo_mirror.config_json import LEGACY_CONFIG_ERROR, is_legacy_config, unique_pairs
from repo_mirror.forgejo_progress import ProgressReader
from repo_mirror.forgejo_sync import (INACTIVE_FIELDS, INACTIVE_PREFIX, INSTALLATION_ID_REQUIRED,
                                      RELOCATION_FIELDS, RELOCATION_PREFIX, SAFE_REASONS,
                                      SOURCE_APP_OWNER_REQUIRED, parse_asset_max_bytes, parse_manifest,
                                      resolve_source_app_owner)
from repo_mirror.host_limits import CONFIG_KEY, HostAdmission, HostLimits, source_host, take_admissible
from repo_mirror.logging_utils import safe_label
from repo_mirror.migrations_store import FileMigrations, MigrationsStore

DEFAULT_HELPER = Path.home() / '.hermes/workspace/github-app-manobot/github_app.py'
SOURCE = re.compile(r'https://github\.com/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}')
MIGRATION_REASONS = frozenset({
    'invalid relocation report',
    'source relocation is not verified',
    'source pin mismatch',
    'destination missing',
    'destination identity mismatch',
    'destination collision',
    'destination rename failed',
    'configuration changed by another writer',
    'configuration publication failed',
    'configuration publication is not configured',
    'migration record missing',
    'migration state inconsistent',
})


def validate_sources(sources):
    parse_manifest(sources)


def parse_relocation(raw):
    """Accept only the exact relocation record the worker protocol defines."""
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (not isinstance(value, dict) or set(value) != RELOCATION_FIELDS
            or any(not isinstance(value[key], str) or not value[key] for key in ('url', 'canonical'))
            or any(type(value[key]) is not int or value[key] <= 0 for key in ('source_id', 'destination_id'))):
        return None
    return value


def parse_inactive(raw):
    """Accept only the exact inactive-source record the worker protocol defines."""
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (not isinstance(value, dict) or set(value) != INACTIVE_FIELDS
            or not isinstance(value['url'], str) or not value['url']
            or not isinstance(value['since'], str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value['since'])
            or type(value['idle_days']) is not int or value['idle_days'] < 0):
        return None
    return value


def inactive_message(record, source_urls):
    """Return the batch log line for a validated inactive record, or None."""
    if record is None or record['url'] not in source_urls:
        return None
    return f'INACTIVE since {record["since"]} ({record["idle_days"]} days)'


def refusal(exc):
    """Report only allowlisted migration failure text."""
    message = str(exc)
    return message if isinstance(exc, forgejo_migration.MigrationRefused) and message in MIGRATION_REASONS else 'unexpected migration failure'


def prepare_manual(args, secret, manifest_bytes, inputs):
    from repo_mirror.forgejo_dispatch_runtime import prepare
    return prepare(args, secret, manifest_bytes, inputs)


class _GitLabDiscoveryClient:
    """Origin-bound GitLab client supporting bounded pagination for onboarding."""

    def __init__(self, origin, token=None):
        from repo_mirror.gitlab_source_adapter import GitLabSourceAdapter
        self._adapter = GitLabSourceAdapter(origin, 'placeholder/project', 1, token=token)

    def request(self, method, url):
        if method != 'GET':
            raise ValueError('read-only GitLab API')
        return self._adapter._get(url)[0]

    def pages(self, url):
        result = []
        page = 1
        separator = '&' if '?' in url else '?'
        while page <= 10000:
            data, headers = self._adapter._get(f'{url}{separator}per_page=100&page={page}')
            if not isinstance(data, list):
                raise ValueError('pagination response is not an array')
            result.extend(data)
            following = headers.get('X-Next-Page', '')
            if not following:
                if len(data) < 100:
                    return result
                page += 1
                continue
            if not following.isascii() or not following.isdigit() or int(following) <= page:
                raise ValueError('invalid pagination')
            page = int(following)
        raise ValueError('pagination exceeded safety limit')


def resolve_installation_id(explicit=None):
    """Resolve and validate the GitHub App installation id for helper runs."""
    installation = explicit if explicit is not None else os.environ.get('MIRROR_APP_INSTALLATION')
    if installation is None or installation == '':
        raise ValueError(INSTALLATION_ID_REQUIRED)
    if not isinstance(installation, str) or not re.fullmatch(r'[0-9]+', installation):
        raise ValueError('invalid installation or port')
    return installation


def _build_auto_discovery_source_client(args):
    from repo_mirror.forgejo_sync import read_owner_secret, read_secure_file
    from repo_mirror.github_api import AppTokenProvider, SourceAPI

    clients = {}

    def source_client(rule):
        kind = rule['type']
        origin = rule['base_url'].rstrip('/')
        key = (kind, origin.lower())
        if key in clients:
            return clients[key]
        if kind == 'github':
            if args.app_id and args.private_key_file:
                private_key = read_secure_file(args.private_key_file)[0]
                app_owner = resolve_source_app_owner(getattr(args, 'source_app_owner', None))
                token = AppTokenProvider(args.app_id, private_key, app_owner)
            else:
                token = os.environ.get('GITHUB_TOKEN') or None
                if token and ('\n' in token or '\r' in token):
                    raise ValueError('invalid GITHUB_TOKEN')
            client = SourceAPI(token)
            client.restrict_origin('https://api.github.com')
        elif kind == 'gitlab':
            gitlab_token = (
                read_owner_secret(args.gitlab_token_file, 'GitLab token')
                if args.gitlab_token_file and origin.lower() == 'https://gitlab.com'
                else None
            )
            client = _GitLabDiscoveryClient(origin, token=gitlab_token)
        else:
            client = SourceAPI(None)
            client.restrict_origin(origin)
        clients[key] = client
        return client

    return source_client


def main(argv=None):
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run'])
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--timestamp-secret-file', required=True)
    parser.add_argument('--journal-dir', required=True)
    parser.add_argument('--owner', required=True)
    parser.add_argument('--organization', action='store_true')
    parser.add_argument('--port', type=int, default=3300)
    parser.add_argument('--asset-max-bytes', help='operator-selected per-asset byte limit for this Forgejo destination')
    parser.add_argument('--gitlab-token-file', help='optional owner-only gitlab.com read token file')
    parser.add_argument('--jobs', type=int, default=4, help='maximum concurrent syncs (default: 4)')
    parser.add_argument('--repository-timeout', type=float, default=3600, metavar='SECONDS',
                        help='maximum runtime per repository in seconds (default: 3600)')
    parser.add_argument('--lock', required=True)
    parser.add_argument('--helper', default=str(DEFAULT_HELPER))
    parser.add_argument('--helper-python', default=sys.executable)
    parser.add_argument('--sync-python', default=sys.executable)
    parser.add_argument('--installation')
    parser.add_argument('--app-id')
    parser.add_argument('--private-key-file')
    parser.add_argument('--source-app-owner')
    parser.add_argument('--config-repo', metavar='OWNER/NAME',
                        help='GitHub repository holding the authoritative config-forgejo.json')
    parser.add_argument('--config-account-type', default='personal', choices=['personal', 'organization'])
    parser.add_argument('--forgejo-config-repo', metavar='OWNER/NAME')
    parser.add_argument('--forgejo-config-id', type=int)
    args = parser.parse_args(argv)
    try:
        if (bool(args.forgejo_config_repo) != (args.forgejo_config_id is not None)
                or (args.forgejo_config_repo and (args.config_repo or args.forgejo_config_id <= 0
                    or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*', args.forgejo_config_repo)))):
            raise ValueError('invalid local configuration authority')
        has_authority = args.config_repo or args.forgejo_config_repo
        raw_dispatch = os.environ.pop('FORGEJO_MANUAL_INPUTS', None)
        inputs = None if raw_dispatch is None else json.loads(raw_dispatch, object_pairs_hook=unique_pairs)
        if raw_dispatch is not None and not isinstance(inputs, dict):
            raise ValueError('invalid manual dispatch')
        if args.asset_max_bytes is not None:
            parse_asset_max_bytes(args.asset_max_bytes)
        manifest_bytes = Path(args.manifest).read_bytes()
        sources = json.loads(manifest_bytes.decode('utf-8'), object_pairs_hook=unique_pairs)
        if is_legacy_config(sources):
            print(LEGACY_CONFIG_ERROR, file=sys.stderr)
            return 2
        groups = sources.get('groups') if isinstance(sources, dict) else None
        auto_discoveries = sources.get('auto_discoveries') if isinstance(sources, dict) else None
        if groups is not None:
            from repo_mirror.gitlab_onboarding_forgejo import translate_groups, validate_groups
            validate_groups(groups)
            if set(sources) - {CONFIG_KEY, 'auto_discoveries'} != {'destinations', 'repositories', 'groups'} or not has_authority or args.asset_max_bytes is None:
                raise ValueError('group onboarding requires authoritative config and asset limit')
            validate_sources(sources)
            worker_manifest = {k: v for k, v in sources.items() if k != 'groups'}
        else:
            worker_manifest = sources
            validate_sources(sources)
        normalized_auto = []
        if auto_discoveries:
            if not has_authority:
                raise ValueError('auto-discovery onboarding requires authoritative management configuration')
            from repo_mirror.config_named import _normalize_auto_discoveries
            normalized_auto = _normalize_auto_discoveries(auto_discoveries)
            if args.asset_max_bytes is None and any(
                    rule['type'] == 'gitlab' for rule in normalized_auto):
                raise ValueError('Forgejo asset byte limit must be configured')
        if isinstance(sources, dict):
            settings = next(iter(sources['destinations'].values()))
            if 'type' in settings:
                from repo_mirror.destination_profile import validate_runtime
                validate_runtime(settings, 'forgejo', f'http://127.0.0.1:{args.port}', args.owner, 'forgejo-home')
                if args.organization:
                    raise ValueError('destination account type mismatch')
            if 'account_type' in settings and (settings['account_type'] == 'organization') != args.organization:
                raise ValueError('destination account type mismatch')
        entries = parse_manifest(worker_manifest) if not groups or worker_manifest.get('repositories') else []
        if not groups and isinstance(worker_manifest, dict):
            # The generated namespace remains reserved even if its group was
            # removed entirely; retained pins alone do not authorize a sync.
            entries = [entry for entry in entries if not (
                entry.get('source_type') == 'gitlab' and
                not entry.get('discovered_by') and
                re.fullmatch(r'gitlab-[0-9a-f]{24}', entry.get('destination_name', '')))]
        if args.asset_max_bytes is None and any(
                e.get('source_type') == 'gitlab' or e['url'].startswith('https://gitlab.com/') for e in entries):
            raise ValueError('Forgejo asset byte limit must be configured')
        if not 1 <= args.port <= 65535 or not 1 <= args.jobs <= 64:
            raise ValueError('invalid installation or port')
        if not math.isfinite(args.repository_timeout) or args.repository_timeout <= 0:
            raise ValueError('invalid repository timeout')
        if bool(args.app_id) != bool(args.private_key_file):
            raise ValueError('incomplete app credentials')
        if args.source_app_owner is not None or args.app_id:
            resolve_source_app_owner(args.source_app_owner)
        needs_github_helper = not args.app_id and (
            any(SOURCE.fullmatch(e['url']) for e in entries)
            or any(rule['type'] == 'github' for rule in normalized_auto)
        )
        installation = None
        if args.installation is not None or needs_github_helper:
            installation = resolve_installation_id(args.installation)
        if args.config_repo and not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.config_repo):
            raise ValueError('invalid configuration repository')
        secret_fd = os.open(args.timestamp_secret_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            secret_info = os.fstat(secret_fd)
            if not stat.S_ISREG(secret_info.st_mode) or secret_info.st_uid != os.getuid() or secret_info.st_mode & 0o077:
                raise ValueError('invalid recovery secret file')
            # Sync strips surrounding whitespace before using the UTF-8 secret.
            # Count the actual value, not a trailing newline from provisioning.
            raw_secret = os.read(secret_fd, 4096)
            if len(raw_secret.strip()) < 32:
                raise ValueError('recovery secret too short')
            try:
                secret = raw_secret.strip().decode('utf-8')
            except UnicodeDecodeError:
                raise ValueError('invalid recovery secret file') from None
            if '\x00' in secret:
                raise ValueError('invalid recovery secret file')
            for other in (args.manifest, args.token_file, args.private_key_file):
                if other and os.path.exists(other):
                    info = os.stat(other)
                    if (info.st_dev, info.st_ino) == (secret_info.st_dev, secret_info.st_ino):
                        raise ValueError('recovery secret must be separate')
        finally:
            os.close(secret_fd)
        journal_info = os.stat(args.journal_dir, follow_symlinks=False)
        if not stat.S_ISDIR(journal_info.st_mode) or journal_info.st_uid != os.getuid() or journal_info.st_mode & 0o077:
            raise ValueError('invalid journal directory')
        fd = os.open(args.lock, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'r+') as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('lock must be an owner-only regular file')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('Batch skipped: lock held', file=sys.stderr)
                return 75
            with tempfile.TemporaryDirectory(prefix='forgejo-schedule-') as directory, ExitStack() as state:
                snapshot = Path(directory) / 'sources.json'
                # Preserve the validated bytes: remote configuration CAS compares
                # against the original Git blob, not reserialized JSON.
                snapshot.write_bytes(manifest_bytes)
                try:
                    transaction = None
                    if args.forgejo_config_repo:
                        from repo_mirror.forgejo_control_config import ForgejoFile
                        from repo_mirror.forgejo_failure_remote import session
                        transaction = forgejo_migration.build_migration(args, secret)
                        if transaction.cas.read() != manifest_bytes:
                            raise ValueError('configuration changed since fetch')
                        recorded = transaction.store.pending()
                        owner, repo = args.forgejo_config_repo.split('/')
                        failures_remote = ForgejoFile(transaction.cas.api, owner, repo, 'failures.json',
                                                      expected_id=args.forgejo_config_id)
                        state.enter_context(session(args.journal_dir, failures_remote))
                    else:
                        recorded = MigrationsStore(FileMigrations(os.path.join(args.journal_dir, 'migrations.json')),
                                                   secret).pending()
                except (OSError, ValueError, TypeError):
                    print('Batch configuration or lock error', file=sys.stderr)
                    return 2
                if groups:
                    translate_groups(sources, stream=sys.stdout)
                selected = None
                transient = False
                isolated = set()
                if inputs is not None:
                    if recorded:
                        print('Manual dispatch refused: pending migration; let the scheduled runner reconcile it first', flush=True)
                        return 1
                    try:
                        replacement, runtime, selected = prepare_manual(args, secret, manifest_bytes, inputs)
                    except (OSError, ValueError, TypeError, RuntimeError) as exc:
                        from repo_mirror.forgejo_dispatch_runtime import dispatch_failure
                        print(f'Manual dispatch refused: {dispatch_failure(exc)}', flush=True)
                        return 2
                    if selected == []:
                        print('No mirroring requested', flush=True)
                        return 0
                    if replacement is not None:
                        manifest_bytes = replacement
                    if selected is not None:
                        authoritative = json.loads(manifest_bytes, object_pairs_hook=unique_pairs)
                        transient = runtime != authoritative
                        profile = next(iter(runtime['destinations']))
                        saved_routes = {(r['source']['url'], r['mirrors'][profile].get('name', r['source']['url'].rsplit('/', 1)[-1]))
                                        for r in authoritative['repositories']}
                        isolated = {r['source']['url'] for r in runtime['repositories']
                                    if (r['source']['url'], r['mirrors'][profile].get('name', r['source']['url'].rsplit('/', 1)[-1])) not in saved_routes}
                        worker_manifest = {k: v for k, v in runtime.items() if k != 'groups'}
                        entries = [e for e in parse_manifest(worker_manifest) if e['url'] in selected]
                        if len(entries) != len(selected):
                            raise ValueError('manual selection changed')
                        snapshot.write_text(json.dumps(worker_manifest), encoding='utf-8')
                migration = None
                if transaction is not None:
                    migration = transaction
                elif has_authority:
                    # Build lazily: an ordinary batch must not require the
                    # configuration-publication credentials.
                    migration = forgejo_migration.DeferredMigration(
                        lambda: forgejo_migration.build_migration(args, secret))
                resumed: list = []
                published = None
                if recorded:
                    if migration is None:
                        print('Migration: FAILED: configuration publication is not configured', flush=True)
                        return 1
                    try:
                        resumed, published = migration.resume()
                    except (forgejo_migration.MigrationRefused, OSError, ValueError) as exc:
                        print(f'Migration: FAILED: {refusal(exc)}', flush=True)
                        return 1
                    except Exception:
                        print('Migration: FAILED: unexpected migration failure', flush=True)
                        return 1
                onboarding_result = None
                if (groups or auto_discoveries) and selected is None:
                    if recorded:
                        label = 'Auto-discovery onboarding' if auto_discoveries else 'Group onboarding'
                        print(f'{label}: FAILED: pending migration requires reconciliation', flush=True)
                        return 1
                    try:
                        from repo_mirror.adhoc import parse_repo_url
                        from repo_mirror.auto_discoveries_onboarding import (
                            PartialOnboarding,
                            conflict_identity_keys,
                            onboard_auto_discoveries_forgejo,
                        )
                        from repo_mirror.forgejo_git import LocalForgejoAPI
                        from repo_mirror.gitlab_onboarding_forgejo import group_origin_key
                        if transaction is None:
                            transaction = forgejo_migration.build_migration(args, secret)
                        if transaction is None:
                            raise ValueError('auto-discovery onboarding requires authoritative management configuration')
                        destination_api = getattr(getattr(transaction, 'destination', None), 'api', None)
                        if destination_api is None:
                            destination_api = LocalForgejoAPI(f'http://127.0.0.1:{args.port}', args.token_file)
                        source_client = _build_auto_discovery_source_client(args)
                        onboarding_result = onboard_auto_discoveries_forgejo(
                            manifest_bytes,
                            transaction.cas,
                            source_client,
                            destination_api,
                            owner=args.owner,
                            organization=args.organization,
                            stream=sys.stdout,
                            emit_group_warnings=False,
                        )
                        manifest_bytes = onboarding_result.config_bytes
                        expanded = json.loads(manifest_bytes.decode('utf-8'), object_pairs_hook=unique_pairs)
                        worker_manifest = {k: v for k, v in expanded.items() if k != 'groups'}
                        profile = next(iter(expanded['destinations']))
                        active_bindings = onboarding_result.active_group_bindings
                        conflicted = conflict_identity_keys(onboarding_result.conflicts)
                        def scheduled_auto(repo):
                            source = repo['source']
                            if conflicted:
                                kind, base, repo_owner, repo_name = parse_repo_url(
                                    source['url'],
                                    source_type=source.get('type'),
                                    allow_forgejo_port=True,
                                )
                                if (
                                    kind,
                                    group_origin_key(base),
                                    repo_owner.lower(),
                                    repo_name.lower(),
                                    source.get('id'),
                                ) in conflicted:
                                    return False
                            name = repo['mirrors'][profile].get('name', '')
                            if source.get('type') == 'gitlab' and re.fullmatch(r'gitlab-[0-9a-f]{24}', name):
                                return bool(repo.get('discovered_by')) or (source.get('url'), source.get('id'), name) in active_bindings
                            return True
                        worker_manifest['repositories'] = [r for r in worker_manifest['repositories'] if scheduled_auto(r)]
                        entries = parse_manifest(worker_manifest) if worker_manifest['repositories'] else []
                        if args.asset_max_bytes is None and any(
                                e.get('source_type') == 'gitlab' or e['url'].startswith('https://gitlab.com/') for e in entries):
                            raise ValueError('Forgejo asset byte limit must be configured')
                        if 'groups' in expanded:
                            snapshot.write_text(json.dumps(worker_manifest), encoding='utf-8')
                        else:
                            snapshot.write_bytes(manifest_bytes)
                    except PartialOnboarding as exc:
                        print('Auto-discovery onboarding: FAILED: ' + str(exc), flush=True)
                        return 1
                    except Exception:
                        print('Auto-discovery onboarding: FAILED: discovery or publication refused', flush=True)
                        return 1
                # A resumed intent already synced its repository; do not repeat
                # that sync against a configuration the transaction just changed.
                resumed_keys = {(entry['source']['origin'].lower(), entry['source']['id'])
                                for entry in resumed}
                document = manifest_bytes if published is None else published
                relocations = []
                failures = 0
                inactive = 0
                source_urls = {entry['url'] for entry in entries}
                host_limits = HostLimits.from_config(
                    sources.get(CONFIG_KEY) if isinstance(sources, dict) else None, default=args.jobs)
                queue = deque(enumerate((entry['url'] for entry in entries), 1))
                admission = HostAdmission(host_limits)
                active = {}
                interrupted = False
                def terminate(signum, frame):
                    nonlocal interrupted
                    interrupted = True
                previous_handlers = {sig: signal.signal(sig, terminate) for sig in (signal.SIGTERM, signal.SIGINT)}
                try:
                    while active or queue:
                        if interrupted:
                            raise KeyboardInterrupt
                        while len(active) < args.jobs and queue:
                            if interrupted:
                                raise KeyboardInterrupt
                            queued = take_admissible(queue, admission, lambda item: source_host(item[1]))
                            if queued is None:
                                break
                            index, source = queued
                            if isinstance(source, dict):
                                source = source['url']
                            entry_meta = entries[index - 1]
                            if (entry_meta.get('source_id') is not None
                                    and (entry_meta['url'].rsplit('/', 2)[0].lower(),
                                         entry_meta['source_id']) in resumed_keys):
                                print(f'Repository {index}: OK (migration resumed)', flush=True)
                                continue
                            command = [args.sync_python, '-m', 'repo_mirror.forgejo_sync', '--config', str(snapshot),
                                       '--token-file', args.token_file, '--owner', args.owner, '--port', str(args.port),
                                       '--timestamp-secret-file', args.timestamp_secret_file, '--journal-dir', args.journal_dir]
                            if args.asset_max_bytes is not None:
                                command += ['--asset-max-bytes', args.asset_max_bytes]
                            if args.gitlab_token_file is not None:
                                command += ['--gitlab-token-file', args.gitlab_token_file]
                            if SOURCE.fullmatch(source):
                                if args.app_id:
                                    command += ['--app-id', args.app_id, '--private-key-file', args.private_key_file]
                                    if args.source_app_owner is not None:
                                        command += ['--source-app-owner', args.source_app_owner]
                                else:
                                    if installation is None:
                                        installation = resolve_installation_id(args.installation)
                                    command += ['--use-github-token']
                                    command = [args.helper_python, args.helper, '--installation', installation, 'run', '--'] + command
                            if args.organization:
                                command.append('--organization')
                            if transient:
                                command.append('--no-dispatch-relocation')
                            if source in isolated:
                                command.append('--isolated-dispatch-journal')
                            command += ['--source', source]
                            diagnostics = tempfile.TemporaryFile()
                            host = source_host(source)
                            admission.acquire(host)
                            try:
                                process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.DEVNULL,
                                                           stderr=diagnostics, pass_fds=(lock.fileno(),), start_new_session=True)
                            except OSError:
                                admission.release(host)
                                diagnostics.close()
                                failures += 1
                                print(f'Repository {index}: FAILED: unable to start helper; check executable and permissions', flush=True)
                            else:
                                launch = time.monotonic()
                                active[process] = [index, diagnostics, launch + args.repository_timeout, None, host, ProgressReader(), launch]
                                label = '/'.join(safe_label(part) for part in urlsplit(source).path.strip('/').split('/'))
                                print(f'\n===== Repository {index}/{len(entries)}: {label} =====', flush=True)
                                print(f'repository {index}/{len(entries)} started', flush=True)
                        for process, (index, diagnostics, deadline, kill_deadline, host, progress, launch) in list(active.items()):
                            status = process.poll()
                            now = time.monotonic()
                            for event in progress.drain(diagnostics):
                                print(f'repository {index}/{len(entries)} {event}', flush=True)
                            if kill_deadline is not None:
                                if now < kill_deadline:
                                    continue
                                try:
                                    os.killpg(process.pid, signal.SIGKILL)
                                except ProcessLookupError:
                                    pass
                                process.wait()
                                diagnostics.close()
                                del active[process]
                                admission.release(host)
                                failures += 1
                                print(f'Repository {index}: FAILED: repository timed out', flush=True)
                                continue
                            if status is None and now >= deadline:
                                try:
                                    os.killpg(process.pid, signal.SIGTERM)
                                except ProcessLookupError:
                                    pass
                                active[process][3] = now + 2
                                continue
                            if status is None:
                                continue
                            # A helper may exit while descendants in its session still write.
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            for event in progress.drain(diagnostics, finished=True):
                                print(f'repository {index}/{len(entries)} {event}', flush=True)
                            reason = 'helper or sync process failed; check helper configuration and credentials'
                            relocation = None
                            invalid_report = False
                            reported = False
                            diagnostics.seek(0)
                            for line in diagnostics:
                                if line.startswith(RELOCATION_PREFIX.encode('ascii')):
                                    parsed = parse_relocation(line[len(RELOCATION_PREFIX):].rstrip(b'\r\n'))
                                    if parsed is None:
                                        invalid_report = True
                                    else:
                                        relocation = parsed
                                    continue
                                prefix = INACTIVE_PREFIX.encode('ascii')
                                if line.startswith(prefix):
                                    record = parse_inactive(line[len(prefix):].rstrip(b'\r\n'))
                                    # A report for an unconfigured URL is ignored: it is
                                    # informational and must never fail a good sync.
                                    message = inactive_message(record, source_urls)
                                    if message is not None:
                                        inactive += 1
                                        print(f'Repository {index}: {message}', flush=True)
                                    continue
                                prefix = b'FORGEJO_FAILURE: '
                                if line.startswith(prefix) and not reported:
                                    candidate = line[len(prefix):].rstrip(b'\r\n').decode('utf-8', errors='replace')
                                    if candidate in SAFE_REASONS:
                                        reason = candidate
                                        reported = True
                            diagnostics.close()
                            del active[process]
                            admission.release(host)
                            ok = status == 0 and not invalid_report
                            if invalid_report:
                                reason = 'invalid relocation report'
                            failures += not ok
                            outcome = 'completed' if ok else 'failed'
                            print(f'repository {index}/{len(entries)} {outcome} elapsed={time.monotonic() - launch:.3f}s', flush=True)
                            print(f'Repository {index}: {"OK" if ok else "FAILED: " + reason}', flush=True)
                            if ok and relocation is not None:
                                relocations.append((index, relocation))
                        if active:
                            time.sleep(0.05)
                finally:
                    for process in active:
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                    deadline = time.monotonic() + 2
                    for process, (_, diagnostics, _, _, _, _, _) in active.items():
                        try:
                            process.wait(timeout=max(0, deadline - time.monotonic()))
                        except subprocess.TimeoutExpired:
                            pass
                        finally:
                            # The group may still contain grandchildren after its leader exits.
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            process.wait()
                            diagnostics.close()
                    for sig, handler in previous_handlers.items():
                        signal.signal(sig, handler)
                if interrupted:
                    raise KeyboardInterrupt
                if relocations:
                    if migration is None:
                        for index, _ in sorted(relocations):
                            failures += 1
                            print(f'Migration {index}: FAILED: configuration publication is not configured', flush=True)
                    else:
                        for index, relocation in sorted(relocations):
                            if interrupted:
                                raise KeyboardInterrupt
                            try:
                                document = migration.apply(document, relocation)
                            except (forgejo_migration.MigrationRefused, OSError, ValueError) as exc:
                                failures += 1
                                print(f'Migration {index}: FAILED: {refusal(exc)}', flush=True)
                                break
                            except Exception:
                                failures += 1
                                print(f'Migration {index}: FAILED: unexpected migration failure', flush=True)
                                break
                            print(f'Migration {index}: OK', flush=True)
                summary = f'Batch: {len(entries) - failures} succeeded, {failures} failed'
                if inactive:
                    summary += f', {inactive} inactive'
                print('\n===== Summary =====', flush=True)
                print(summary, flush=True)
                if onboarding_result is not None:
                    from repo_mirror.auto_discoveries_onboarding import report_orphans
                    report_orphans(onboarding_result, stream=sys.stdout)
                print(f'runtime total elapsed={time.monotonic() - started:.3f}s', flush=True)
                return 1 if failures else 0
    except KeyboardInterrupt:
        print('Batch interrupted; active processes stopped', file=sys.stderr)
        return 130
    except (OSError, ValueError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
        if type(exc) is ValueError and str(exc) in (SOURCE_APP_OWNER_REQUIRED, INSTALLATION_ID_REQUIRED):
            print(str(exc), file=sys.stderr)
        else:
            print('Batch configuration or lock error', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
