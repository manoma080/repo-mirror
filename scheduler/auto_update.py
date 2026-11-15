#!/usr/bin/env python3
"""Fetch remote main and run its scheduler and manifest from one private snapshot.

Install a copy of this launcher outside the checkout. All paths and the remote
are trusted administrator configuration; never put credentials in the URL.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit

SOURCE = re.compile(r'https://github\.com/manoma080/mirror-[A-Za-z0-9_][A-Za-z0-9_.-]*')
FORGEJO_SOURCE = re.compile(r'https://[A-Za-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9_][A-Za-z0-9_-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*')
DEFAULT_HELPER = Path.home() / '.hermes/workspace/github-app-manobot/github_app.py'


def valid_source(source):
    if not isinstance(source, str) or source.endswith('.git'):
        return False
    if SOURCE.fullmatch(source):
        return True
    if not FORGEJO_SOURCE.fullmatch(source):
        return False
    try:
        parsed = urlsplit(source)
        host, port = parsed.hostname, parsed.port
        return bool(host and host != 'github.com' and not host.startswith('github.com.') and port != 0 and all(
            re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
            for label in host.split('.')))
    except ValueError:
        return False


def checked_process(command, *, cwd, env, lock_fd, quiet=True, scheduler=False):
    """Forward termination, allowing the scheduler time to clean up its workers."""
    pending = []
    def forward(signum, _frame):
        pending.append(signum)
    previous = {s: signal.signal(s, forward) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        child = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL if quiet else None,
                                 stderr=subprocess.DEVNULL if quiet else None,
                                 pass_fds=(lock_fd,), start_new_session=True)
        deadline = None
        while True:
            if pending and deadline is None:
                signum = pending[0]
                deadline = time.monotonic() + 5
                try:
                    if scheduler:
                        # Scheduler owns separately-sessioned workers and must reap them.
                        child.send_signal(signal.SIGTERM)
                    else:
                        os.killpg(child.pid, signum)
                except ProcessLookupError:
                    pass
            status = child.poll()
            if status is not None:
                if deadline is not None and not scheduler:
                    # The helper may have exited while its fetch still runs.
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                break
            if deadline is not None and not scheduler and time.monotonic() >= deadline:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                status = child.wait()
                break
            time.sleep(.05)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if pending and status == 0:
        return 128 + pending[0]
    return status if status >= 0 else 128 - status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--remote', required=True, help='trusted repository URL or local path')
    parser.add_argument('--state-dir', required=True, help='private directory for temporary snapshots')
    parser.add_argument('--job-lock', required=True, help='distinct from scheduler --lock')
    parser.add_argument('--helper', default=str(DEFAULT_HELPER))
    parser.add_argument('--helper-python', default=sys.executable, help='Python with PyJWT installed for Manobot')
    parser.add_argument('--installation', default='178997310')
    parser.add_argument('scheduler_args', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    extra = args.scheduler_args
    if extra and extra[0] == '--':
        extra = extra[1:]
    try:
        if not re.fullmatch(r'[0-9]+', args.installation):
            raise ValueError('installation')
        if args.remote.startswith('-') or '\n' in args.remote or '\r' in args.remote:
            raise ValueError('remote')
        if '://' in args.remote:
            url = urlsplit(args.remote)
            if url.scheme != 'https' or not url.hostname or url.username or url.password:
                raise ValueError('remote')
        elif ':' in args.remote:
            raise ValueError('remote')
        # Validate before invoking the fetched scheduler: its argparse may accept
        # abbreviated options that override the fixed manifest or scheduler lock.
        valued = {'--token-file', '--timestamp-secret-file', '--journal-dir', '--owner',
                  '--port', '--jobs', '--repository-timeout', '--lock', '--helper',
                  '--helper-python', '--sync-python', '--installation', '--app-id',
                  '--private-key-file'}
        seen = {}
        i = 0
        while i < len(extra):
            name, separator, value = extra[i].partition('=')
            if name in seen or name not in valued | {'--organization'}:
                raise ValueError('invalid scheduler option')
            if name == '--organization':
                if separator:
                    raise ValueError('invalid scheduler flag')
                seen[name] = True
            else:
                if not separator:
                    i += 1
                    if i >= len(extra):
                        raise ValueError('missing scheduler option value')
                    value = extra[i]
                if not value or value.startswith('-'):
                    raise ValueError('invalid scheduler option value')
                seen[name] = value
            i += 1
        locks = [seen['--lock']] if '--lock' in seen else []
        if len(locks) != 1 or os.path.realpath(locks[0]) == os.path.realpath(args.job_lock):
            raise ValueError('distinct scheduler lock required')
        state = Path(args.state_dir)
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = state.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('state directory')
        fd = os.open(args.job_lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, 'r+') as lock:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('job lock')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('Updater skipped: lock held', file=sys.stderr)
                return 75
            if os.path.exists(locks[0]) and os.path.samestat(info, os.stat(locks[0])):
                raise ValueError('distinct scheduler lock required')
            scheduler_env = os.environ.copy()
            scheduler_env.pop('PYTHONPATH', None)
            scheduler_env.pop('PYTHONHOME', None)
            env = {key: value for key, value in scheduler_env.items() if not key.startswith('GIT_')}
            env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
            with tempfile.TemporaryDirectory(prefix='forgejo-update-', dir=state) as tmp:
                root = Path(tmp)
                def run(command, quiet=True):
                    return checked_process(command, cwd=root, env=env, lock_fd=fd, quiet=quiet)
                if run(['git', 'init', '--bare', str(root / 'objects')]):
                    raise ValueError('git init')
                fetch = [args.helper_python, args.helper, '--installation', args.installation, 'run', '--',
                         'git', '--git-dir', str(root / 'objects'), 'fetch', '--no-tags', '--no-recurse-submodules', '--', args.remote, 'refs/heads/main']
                if run(fetch):
                    raise ValueError('fetch failed')
                # Resolve only FETCH_HEAD from this successful, fresh fetch, never a cached ref.
                commit = subprocess.check_output(['git', '--git-dir', str(root / 'objects'), 'rev-parse', '--verify', 'FETCH_HEAD^{commit}'], cwd=root, env=env, stderr=subprocess.DEVNULL, text=True).strip()
                if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', commit):
                    raise ValueError('invalid commit')
                archive = root / 'snapshot.tar'
                with archive.open('wb') as output:
                    status = subprocess.run(['git', '--git-dir', str(root / 'objects'), 'archive', '--format=tar', commit], cwd=root, env=env, stdout=output, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, pass_fds=(fd,)).returncode
                if status:
                    raise ValueError('archive')
                snapshot = root / 'snapshot'
                snapshot.mkdir(mode=0o700)
                with tarfile.open(archive) as tar:
                    for member in tar:
                        path = PurePosixPath(member.name)
                        if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                            raise ValueError('unsafe snapshot')
                        tar.extract(member, snapshot, filter='data')
                manifest = snapshot / 'config-forgejo.json'
                sources = json.loads(manifest.read_text(encoding='utf-8'))
                if (not isinstance(sources, list) or not sources or
                    any(not valid_source(s) for s in sources) or
                    len({s.rsplit('/', 1)[-1].lower() for s in sources}) != len(sources)):
                    raise ValueError('invalid manifest')
                if not (snapshot / 'repo_mirror' / 'forgejo_schedule.py').is_file():
                    raise ValueError('scheduler missing')
                print(f'Updater fetched commit: {commit}', file=sys.stderr, flush=True)
                return checked_process([sys.executable, '-m', 'repo_mirror.forgejo_schedule', 'run', '--manifest', str(manifest), *extra], cwd=snapshot, env=scheduler_env, lock_fd=fd, quiet=False, scheduler=True)
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError):
        print('Updater failed: fetch, snapshot, configuration, or lock error', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
