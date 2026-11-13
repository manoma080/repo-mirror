"""Run a Forgejo mirror batch under one lock, refreshing Manobot per source."""
import argparse
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from repo_mirror.forgejo_sync import SAFE_REASONS

DEFAULT_HELPER = Path.home() / '.hermes/workspace/github-app-manobot/github_app.py'
SOURCE = re.compile(r'https://github\.com/manoma080/mirror-[A-Za-z0-9_][A-Za-z0-9_.-]*')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run'])
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--owner', required=True)
    parser.add_argument('--organization', action='store_true')
    parser.add_argument('--port', type=int, default=3300)
    parser.add_argument('--lock', required=True)
    parser.add_argument('--helper', default=str(DEFAULT_HELPER))
    parser.add_argument('--helper-python', default=sys.executable)
    parser.add_argument('--sync-python', default=sys.executable)
    parser.add_argument('--installation', default='178997310')
    parser.add_argument('--app-id')
    parser.add_argument('--private-key-file')
    args = parser.parse_args(argv)
    try:
        sources = json.loads(Path(args.manifest).read_text(encoding='utf-8'))
        if (not isinstance(sources, list) or not sources or
            any(not isinstance(s, str) or not SOURCE.fullmatch(s) or s.endswith('.git') for s in sources) or
            len({s.lower() for s in sources}) != len(sources)):
            raise ValueError('invalid source manifest')
        if not re.fullmatch(r'[0-9]+', args.installation) or not 1 <= args.port <= 65535:
            raise ValueError('invalid installation or port')
        if bool(args.app_id) != bool(args.private_key_file):
            raise ValueError('incomplete app credentials')
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
                for source in sources:
                    command = [args.sync_python, '-m', 'repo_mirror.forgejo_sync', '--config', str(snapshot),
                               '--token-file', args.token_file, '--owner', args.owner, '--port', str(args.port)]
                    if args.app_id:
                        command += ['--app-id', args.app_id, '--private-key-file', args.private_key_file]
                    else:
                        command += ['--use-github-token']
                        command = [args.helper_python, args.helper, '--installation', args.installation, 'run', '--'] + command
                    if args.organization:
                        command.append('--organization')
                    command += ['--source', source]
                    reason = 'helper or sync process failed; check helper configuration and credentials'
                    try:
                        with tempfile.TemporaryFile() as diagnostics:
                            result = subprocess.run(command, cwd=ROOT, stdout=subprocess.DEVNULL,
                                                    stderr=diagnostics, check=False, pass_fds=(lock.fileno(),))
                            diagnostics.seek(0)
                            for line in diagnostics:
                                prefix = b'FORGEJO_FAILURE: '
                                if line.startswith(prefix):
                                    candidate = line[len(prefix):].rstrip(b'\r\n').decode('utf-8', errors='replace')
                                    if candidate in SAFE_REASONS:
                                        reason = candidate
                                        break
                        ok = result.returncode == 0
                    except OSError:
                        ok = False
                        reason = 'unable to start helper; check executable and permissions'
                    failures += not ok
                    print(f'{source.rsplit("/", 1)[-1]}: {"OK" if ok else "FAILED: " + reason}', flush=True)
                print(f'Batch: {len(sources) - failures} succeeded, {failures} failed', flush=True)
                return 1 if failures else 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        print('Batch configuration or lock error', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
