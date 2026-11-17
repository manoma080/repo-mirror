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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from repo_mirror import forgejo_migration
from repo_mirror.config_json import unique_pairs
from repo_mirror.forgejo_sync import (RELOCATION_FIELDS, RELOCATION_PREFIX, SAFE_REASONS,
                                      parse_manifest)
from repo_mirror.migrations_store import FileMigrations, MigrationsStore

DEFAULT_HELPER = Path.home() / '.hermes/workspace/github-app-manobot/github_app.py'
SOURCE = re.compile(r'https://github\.com/manoma080/mirror-[A-Za-z0-9_][A-Za-z0-9_.-]*')
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


def refusal(exc):
    """Report only allowlisted migration failure text."""
    message = str(exc)
    return message if isinstance(exc, forgejo_migration.MigrationRefused) and message in MIGRATION_REASONS else 'unexpected migration failure'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run'])
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--timestamp-secret-file', required=True)
    parser.add_argument('--journal-dir', required=True)
    parser.add_argument('--owner', required=True)
    parser.add_argument('--organization', action='store_true')
    parser.add_argument('--port', type=int, default=3300)
    parser.add_argument('--jobs', type=int, default=4, help='maximum concurrent syncs (default: 4)')
    parser.add_argument('--repository-timeout', type=float, default=3600, metavar='SECONDS',
                        help='maximum runtime per repository in seconds (default: 3600)')
    parser.add_argument('--lock', required=True)
    parser.add_argument('--helper', default=str(DEFAULT_HELPER))
    parser.add_argument('--helper-python', default=sys.executable)
    parser.add_argument('--sync-python', default=sys.executable)
    parser.add_argument('--installation', default='178997310')
    parser.add_argument('--app-id')
    parser.add_argument('--private-key-file')
    parser.add_argument('--config-repo', metavar='OWNER/NAME',
                        help='GitHub repository holding the authoritative config-forgejo.json')
    parser.add_argument('--config-account-type', default='personal', choices=['personal', 'organization'])
    args = parser.parse_args(argv)
    try:
        manifest_bytes = Path(args.manifest).read_bytes()
        sources = json.loads(manifest_bytes.decode('utf-8'), object_pairs_hook=unique_pairs)
        validate_sources(sources)
        if isinstance(sources, dict):
            settings = next(iter(sources['destinations'].values()))
            if 'type' in settings:
                from repo_mirror.destination_profile import validate_runtime
                validate_runtime(settings, 'forgejo', f'http://127.0.0.1:{args.port}', args.owner, 'forgejo-home')
                if args.organization:
                    raise ValueError('destination account type mismatch')
            if 'account_type' in settings and (settings['account_type'] == 'organization') != args.organization:
                raise ValueError('destination account type mismatch')
        entries = parse_manifest(sources)
        if not re.fullmatch(r'[0-9]+', args.installation) or not 1 <= args.port <= 65535 or not 1 <= args.jobs <= 64:
            raise ValueError('invalid installation or port')
        if not math.isfinite(args.repository_timeout) or args.repository_timeout <= 0:
            raise ValueError('invalid repository timeout')
        if bool(args.app_id) != bool(args.private_key_file):
            raise ValueError('incomplete app credentials')
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
            with tempfile.TemporaryDirectory(prefix='forgejo-schedule-') as directory:
                snapshot = Path(directory) / 'sources.json'
                # Preserve the validated bytes: remote configuration CAS compares
                # against the original Git blob, not reserialized JSON.
                snapshot.write_bytes(manifest_bytes)
                try:
                    recorded = MigrationsStore(FileMigrations(os.path.join(args.journal_dir, 'migrations.json')),
                                               secret).pending()
                except (OSError, ValueError, TypeError):
                    print('Batch configuration or lock error', file=sys.stderr)
                    return 2
                migration = None
                if args.config_repo:
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
                # A resumed intent already synced its repository; do not repeat
                # that sync against a configuration the transaction just changed.
                resumed_keys = {(entry['source']['origin'].lower(), entry['source']['id'])
                                for entry in resumed}
                document = manifest_bytes if published is None else published
                relocations = []
                failures = 0
                pending = iter(enumerate((entry['url'] for entry in entries), 1))
                active = {}
                interrupted = False
                def terminate(signum, frame):
                    nonlocal interrupted
                    interrupted = True
                previous_handlers = {sig: signal.signal(sig, terminate) for sig in (signal.SIGTERM, signal.SIGINT)}
                try:
                    exhausted = False
                    while active or not exhausted:
                        if interrupted:
                            raise KeyboardInterrupt
                        while len(active) < args.jobs and not exhausted:
                            if interrupted:
                                raise KeyboardInterrupt
                            try:
                                index, source = next(pending)
                            except StopIteration:
                                exhausted = True
                                break
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
                            if SOURCE.fullmatch(source):
                                if args.app_id:
                                    command += ['--app-id', args.app_id, '--private-key-file', args.private_key_file]
                                else:
                                    command += ['--use-github-token']
                                    command = [args.helper_python, args.helper, '--installation', args.installation, 'run', '--'] + command
                            if args.organization:
                                command.append('--organization')
                            command += ['--source', source]
                            diagnostics = tempfile.TemporaryFile()
                            try:
                                process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.DEVNULL,
                                                           stderr=diagnostics, pass_fds=(lock.fileno(),), start_new_session=True)
                            except OSError:
                                diagnostics.close()
                                failures += 1
                                print(f'Repository {index}: FAILED: unable to start helper; check executable and permissions', flush=True)
                            else:
                                active[process] = [index, diagnostics, time.monotonic() + args.repository_timeout, None]
                        for process, (index, diagnostics, deadline, kill_deadline) in list(active.items()):
                            status = process.poll()
                            now = time.monotonic()
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
                                prefix = b'FORGEJO_FAILURE: '
                                if line.startswith(prefix) and not reported:
                                    candidate = line[len(prefix):].rstrip(b'\r\n').decode('utf-8', errors='replace')
                                    if candidate in SAFE_REASONS:
                                        reason = candidate
                                        reported = True
                            diagnostics.close()
                            del active[process]
                            ok = status == 0 and not invalid_report
                            if invalid_report:
                                reason = 'invalid relocation report'
                            failures += not ok
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
                    for process, (_, diagnostics, _, _) in active.items():
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
                print(f'Batch: {len(entries) - failures} succeeded, {failures} failed', flush=True)
                return 1 if failures else 0
    except KeyboardInterrupt:
        print('Batch interrupted; active processes stopped', file=sys.stderr)
        return 130
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        print('Batch configuration or lock error', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
