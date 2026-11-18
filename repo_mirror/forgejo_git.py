"""Provision private local Forgejo mirrors and copy Git branches and tags safely."""

import http.client
import hashlib
from datetime import datetime, timezone
from functools import partial
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from repo_mirror.forgejo_local import forgejo_source_identity
from repo_mirror.git_sync import synchronize_snapshots
from repo_mirror.snapshot_push import _default_branch, _refs


class APIError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Forgejo API HTTP {status}")


def _name(value):
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value)
        or value in (".", "..")
    ):
        raise ValueError("invalid repository identity")
    return value


def _source(source):
    if isinstance(source, str) and source.startswith("https://"):
        from repo_mirror.adhoc import parse_repo_url
        _, base, owner, repo = parse_repo_url(source, source_type='gitlab', allow_forgejo_port=True)
        host = urlsplit(base).hostname
        if (not host or host.startswith('github.com.') or not all(
                re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                for label in host.split('.'))):
            raise ValueError('invalid source URL')
        if repo.endswith('.git'):
            raise ValueError("source identity must not have .git suffix")
        return owner, repo
    if not isinstance(source, str) or source.count("/") != 1:
        raise ValueError("invalid source identity")
    owner, repo = source.split("/")
    _name(owner)
    _name(repo)
    if repo.endswith(".git"):
        raise ValueError("source identity must not have .git suffix")
    return owner, repo


def _source_url(source):
    owner, repo = _source(source)
    if source.startswith("https://"):
        parsed = urlsplit(source)
        if '/' not in owner and parsed.hostname != 'gitlab.com':
            return forgejo_source_identity(source) + '.git'
        return source + ".git"
    return f"https://github.com/{owner}/{repo}.git"


def _source_marker(source):
    return "repo-mirror-source: " + _source_url(source)[len("https://"):-len(".git")]


class LocalForgejoAPI:
    """HTTP-only API client restricted to a literal loopback address or localhost."""

    def __init__(self, base_url, token_file):
        url = urlsplit(base_url)
        if (
            url.scheme != "http"
            or url.hostname not in ("localhost", "127.0.0.1", "::1")
            or url.username
            or url.password
            or url.path not in ("", "/")
            or url.query
            or url.fragment
        ):
            raise ValueError("Forgejo URL must be a local HTTP origin")
        self.host, self.port = ("127.0.0.1" if url.hostname == "localhost" else url.hostname), url.port or 80
        host = "[::1]" if self.host == "::1" else self.host
        self.base_url = f"http://{host}:{self.port}"
        self.token_file = Path(token_file)

    def token(self):
        fd = os.open(self.token_file, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Forgejo token file must be owner-only and owned by current user")
            with os.fdopen(fd, "r") as stream:
                fd = -1
                token = stream.read().strip()
            if not token or "\n" in token or "\r" in token:
                raise ValueError("invalid Forgejo token")
            return token
        finally:
            if fd != -1:
                os.close(fd)

    def request(self, method, path, payload=None, *, token=None):
        if (
            method not in ("GET", "POST", "PATCH")
            or not re.fullmatch(r"/api/v1/[A-Za-z0-9_./-]+", path)
            or ".." in path.split("/")
        ):
            raise ValueError("invalid Forgejo API request")
        body = json.dumps(payload).encode() if payload is not None else None
        conn = http.client.HTTPConnection(self.host, self.port, timeout=30)
        try:
            conn.request(
                method,
                path,
                body=body,
                headers={"Authorization": "token " + (self.token() if token is None else token), "Content-Type": "application/json"},
            )
            response = conn.getresponse()
            data = response.read()
            if not 200 <= response.status < 300:
                raise APIError(response.status)
            return json.loads(data) if data else None
        finally:
            conn.close()


class ForgejoDestination:
    def __init__(self, api, owner, *, organization=False):
        self.api = api
        self.owner = _name(owner)
        self.organization = organization
        self.run_pin = None

    def ensure_repository(self, name, source, *, expected_id=None):
        if expected_id is not None and (type(expected_id) is not int or expected_id <= 0):
            raise ValueError("invalid destination repository ID")
        name = _name(name)
        source_owner, source_name = _source(source)
        marker = _source_marker(source)
        path = f"/api/v1/repos/{self.owner}/{name}"
        try:
            repo = self.api.request("GET", path)
        except APIError as exc:
            if exc.status != 404:
                raise
            if expected_id is not None:
                raise RuntimeError("pinned Forgejo destination missing") from None
            request = self.api.request
            if not self.organization:
                if isinstance(self.api, LocalForgejoAPI):
                    request = partial(request, token=self.api.token())
                identity = request("GET", "/api/v1/user")
                login = identity.get("login") if isinstance(identity, dict) else None
                if not isinstance(login, str) or not login or login.lower() != self.owner.lower():
                    raise RuntimeError("authenticated Forgejo user does not match configured destination")
            endpoint = f"/api/v1/orgs/{self.owner}/repos" if self.organization else "/api/v1/user/repos"
            created = request(
                "POST",
                endpoint,
                {
                    "name": name,
                    "private": True,
                    "auto_init": False,
                    "description": marker + " [repo-mirror:initializing:v1]",
                    "has_actions": False,
                },
            )
            self._verify(created, name, marker, actions=False)
            created_id = created['id']
            repo = self.api.request("GET", path)
            if repo.get('id') != created_id:
                raise RuntimeError('Forgejo repository replaced during creation')
        self._verify(repo, name, marker, actions=False)
        if expected_id is not None and repo['id'] != expected_id:
            raise RuntimeError("Forgejo destination ID mismatch")
        expected_id = repo['id']
        expected_description = repo['description']
        if repo.get("has_actions") is not False:
            self.api.request("PATCH", path, {"has_actions": False})
            repo = self.api.request("GET", path)
        self._verify(repo, name, marker, actions=True)
        if repo['id'] != expected_id or repo['description'] != expected_description:
            raise RuntimeError('Forgejo repository replaced or initialization state changed')
        return repo

    def _verify(self, repo, name, marker, *, actions):
        if (
            not isinstance(repo, dict)
            or type(repo.get('id')) is not int or repo['id'] <= 0
            or repo.get("full_name", "").lower() != f"{self.owner}/{name}".lower()
            or repo.get("private") is not True
            or repo.get("description") not in (marker, marker + " [repo-mirror:initializing:v1]", marker + " [repo-mirror:initialized:v1]")
            or (actions and repo.get("has_actions") is not False)
        ):
            raise RuntimeError("Forgejo repository identity, provenance, privacy or Actions not verified")

    def _read_pinned(self, name, marker, expected_id, description):
        # Forgejo offers no conditional write tied to repository ID; replacement
        # between this GET and the following Git/API write remains non-atomic.
        repo = self.api.request("GET", f"/api/v1/repos/{self.owner}/{_name(name)}")
        self._verify(repo, name, marker, actions=True)
        if repo['id'] != expected_id or repo['description'] != description:
            raise RuntimeError("Forgejo repository identity or initialization state changed")
        return repo

    def begin_initialization(self, name, source, *, expected_id=None):
        """Conservatively migrate legacy mirrors through a full-history pass."""
        self.run_pin = None
        repo = (self.ensure_repository(name, source) if expected_id is None
                else self.ensure_repository(name, source, expected_id=expected_id))
        owner, source_name = _source(source)
        marker = _source_marker(source)
        if repo["description"] == marker:
            expected_id = repo['id']
            self._read_pinned(name, marker, expected_id, marker)
            self.api.request("PATCH", f"/api/v1/repos/{self.owner}/{_name(name)}",
                             {"description": marker + " [repo-mirror:initializing:v1]"})
            repo = self._read_pinned(name, marker, expected_id, marker + " [repo-mirror:initializing:v1]")
        self.run_pin = (repo['id'], repo['description'])
        return repo["description"] == marker + " [repo-mirror:initializing:v1]"

    def revalidate(self, name, source, expected):
        owner, source_name = _source(source)
        marker = _source_marker(source)
        return self._read_pinned(name, marker, *expected)

    def complete_initialization(self, name, source, *, expected=None):
        name = _name(name)
        owner, source_name = _source(source)
        marker = _source_marker(source)
        path = f"/api/v1/repos/{self.owner}/{name}"
        repo = (self.revalidate(name, source, expected) if expected is not None
                else self.api.request("GET", path))
        self._verify(repo, name, marker, actions=True)
        expected_id = repo['id']
        initializing = marker + " [repo-mirror:initializing:v1]"
        if repo["description"] != initializing:
            raise RuntimeError("Forgejo repository identity, provenance, privacy or Actions not verified")
        self._read_pinned(name, marker, expected_id, initializing)
        self.api.request("PATCH", path, {"description": marker + " [repo-mirror:initialized:v1]"})
        self._read_pinned(name, marker, expected_id, marker + " [repo-mirror:initialized:v1]")
        if expected is not None:
            self.run_pin = (expected_id, marker + " [repo-mirror:initialized:v1]")


def plan_updates(desired, observed, is_ancestor):
    """Plan additions and fast-forwards only; never delete or move tags."""
    updates = []
    for ref, oid in sorted(desired.items()):
        old = observed.get(ref)
        if old == oid:
            continue
        if old and (not ref.startswith("refs/heads/") or not is_ancestor(old, oid)):
            raise RuntimeError("destination ref diverged or tag moved; refusing update")
        updates.append((ref, oid, old))
    return updates


def sync_refs(destination, name, source, *, source_token=None, expected=None, recovery_identity=None):
    """Reconcile heads and tags using the shared archive-before-wipe policy."""
    provenance = recovery_identity if recovery_identity is not None else source
    owner, repo = _source(source)
    name = _name(name)
    _name(destination.owner)
    origin = urlsplit(destination.api.base_url)
    if (
        origin.scheme != "http"
        or origin.hostname not in ("localhost", "127.0.0.1", "::1")
        or origin.username
        or origin.password
        or origin.path not in ("", "/")
        or origin.query
        or origin.fragment
    ):
        raise ValueError("invalid Forgejo Git origin")
    public_forgejo = source.startswith("https://")
    if source_token is None and not public_forgejo:
        source_token = os.environ.get("GITHUB_TOKEN")
    def github_token():
        token = source_token() if callable(source_token) else source_token
        if public_forgejo and token is None:
            return ""
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            raise RuntimeError("GITHUB_TOKEN required for source clone")
        return token
    info = (destination.revalidate(name, provenance, expected) if expected is not None
            else destination.ensure_repository(name, provenance))
    pins = {}
    def pin(repo_name, info):
        repo_id = info.get('id') if isinstance(info, dict) else None
        if type(repo_id) is not int or repo_id <= 0:
            raise RuntimeError('Forgejo repository ID not verified')
        pins[repo_name] = (repo_id, info['description'])

    def revalidate(repo_name):
        marker_owner, marker_repo = _source(source)
        marker = _source_marker(provenance)
        current = destination.api.request('GET', f'/api/v1/repos/{destination.owner}/{repo_name}')
        destination._verify(current, repo_name, marker, actions=True)
        if (current.get('id'), current.get('description')) != pins[repo_name]:
            raise RuntimeError('Forgejo repository identity or initialization state changed')
        return current

    pin(name, info)
    source_url = _source_url(source)
    destination_url = f"{destination.api.base_url.rstrip('/')}/{destination.owner}/{name}.git"
    with tempfile.TemporaryDirectory(prefix="repo-mirror-forgejo-") as tmp:
        askpass = Path(tmp) / "askpass"
        askpass.write_text(
            '#!/bin/sh\ncase "$1" in *Username*) printf "%s\n" "$MIRROR_USER";; *) printf "%s\n" "$MIRROR_TOKEN";; esac\n'
        )
        askpass.chmod(0o700)
        store = str(Path(tmp) / "source.git")
        base_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": tmp,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": str(askpass),
            "LC_ALL": "C",
        }

        def git(args, token="", user="x-access-token", allow_failure=False):
            env = dict(base_env, MIRROR_TOKEN=token, MIRROR_USER=user)
            command = [
                "git",
                "-c",
                "core.hooksPath=" + os.devnull,
                "-c",
                "credential.helper=",
                "-c",
                "http.followRedirects=false",
                "-c",
                "protocol.file.allow=never",
                *args,
            ]
            result = subprocess.run(command, env=env, capture_output=True, check=False)
            if result.returncode and not allow_failure:
                raise RuntimeError("Git operation failed; details suppressed to protect credentials")
            return result

        source_snapshot = None

        def source_run(args):
            nonlocal source_snapshot
            output = git(args, github_token()).stdout
            if source_snapshot is None and 'ls-remote' in args and '--refs' in args and source_url in args:
                source_snapshot = _refs(lambda unused: output, source_url)
            if ("rev-parse" in args and head and args[-1].endswith("/" + head[0])
                    and output.strip().decode("ascii") != head[1]):
                raise RuntimeError("source default branch changed during snapshot")
            return output

        archive_targets = {}

        def dest_run(args):
            if "push" in args:
                if source_head() != head:
                    raise RuntimeError("source default branch changed during sync")
                if source_snapshot is None or _refs(source_run, source_url) != source_snapshot:
                    raise RuntimeError('source refs changed during sync')
                revalidate(name)
                for url, archive_name in archive_targets.items():
                    if url in args:
                        revalidate(archive_name)
            return git(args, destination.api.token(), "oauth2").stdout

        git(["init", "--bare", store])

        def source_head():
            data = source_run(["ls-remote", "--symref", "--", source_url, "HEAD"])
            if not data:
                return None
            try:
                lines = data.decode("ascii").splitlines()
                refs = [line[5:].split("\t") for line in lines if line.startswith("ref: ")]
                oids = [line.split("\t") for line in lines if not line.startswith("ref: ")]
                if (len(refs) != 1 or len(oids) != 1 or len(refs[0]) != 2 or len(oids[0]) != 2
                        or refs[0][1] != "HEAD" or oids[0][1] != "HEAD"
                        or not refs[0][0].startswith("refs/heads/")
                        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oids[0][0])):
                    raise ValueError
                return refs[0][0], oids[0][0]
            except (ValueError, UnicodeDecodeError):
                raise RuntimeError("source default branch not verified") from None

        head = source_head()
        info = revalidate(name)

        def set_default(repo_name, ref, *, initial_refs=None):
            if not isinstance(ref, str) or not ref.startswith("refs/heads/") or not ref[11:]:
                raise RuntimeError("invalid default branch")
            if source_head() != head:
                raise RuntimeError("source default branch changed during sync")
            repo_path = f"/api/v1/repos/{destination.owner}/{repo_name}"
            if source_snapshot is None or _refs(source_run, source_url) != source_snapshot:
                raise RuntimeError('source refs changed during sync')
            revalidate(name)
            current = revalidate(repo_name)
            branch = ref[11:]
            if current.get("default_branch") != branch:
                destination.api.request("PATCH", repo_path, {"default_branch": branch})
            verified = revalidate(repo_name)
            if verified.get("default_branch") != branch:
                raise RuntimeError("Forgejo default branch readback did not match")
            url = f"{destination.api.base_url.rstrip('/')}/{destination.owner}/{repo_name}.git"
            if _default_branch(dest_run, url) != ref:
                raise RuntimeError("Forgejo Git HEAD did not match default branch")

        def archive_factory():
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            suffix = "-wipe-" + timestamp
            if len(name) + len(suffix) > 100:
                suffix = "-" + hashlib.sha256(name.lower().encode()).hexdigest()[:16] + suffix
            archive_name = name[:100 - len(suffix)] + suffix
            revalidate(name)
            if source_snapshot is None or _refs(source_run, source_url) != source_snapshot:
                raise RuntimeError('source refs changed during sync')
            pin(archive_name, destination.ensure_repository(archive_name, provenance))
            archive_url = f"{destination.api.base_url.rstrip('/')}/{destination.owner}/{archive_name}.git"
            archive_targets[archive_url] = archive_name
            return archive_url, lambda ref: set_default(archive_name, ref)

        result = synchronize_snapshots(
            source_run, dest_run, store, source_url, destination_url,
            head[0][11:] if head else None, info.get("default_branch"), archive_factory,
            lambda ref, **kwargs: set_default(name, ref, **kwargs),
        )
        if not result:
            if head is not None:
                raise RuntimeError("source refs changed during sync")
            revalidate(name)
            if source_head() is not None or _refs(source_run, source_url) != source_snapshot:
                raise RuntimeError("source refs changed during sync")
            if _refs(dest_run, destination_url):
                raise RuntimeError("Forgejo ref readback did not match source snapshot")
            revalidate(name)
            return 0
        if source_head() != head:
            raise RuntimeError("source default branch changed during sync")
        revalidate(name)
        source_refs = _refs(source_run, source_url)
        if (source_refs != source_snapshot or source_refs.get(head[0]) != head[1]
                or _refs(dest_run, destination_url) != source_refs):
            raise RuntimeError("Forgejo ref readback did not match source snapshot")
        revalidate(name)
        return result



def snapshot_refs(destination, name, source, *, source_token, expected=None, recovery_identity=None):
    provenance = recovery_identity if recovery_identity is not None else source
    owner, repo = _source(source)
    name = _name(name)
    _name(destination.owner)
    origin = urlsplit(destination.api.base_url)
    if (origin.scheme != 'http' or origin.hostname not in ('localhost', '127.0.0.1', '::1')
            or origin.username or origin.password or origin.path not in ('', '/')
            or origin.query or origin.fragment):
        raise ValueError('invalid Forgejo Git origin')
    source_url = _source_url(source)
    dest_url = f'{destination.api.base_url.rstrip("/")}/{destination.owner}/{name}.git'
    if expected is not None:
        destination.revalidate(name, provenance, expected)
    with tempfile.TemporaryDirectory(prefix='repo-mirror-forgejo-') as tmp:
        askpass = Path(tmp) / 'askpass'
        askpass.write_text('#!/bin/sh\ncase "$1" in *Username*) printf "%s\\n" "$MIRROR_USER";; *) printf "%s\\n" "$MIRROR_TOKEN";; esac\n')
        askpass.chmod(0o700)
        base_env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': tmp,
                    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                    'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': str(askpass), 'LC_ALL': 'C'}

        def git(args, token, user):
            token = token() if callable(token) else token
            if token is None and source.startswith("https://"):
                token = ""
            if not isinstance(token, str) or (not token and not source.startswith("https://")) or '\n' in token or '\r' in token:
                raise RuntimeError('GITHUB_TOKEN required for source clone')
            env = dict(base_env, MIRROR_TOKEN=token, MIRROR_USER=user)
            result = subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull,
                '-c', 'credential.helper=', '-c', 'http.followRedirects=false',
                '-c', 'protocol.file.allow=never', *args], env=env, capture_output=True, check=False)
            if result.returncode:
                raise RuntimeError('Git operation failed; details suppressed to protect credentials')
            return result.stdout

        def head():
            try:
                lines = git(['ls-remote', '--symref', '--', source_url, 'HEAD'], source_token, 'x-access-token').decode('ascii').splitlines()
                if not lines:
                    return None
                refs = [line.removeprefix('ref: ').split('\t') for line in lines if line.startswith('ref: ')]
                oids = [line.split('\t') for line in lines if not line.startswith('ref: ')]
                if (len(refs) != 1 or len(oids) != 1 or len(refs[0]) != 2 or len(oids[0]) != 2
                        or refs[0][1] != 'HEAD' or oids[0][1] != 'HEAD'
                        or not refs[0][0].startswith('refs/heads/')
                        or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', oids[0][0])):
                    raise ValueError
                return refs[0][0], oids[0][0]
            except (ValueError, UnicodeDecodeError):
                raise RuntimeError('source default branch not verified') from None

        def listing(url, token, user):
            data = git(['ls-remote', '--refs', '--', url, 'refs/heads/*', 'refs/tags/*'], token, user)
            result = {}
            try:
                for line in data.decode('ascii').splitlines():
                    oid, ref = line.split()
                    if (not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', oid)
                            or not ref.startswith(('refs/heads/', 'refs/tags/')) or ref in result):
                        raise ValueError
                    result[ref] = oid
            except (ValueError, UnicodeDecodeError):
                raise RuntimeError('invalid Git ref listing') from None
            return result

        source_head = head()
        source_refs = listing(source_url, source_token, 'x-access-token')
        destination_refs = listing(dest_url, destination.api.token(), 'oauth2')
        if head() != source_head:
            raise RuntimeError('source default branch changed during sync')
        if listing(source_url, source_token, 'x-access-token') != source_refs:
            raise RuntimeError('source refs changed during sync')
        if ((source_head is None and source_refs) or
                (source_head is not None and source_refs.get(source_head[0]) != source_head[1])):
            raise RuntimeError('source default branch not verified')
        info = (destination.revalidate(name, provenance, expected) if expected is not None
                else destination.api.request('GET', f'/api/v1/repos/{destination.owner}/{name}'))
        destination._verify(info, name, _source_marker(provenance), actions=True)
        if source_head is not None and info.get('default_branch') != source_head[0][len('refs/heads/'):]:
            raise RuntimeError('Forgejo default branch readback did not match')
        if destination_refs != source_refs:
            raise RuntimeError('Forgejo ref readback did not match source snapshot')
        return source_head, source_refs, destination_refs
