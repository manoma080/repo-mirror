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
from repo_mirror.forgejo_sync import SAFE_REASONS, parse_manifest
from repo_mirror.config_json import unique_pairs

DEFAULT_HELPER = Path.home() / '.hermes/workspace/github-app-manobot/github_app.py'
SOURCE = re.compile(r'https://github\.com/manoma080/mirror-[A-Za-z0-9_][A-Za-z0-9_.-]*')


def validate_sources(sources):
    parse_manifest(sources)


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
    args = parser.parse_args(argv)
    try:
        sources = json.loads(Path(args.manifest).read_text(encoding='utf-8'), object_pairs_hook=unique_pairs)
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
        secret_fd = os.open(args.timestamp_secret_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            secret_info = os.fstat(secret_fd)
            if not stat.S_ISREG(secret_info.st_mode) or secret_info.st_uid != os.getuid() or secret_info.st_mode & 0o077:
                raise ValueError('invalid recovery secret file')
            # Sync strips surrounding whitespace before using the UTF-8 secret.
            # Count the actual value, not a trailing newline from provisioning.
            if len(os.read(secret_fd, 4096).strip()) < 32:
                raise ValueError('recovery secret too short')
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
                snapshot.write_text(json.dumps(sources), encoding='utf-8')
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
                            diagnostics.seek(0)
                            for line in diagnostics:
                                prefix = b'FORGEJO_FAILURE: '
                                if line.startswith(prefix):
                                    candidate = line[len(prefix):].rstrip(b'\r\n').decode('utf-8', errors='replace')
                                    if candidate in SAFE_REASONS:
                                        reason = candidate
                                        break
                            diagnostics.close()
                            del active[process]
                            ok = status == 0
                            failures += not ok
                            print(f'Repository {index}: {"OK" if ok else "FAILED: " + reason}', flush=True)
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
