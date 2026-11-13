"""Provision private local Forgejo mirrors and copy Git branches and tags safely."""

import http.client
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


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
    if not isinstance(source, str) or source.count("/") != 1:
        raise ValueError("invalid source identity")
    owner, repo = source.split("/")
    _name(owner)
    _name(repo)
    if repo.endswith(".git"):
        raise ValueError("source identity must not have .git suffix")
    return owner, repo


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

    def request(self, method, path, payload=None):
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
                headers={"Authorization": "token " + self.token(), "Content-Type": "application/json"},
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

    def ensure_repository(self, name, source):
        name = _name(name)
        source_owner, source_name = _source(source)
        marker = f"repo-mirror-source: github.com/{source_owner}/{source_name}"
        path = f"/api/v1/repos/{self.owner}/{name}"
        try:
            repo = self.api.request("GET", path)
        except APIError as exc:
            if exc.status != 404:
                raise
            endpoint = f"/api/v1/orgs/{self.owner}/repos" if self.organization else "/api/v1/user/repos"
            self.api.request(
                "POST",
                endpoint,
                {
                    "name": name,
                    "private": True,
                    "auto_init": False,
                    "description": marker,
                    "has_actions": False,
                },
            )
            repo = self.api.request("GET", path)
        self._verify(repo, name, marker, actions=False)
        if repo.get("has_actions") is not False:
            self.api.request("PATCH", path, {"has_actions": False})
            repo = self.api.request("GET", path)
        self._verify(repo, name, marker, actions=True)
        return repo

    def _verify(self, repo, name, marker, *, actions):
        if (
            not isinstance(repo, dict)
            or repo.get("full_name", "").lower() != f"{self.owner}/{name}".lower()
            or repo.get("private") is not True
            or repo.get("description") != marker
            or (actions and repo.get("has_actions") is not False)
        ):
            raise RuntimeError("Forgejo repository identity, provenance, privacy or Actions not verified")


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


def sync_refs(destination, name, source, *, source_token=None):
    """Copy source heads/tags to a verified destination; never prune or rewrite history.

    Returns the number of refs changed. Uses atomic compare-and-swap pushes and
    reads back all requested refs; server must support atomic push.
    """
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
    if source_token is None:
        source_token = os.environ.get("GITHUB_TOKEN")
    def github_token():
        token = source_token() if callable(source_token) else source_token
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            raise RuntimeError("GITHUB_TOKEN required for source clone")
        return token
    destination.ensure_repository(name, source)
    local_token = destination.api.token()
    source_url = f"https://github.com/{owner}/{repo}.git"
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

        git(["clone", "--bare", "--", source_url, store], github_token())
        output = git(
            ["-C", store, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", "refs/tags"]
        ).stdout

        def parse(data):
            result = {}
            for line in data.decode("ascii").splitlines():
                oid, ref = line.split()
                if (
                    not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid)
                    or not ref.startswith(("refs/heads/", "refs/tags/"))
                    or ref in result
                ):
                    raise RuntimeError("invalid Git ref listing")
                result[ref] = oid
            return result

        desired = parse(output)

        def source_head():
            data = git(["ls-remote", "--symref", "--", source_url, "HEAD"], github_token()).stdout
            try:
                lines = data.decode("ascii").splitlines()
                refs = [line.removeprefix("ref: ").split("\t") for line in lines if line.startswith("ref: ")]
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
        if desired.get(head[0]) != head[1]:
            raise RuntimeError("source default branch changed during sync")

        def remote_refs():
            return parse(
                git(
                    ["ls-remote", "--refs", "--", destination_url, "refs/heads/*", "refs/tags/*"],
                    local_token,
                    "oauth2",
                ).stdout
            )

        observed = remote_refs()
        if any(
            ref.startswith("refs/heads/") and ref in desired and desired[ref] != oid
            for ref, oid in observed.items()
        ):
            git(
                [
                    "-C",
                    store,
                    "fetch",
                    "--no-tags",
                    "--",
                    destination_url,
                    "+refs/heads/*:refs/mirror-destination/*",
                ],
                local_token,
                "oauth2",
            )

        def ancestor(old, new):
            return (
                git(["-C", store, "merge-base", "--is-ancestor", old, new], allow_failure=True).returncode
                == 0
            )

        updates = plan_updates(desired, observed, ancestor)
        if updates:
            destination.ensure_repository(name, source)
            leases = [f"--force-with-lease={ref}:{old or ''}" for ref, _, old in updates]
            refspecs = [f"{oid}:{ref}" for ref, oid, _ in updates]
            git(
                ["-C", store, "push", "--atomic", *leases, "--", destination_url, *refspecs],
                local_token,
                "oauth2",
            )
            after = remote_refs()
            if any(after.get(ref) != oid for ref, oid, _ in updates):
                raise RuntimeError("Forgejo ref readback did not match requested update")
        if source_head() != head:
            raise RuntimeError("source default branch changed during sync")
        repo_info = destination.ensure_repository(name, source)
        path = f"/api/v1/repos/{destination.owner}/{name}"
        branch = head[0][len("refs/heads/"):]
        if repo_info.get("default_branch") != branch:
            # Recheck immediately before the non-conditional Forgejo API update.
            if source_head() != head:
                raise RuntimeError("source default branch changed during sync")
            destination.api.request("PATCH", path, {"default_branch": branch})
        verified = destination.api.request("GET", path)
        marker = f"repo-mirror-source: github.com/{owner}/{repo}"
        destination._verify(verified, name, marker, actions=True)
        if verified.get("default_branch") != branch:
            raise RuntimeError("Forgejo default branch readback did not match")
        if source_head() != head:
            raise RuntimeError("source default branch changed during sync")
        source_refs = parse(git(
            ["ls-remote", "--refs", "--", source_url, "refs/heads/*", "refs/tags/*"],
            github_token(),
        ).stdout)
        if source_refs != desired:
            raise RuntimeError("source refs changed during sync")
        final_refs = remote_refs()
        if any(final_refs.get(ref) != oid for ref, oid in desired.items()):
            raise RuntimeError("Forgejo ref readback did not match source snapshot")
        return len(updates)


def snapshot_refs(destination, name, source, *, source_token):
    owner, repo = _source(source)
    name = _name(name)
    _name(destination.owner)
    origin = urlsplit(destination.api.base_url)
    if (origin.scheme != 'http' or origin.hostname not in ('localhost', '127.0.0.1', '::1')
            or origin.username or origin.password or origin.path not in ('', '/')
            or origin.query or origin.fragment):
        raise ValueError('invalid Forgejo Git origin')
    source_url = f'https://github.com/{owner}/{repo}.git'
    dest_url = f'{destination.api.base_url.rstrip("/")}/{destination.owner}/{name}.git'
    with tempfile.TemporaryDirectory(prefix='repo-mirror-forgejo-') as tmp:
        askpass = Path(tmp) / 'askpass'
        askpass.write_text('#!/bin/sh\ncase "$1" in *Username*) printf "%s\\n" "$MIRROR_USER";; *) printf "%s\\n" "$MIRROR_TOKEN";; esac\n')
        askpass.chmod(0o700)
        base_env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': tmp,
                    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                    'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': str(askpass), 'LC_ALL': 'C'}

        def git(args, token, user):
            token = token() if callable(token) else token
            if not isinstance(token, str) or not token or '\n' in token or '\r' in token:
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
        if head() != source_head or source_refs.get(source_head[0]) != source_head[1]:
            raise RuntimeError('source default branch changed during sync')
        info = destination.api.request('GET', f'/api/v1/repos/{destination.owner}/{name}')
        destination._verify(info, name, f'repo-mirror-source: github.com/{owner}/{repo}', actions=True)
        if info.get('default_branch') != source_head[0][len('refs/heads/'):]:
            raise RuntimeError('Forgejo default branch readback did not match')
        if any(destination_refs.get(ref) != oid for ref, oid in source_refs.items()):
            raise RuntimeError('Forgejo ref readback did not match source snapshot')
        return source_head, source_refs, destination_refs
