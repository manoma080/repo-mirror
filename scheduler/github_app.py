#!/usr/bin/env python3
"""Generic GitHub App authentication helper; secrets stay in memory or child env."""

import argparse
import http.client
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

API = 'https://api.github.com'
USER_AGENT = 'repo-mirror-github-app'
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_KEY_BYTES = 65536
MAX_INSTALLATION_PAGES = 100
_OWNER_RE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]*')
_DIGITS_RE = re.compile(r'[0-9]+')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def read_private_key_bytes(path):
    """Read an owner-only regular PEM private key file without following symlinks."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        raise RuntimeError('Failed to read private key file') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or (info.st_mode & 0o077):
            raise RuntimeError(
                'Private key file must be an owner-only regular file owned by the current user'
            )
        if info.st_size <= 0 or info.st_size > MAX_KEY_BYTES:
            raise RuntimeError('Private key file size is invalid')
        with os.fdopen(fd, 'rb') as stream:
            fd = -1
            data = stream.read(MAX_KEY_BYTES + 1)
    except OSError:
        raise RuntimeError('Failed to read private key file') from None
    finally:
        if fd != -1:
            os.close(fd)
    if not data or len(data) > MAX_KEY_BYTES:
        raise RuntimeError('Private key file size is invalid')
    return data


def app_jwt(app_id, key_bytes):
    """Create a short-lived RS256 JWT for the GitHub App."""
    try:
        import jwt
    except ImportError:
        raise RuntimeError('PyJWT is required for GitHub App authentication') from None
    now = int(time.time())
    try:
        token = jwt.encode(
            {'iat': now - 60, 'exp': now + 540, 'iss': str(app_id)},
            key_bytes,
            algorithm='RS256',
        )
    except (jwt.PyJWTError, ValueError, TypeError):
        raise RuntimeError(
            'Failed to sign GitHub App JWT; check --app-id and --private-key-file'
        ) from None
    return token.decode('utf-8') if isinstance(token, bytes) else token


def api_request(path, token, method='GET'):
    """Perform a single non-redirecting GitHub REST API request."""
    if not isinstance(path, str) or not path.startswith('/') or any(c.isspace() or ord(c) < 32 for c in path):
        raise ValueError('Invalid GitHub API path')
    req = urllib.request.Request(
        API + path,
        method=method,
        headers={
            'Authorization': 'Bearer ' + token,
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': USER_AGENT,
        },
        data=b'{}' if method == 'POST' else None,
    )
    try:
        with _OPENER.open(req, timeout=30) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError(f'GitHub API {method} {path}: response exceeds size limit')
            return json.loads(raw.decode('utf-8'))
    except urllib.error.HTTPError as exc:
        # Never read or echo response bodies; they may contain sensitive details.
        raise RuntimeError(f'GitHub API {method} {path}: HTTP {exc.code}') from None
    except (urllib.error.URLError, http.client.HTTPException, OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError(f'GitHub API {method} {path}: request failed') from None


def list_installations(jwt_token, requester=api_request):
    """Fetch all installations visible to the authenticated GitHub App."""
    installations = []
    for page in range(1, MAX_INSTALLATION_PAGES + 1):
        batch = requester(f'/app/installations?per_page=100&page={page}', jwt_token)
        if not isinstance(batch, list):
            raise RuntimeError('GitHub API GET /app/installations: unexpected response')
        installations.extend(batch)
        if len(batch) < 100:
            return installations
    raise RuntimeError('GitHub API GET /app/installations: pagination limit exceeded')


def select_installations(installations, installation_id=None, owner=None):
    """Filter installations by optional installation ID and/or account login."""
    choices = list(installations)
    if installation_id is not None:
        choices = [inst for inst in choices if str(inst.get('id')) == installation_id]
    if owner is not None:
        target = owner.lower()
        choices = [
            inst
            for inst in choices
            if isinstance(inst.get('account'), dict)
            and isinstance(inst['account'].get('login'), str)
            and inst['account']['login'].lower() == target
        ]
    return choices


def _select_one_installation(installations, installation_id, owner):
    choices = select_installations(installations, installation_id, owner)
    if not choices:
        raise RuntimeError('No matching GitHub App installation found; use inspect to list installations')
    if len(choices) > 1:
        raise RuntimeError(
            'Select exactly one installation with --installation or --owner; use inspect to list installations'
        )
    chosen = choices[0]
    inst_id = chosen.get('id')
    if type(inst_id) is not int or inst_id <= 0:
        raise RuntimeError('Invalid GitHub App installation record')
    return chosen


def _mint_installation_token(requester, jwt_token, installation_id):
    result = requester(f'/app/installations/{installation_id}/access_tokens', jwt_token, 'POST')
    token = result.get('token') if isinstance(result, dict) else None
    if not isinstance(token, str) or not token or any(c.isspace() or ord(c) < 32 for c in token):
        raise RuntimeError('GitHub API returned an invalid installation token')
    return token


def _is_github_prompt(prompt):
    lowered = prompt.lower()
    if '://' in lowered:
        for match in re.finditer(r"https://([^\s'\"]+)", lowered):
            netloc = match.group(1).split('/', 1)[0].split('@')[-1]
            host, sep, port = netloc.partition(':')
            if host == 'github.com' and (not sep or port == '443'):
                return True
        return False
    return bool(re.fullmatch(r"(?:username|password)\s+for\s+'?(?:[^/@'\s]+@)?github\.com(?::443)?'?:?\s*", lowered))


def _handle_askpass(prompt):
    if not _is_github_prompt(prompt):
        print('Refusing credentials for non-GitHub host', file=sys.stderr)
        return 1
    lowered = prompt.lower()
    if lowered.startswith('username'):
        print('x-access-token')
        return 0
    if lowered.startswith('password'):
        token = os.environ.get('GITHUB_TOKEN', '')
        if not token:
            print('GITHUB_TOKEN is not set', file=sys.stderr)
            return 1
        print(token)
        return 0
    print('Unrecognized credential prompt', file=sys.stderr)
    return 1


def _run_with_askpass(command, installation_token):
    helper_path = Path(__file__).resolve()
    with tempfile.TemporaryDirectory(prefix='github-app-askpass-') as tmp_dir:
        os.chmod(tmp_dir, 0o700)
        askpass = Path(tmp_dir) / 'git-askpass.sh'
        askpass.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(helper_path))} --_askpass "$@"\n',
            encoding='utf-8',
        )
        askpass.chmod(0o700)
        env = os.environ.copy()
        env.update(
            {
                'GH_TOKEN': installation_token,
                'GITHUB_TOKEN': installation_token,
                'GIT_ASKPASS': str(askpass),
                'GIT_TERMINAL_PROMPT': '0',
            }
        )
        try:
            child = subprocess.Popen(command, env=env)
        except OSError:
            raise RuntimeError('Failed to execute command') from None
        previous = {}

        def _forward(signum, _frame):
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                previous[sig] = signal.signal(sig, _forward)
            except ValueError:
                pass
        try:
            rc = child.wait()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        return rc if rc >= 0 else 128 - rc


def _parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-id', help='GitHub App ID (or MIRROR_APP_ID)')
    parser.add_argument(
        '--private-key-file',
        help='path to owner-only PEM private key file (or MIRROR_APP_PRIVATE_KEY_FILE)',
    )
    parser.add_argument('--installation', help='installation ID (or MIRROR_APP_INSTALLATION)')
    parser.add_argument('--owner', help='installation account login (or MIRROR_APP_OWNER)')
    parser.add_argument('action', choices=['token', 'inspect', 'run'])
    parser.add_argument('command', nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def _validate_inputs(args):
    app_id = args.app_id if args.app_id is not None else os.environ.get('MIRROR_APP_ID')
    if not app_id:
        raise ValueError('GitHub App ID is required (--app-id or MIRROR_APP_ID)')
    if not _DIGITS_RE.fullmatch(app_id):
        raise ValueError('Invalid GitHub App ID')

    key_file = (
        args.private_key_file
        if args.private_key_file is not None
        else os.environ.get('MIRROR_APP_PRIVATE_KEY_FILE')
    )
    if not key_file:
        raise ValueError('GitHub App private key file is required (--private-key-file or MIRROR_APP_PRIVATE_KEY_FILE)')

    if args.installation is not None:
        installation_id = args.installation
        if not installation_id or not _DIGITS_RE.fullmatch(installation_id):
            raise ValueError('Invalid GitHub App installation ID')
    else:
        env_installation = os.environ.get('MIRROR_APP_INSTALLATION') or None
        if env_installation is not None and not _DIGITS_RE.fullmatch(env_installation):
            raise ValueError('Invalid GitHub App installation ID')
        installation_id = env_installation

    if args.owner is not None:
        owner = args.owner
        if not owner or not _OWNER_RE.fullmatch(owner) or owner in ('.', '..'):
            raise ValueError('Invalid GitHub App installation owner')
    elif installation_id is None:
        env_owner = os.environ.get('MIRROR_APP_OWNER') or None
        if env_owner is not None and (not _OWNER_RE.fullmatch(env_owner) or env_owner in ('.', '..')):
            raise ValueError('Invalid GitHub App installation owner')
        owner = env_owner
    else:
        owner = None

    command = args.command[1:] if (args.command and args.command[0] == '--') else list(args.command)
    if args.action == 'run':
        if not command:
            raise RuntimeError('Missing command')
    elif command:
        raise RuntimeError(f'Unexpected arguments for {args.action}')

    return app_id, key_file, installation_id, owner, command


def main(argv=None, *, requester=None, jwt_factory=None):
    """CLI entry point supporting inspect, token, and run actions."""
    raw_argv = list(sys.argv[1:]) if argv is None else list(argv)
    if raw_argv and raw_argv[0] == '--_askpass':
        return _handle_askpass(raw_argv[1] if len(raw_argv) > 1 else '')

    args = _parse_args(raw_argv)
    req_fn = requester or api_request
    jwt_fn = jwt_factory or app_jwt

    try:
        app_id, key_file, installation_id, owner, command = _validate_inputs(args)
        key_bytes = read_private_key_bytes(key_file)
        jwt_token = jwt_fn(app_id, key_bytes)

        if args.action == 'inspect':
            app = req_fn('/app', jwt_token)
            if not isinstance(app, dict):
                raise RuntimeError('GitHub API GET /app: unexpected response')
            installations = list_installations(jwt_token, req_fn)
            filtered = select_installations(installations, installation_id, owner)
            payload = {
                'app': {k: app.get(k) for k in ('id', 'slug', 'name', 'html_url')},
                'installations': [
                    {
                        'id': inst.get('id'),
                        'account': (inst.get('account') or {}).get('login')
                        if isinstance(inst.get('account'), dict)
                        else None,
                        'repository_selection': inst.get('repository_selection'),
                        'suspended_at': inst.get('suspended_at'),
                        'permissions': inst.get('permissions') or {},
                    }
                    for inst in filtered
                ],
            }
            print(json.dumps(payload, indent=2))
            return 0

        installations = list_installations(jwt_token, req_fn)
        chosen = _select_one_installation(installations, installation_id, owner)
        installation_token = _mint_installation_token(req_fn, jwt_token, chosen['id'])

        if args.action == 'token':
            print(installation_token)
            return 0

        return _run_with_askpass(command, installation_token)
    except (RuntimeError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
