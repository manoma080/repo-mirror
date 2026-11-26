"""Offline unit tests for the generic scheduler/github_app.py helper."""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / 'scheduler' / 'github_app.py'


def _load_helper():
    spec = importlib.util.spec_from_file_location('github_app_tested', HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_key(directory, name='app.pem', content=b'PRIVATE-KEY-SECRET-MATERIAL', mode=0o600):
    path = Path(directory) / name
    path.write_bytes(content)
    path.chmod(mode)
    return path


def _clean_env(**extra):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ('MIRROR_APP_ID', 'MIRROR_APP_PRIVATE_KEY_FILE', 'MIRROR_APP_INSTALLATION', 'MIRROR_APP_OWNER')
    }
    env.update(extra)
    return env


class GitHubAppHelperValidationTests(unittest.TestCase):
    def setUp(self):
        self.helper = _load_helper()

    def test_requires_app_id_and_private_key_file(self):
        with tempfile.TemporaryDirectory() as directory:
            key = _write_key(directory)
            with patch.dict(os.environ, _clean_env(), clear=True):
                for argv, expected in (
                    (['--private-key-file', str(key), 'token'], 'GitHub App ID is required'),
                    (['--app-id', 'abc', '--private-key-file', str(key), 'token'], 'Invalid GitHub App ID'),
                    (['--app-id', '123', 'token'], 'GitHub App private key file is required'),
                    (
                        ['--app-id', '123', '--private-key-file', str(key), '--installation', 'abc', 'token'],
                        'Invalid GitHub App installation ID',
                    ),
                    (
                        ['--app-id', '123', '--private-key-file', str(key), '--owner', '../bad', 'token'],
                        'Invalid GitHub App installation owner',
                    ),
                    (['--app-id', '123', '--private-key-file', str(key), 'run'], 'Missing command'),
                    (['--app-id', '123', '--private-key-file', str(key), 'run', '--'], 'Missing command'),
                    (
                        ['--app-id', '123', '--private-key-file', str(key), 'token', 'extra'],
                        'Unexpected arguments for token',
                    ),
                    (
                        ['--app-id', '123', '--private-key-file', str(key), 'inspect', 'extra'],
                        'Unexpected arguments for inspect',
                    ),
                ):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with (
                        self.subTest(argv=argv),
                        redirect_stdout(stdout),
                        redirect_stderr(stderr),
                    ):
                        rc = self.helper.main(argv, requester=lambda *a: {}, jwt_factory=lambda *a: 'jwt')
                    self.assertEqual(rc, 1)
                    self.assertIn(expected, stderr.getvalue())
                    self.assertEqual(stdout.getvalue(), '')

    def test_rejects_insecure_symlink_and_missing_key_files_without_leaking_contents(self):
        secret_key = b'SUPER-SECRET-PEM-BYTES-DO-NOT-PRINT'
        with tempfile.TemporaryDirectory() as directory:
            key = _write_key(directory, content=secret_key, mode=0o644)
            link = Path(directory) / 'link.pem'
            link.symlink_to(key)
            missing = Path(directory) / 'missing.pem'

            for target, msg in (
                (key, 'owner-only regular file'),
                (link, 'Failed to read private key file'),
                (missing, 'Failed to read private key file'),
            ):
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    self.subTest(target=target),
                    patch.dict(os.environ, _clean_env(), clear=True),
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    rc = self.helper.main(
                        ['--app-id', '123', '--private-key-file', str(target), 'token'],
                        requester=lambda *a: {},
                        jwt_factory=lambda *a: 'jwt',
                    )
                self.assertEqual(rc, 1)
                self.assertIn(msg, stderr.getvalue())
                self.assertNotIn(secret_key.decode(), stdout.getvalue() + stderr.getvalue())


class GitHubAppHelperActionsTests(unittest.TestCase):
    def setUp(self):
        self.helper = _load_helper()

    def _fake_api(self):
        calls = []

        def requester(path, token, method='GET'):
            calls.append((method, path, token))
            if path == '/app':
                return {'id': 42, 'slug': 'example-app', 'name': 'Example App', 'html_url': 'https://github.com/apps/example-app'}
            if path == '/app/installations?per_page=100&page=1':
                return [
                    {
                        'id': 101,
                        'account': {'login': 'Alice'},
                        'repository_selection': 'selected',
                        'suspended_at': None,
                        'permissions': {'contents': 'read'},
                    },
                    {
                        'id': 202,
                        'account': {'login': 'Bob'},
                        'repository_selection': 'all',
                        'suspended_at': None,
                        'permissions': {'contents': 'write', 'administration': 'write'},
                    },
                ]
            if path == '/app/installations/202/access_tokens' and method == 'POST':
                return {'token': 'ghs_installation_token_202', 'expires_at': '2026-11-26T16:00:00Z'}
            if path == '/app/installations/101/access_tokens' and method == 'POST':
                return {'token': 'ghs_installation_token_101', 'expires_at': '2026-11-26T16:00:00Z'}
            raise AssertionError(f'Unexpected API call: {method} {path}')

        return requester, calls

    def test_inspect_lists_app_and_installations_without_minting_token(self):
        requester, calls = self._fake_api()
        with tempfile.TemporaryDirectory() as directory:
            key = _write_key(directory)
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                rc = self.helper.main(
                    ['--app-id', '42', '--private-key-file', str(key), '--owner', 'bob', 'inspect'],
                    requester=requester,
                    jwt_factory=lambda app_id, kb: 'fake-jwt',
                )
            self.assertEqual(rc, 0, stderr.getvalue())
            data = json.loads(stdout.getvalue())
            self.assertEqual(data['app']['slug'], 'example-app')
            self.assertEqual(len(data['installations']), 1)
            self.assertEqual(data['installations'][0]['id'], 202)
            self.assertEqual(data['installations'][0]['account'], 'Bob')
            self.assertEqual(data['installations'][0]['permissions'], {'contents': 'write', 'administration': 'write'})
            self.assertFalse(any(method == 'POST' for method, _, _ in calls))

    def test_token_selection_by_installation_or_owner_and_env_fallbacks(self):
        requester, _ = self._fake_api()
        with tempfile.TemporaryDirectory() as directory:
            key = _write_key(directory)
            # Multiple installations without filter fails closed.
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                patch.dict(os.environ, _clean_env(MIRROR_APP_ID='42', MIRROR_APP_PRIVATE_KEY_FILE=str(key)), clear=True),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                rc = self.helper.main(['token'], requester=requester, jwt_factory=lambda *a: 'fake-jwt')
            self.assertEqual(rc, 1)
            self.assertIn('Select exactly one installation', stderr.getvalue())

            # Env owner selects matching installation (case-insensitive).
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    _clean_env(MIRROR_APP_ID='42', MIRROR_APP_PRIVATE_KEY_FILE=str(key), MIRROR_APP_OWNER='bob'),
                    clear=True,
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                rc = self.helper.main(['token'], requester=requester, jwt_factory=lambda *a: 'fake-jwt')
            self.assertEqual(rc, 0, stderr.getvalue())
            self.assertEqual(stdout.getvalue().strip(), 'ghs_installation_token_202')

            # Explicit --installation ignores unrelated MIRROR_APP_OWNER env var.
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    _clean_env(MIRROR_APP_ID='42', MIRROR_APP_PRIVATE_KEY_FILE=str(key), MIRROR_APP_OWNER='other'),
                    clear=True,
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                rc = self.helper.main(
                    ['--installation', '101', 'token'],
                    requester=requester,
                    jwt_factory=lambda *a: 'fake-jwt',
                )
            self.assertEqual(rc, 0, stderr.getvalue())
            self.assertEqual(stdout.getvalue().strip(), 'ghs_installation_token_101')

            # Conflicting explicit --installation and --owner fails closed.
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                patch.dict(os.environ, _clean_env(MIRROR_APP_ID='42', MIRROR_APP_PRIVATE_KEY_FILE=str(key)), clear=True),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                rc = self.helper.main(
                    ['--installation', '101', '--owner', 'Bob', 'token'],
                    requester=requester,
                    jwt_factory=lambda *a: 'fake-jwt',
                )
            self.assertEqual(rc, 1)
            self.assertIn('No matching GitHub App installation', stderr.getvalue())


class GitHubAppHelperRunAndSecurityTests(unittest.TestCase):
    def setUp(self):
        self.helper = _load_helper()

    def test_run_propagates_env_askpass_and_exit_status_and_cleans_up(self):
        def requester(path, token, method='GET'):
            if path.startswith('/app/installations?'):
                return [{'id': 55, 'account': {'login': 'owner'}, 'repository_selection': 'all', 'suspended_at': None}]
            if path == '/app/installations/55/access_tokens' and method == 'POST':
                return {'token': 'ghs_child_secret_token', 'expires_at': '2026-11-26T16:00:00Z'}
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as directory:
            key = _write_key(directory)
            probe_out = Path(directory) / 'probe.json'
            child_script = (
                'import json, os, pathlib, subprocess, sys\n'
                'askpass = os.environ["GIT_ASKPASS"]\n'
                'u = subprocess.check_output([askpass, "Username for \'https://github.com\': "], text=True).strip()\n'
                'p = subprocess.check_output([askpass, "Password for \'https://x-access-token@github.com\': "], text=True).strip()\n'
                'bad = subprocess.run([askpass, "Password for \'https://github.com.evil.example\': "], capture_output=True, text=True)\n'
                'bad2 = subprocess.run([askpass, "Password for \'https://evil.example/github.com\': "], capture_output=True, text=True)\n'
                'pathlib.Path(sys.argv[1]).write_text(json.dumps({\n'
                '  "gh_token": os.environ["GH_TOKEN"],\n'
                '  "github_token": os.environ["GITHUB_TOKEN"],\n'
                '  "prompt": os.environ["GIT_TERMINAL_PROMPT"],\n'
                '  "askpass": askpass,\n'
                '  "user": u,\n'
                '  "pass": p,\n'
                '  "bad_rc": bad.returncode,\n'
                '  "bad2_rc": bad2.returncode,\n'
                '}))\n'
                'sys.exit(int(sys.argv[2]))\n'
            )

            with patch.dict(
                os.environ,
                _clean_env(MIRROR_APP_ID='99', MIRROR_APP_PRIVATE_KEY_FILE=str(key)),
                clear=True,
            ):
                rc0 = self.helper.main(
                    ['--installation', '55', 'run', '--', sys.executable, '-c', child_script, str(probe_out), '0'],
                    requester=requester,
                    jwt_factory=lambda *a: 'fake-jwt',
                )
                rc42 = self.helper.main(
                    ['--installation', '55', 'run', '--', sys.executable, '-c', child_script, str(probe_out), '42'],
                    requester=requester,
                    jwt_factory=lambda *a: 'fake-jwt',
                )

            self.assertEqual(rc0, 0)
            self.assertEqual(rc42, 42)
            recorded = json.loads(probe_out.read_text())
            self.assertEqual(recorded['gh_token'], 'ghs_child_secret_token')
            self.assertEqual(recorded['github_token'], 'ghs_child_secret_token')
            self.assertEqual(recorded['prompt'], '0')
            self.assertEqual(recorded['user'], 'x-access-token')
            self.assertEqual(recorded['pass'], 'ghs_child_secret_token')
            self.assertNotEqual(recorded['bad_rc'], 0)
            self.assertNotEqual(recorded['bad2_rc'], 0)
            self.assertFalse(Path(recorded['askpass']).exists())
            self.assertFalse(Path(recorded['askpass']).parent.exists())

    def test_no_secret_echoing_on_http_or_jwt_or_subprocess_failure(self):
        secret_jwt = 'JWT_SECRET_SHOULD_NOT_LEAK'
        secret_body = b'{"error":"sensitive_body_token_XYZ"}'
        key_material = b'-----BEGIN RSA PRIVATE KEY-----\nSECRET_KEY_BODY\n-----END RSA PRIVATE KEY-----\n'

        with tempfile.TemporaryDirectory() as directory:
            key = _write_key(directory, content=key_material)

            # 1. HTTP error from urllib does not echo body or JWT
            http_err = urllib.error.HTTPError(
                'https://api.github.com/app/installations',
                403,
                'Forbidden',
                Message(),
                io.BytesIO(secret_body),
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                patch.object(self.helper._OPENER, 'open', side_effect=http_err),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                rc = self.helper.main(
                    ['--app-id', '7', '--private-key-file', str(key), 'inspect'],
                    jwt_factory=lambda *a: secret_jwt,
                )
            combined = stdout.getvalue() + stderr.getvalue()
            self.assertEqual(rc, 1)
            self.assertIn('HTTP 403', combined)
            self.assertNotIn(secret_jwt, combined)
            self.assertNotIn('sensitive_body_token_XYZ', combined)
            self.assertNotIn('SECRET_KEY_BODY', combined)

            # 2. JWT signing failure does not echo key bytes (if PyJWT installed)
            try:
                import jwt  # noqa: F401
            except ImportError:
                pass
            else:
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    patch.dict(os.environ, _clean_env(), clear=True),
                    redirect_stdout(stdout),
                    redirect_stderr(stderr),
                ):
                    rc = self.helper.main(['--app-id', '7', '--private-key-file', str(key), 'inspect'])
                combined = stdout.getvalue() + stderr.getvalue()
                self.assertEqual(rc, 1)
                self.assertIn('Failed to sign GitHub App JWT', combined)
                self.assertNotIn('SECRET_KEY_BODY', combined)

            # 3. Missing executable in run does not leak installation token
            def requester(path, token, method='GET'):
                if path.startswith('/app/installations?'):
                    return [{'id': 55, 'account': {'login': 'owner'}}]
                if path == '/app/installations/55/access_tokens':
                    return {'token': 'ghs_do_not_leak_token'}
                raise AssertionError(path)

            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                patch.dict(os.environ, _clean_env(), clear=True),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                rc = self.helper.main(
                    ['--app-id', '7', '--private-key-file', str(key), '--installation', '55', 'run', '--', '/nonexistent-cmd-12345'],
                    requester=requester,
                    jwt_factory=lambda *a: secret_jwt,
                )
            combined = stdout.getvalue() + stderr.getvalue()
            self.assertEqual(rc, 1)
            self.assertIn('Failed to execute command', combined)
            self.assertNotIn('ghs_do_not_leak_token', combined)
            self.assertNotIn(secret_jwt, combined)
            self.assertNotIn('SECRET_KEY_BODY', combined)

    def test_subprocess_cli_auto_update_invocation_shape(self):
        # Exercises the exact CLI invocation shape used by scheduler/auto_update.py:
        # helper --installation N run -- <command...>
        with tempfile.TemporaryDirectory() as directory:
            key = _write_key(directory)
            script = (
                'import json, os, sys\n'
                'from pathlib import Path\n'
                'sys.path.insert(0, sys.argv[1])\n'
                'import importlib.util\n'
                'spec = importlib.util.spec_from_file_location("gh", sys.argv[2])\n'
                'mod = importlib.util.module_from_spec(spec)\n'
                'spec.loader.exec_module(mod)\n'
                'def req(path, tok, method="GET"):\n'
                '    if path.startswith("/app/installations?"):\n'
                '        return [{"id": 777, "account": {"login": "owner"}}]\n'
                '    if path == "/app/installations/777/access_tokens":\n'
                '        return {"token": "ghs_subproc_ok"}\n'
                '    raise AssertionError(path)\n'
                'raise SystemExit(mod.main(sys.argv[3:], requester=req, jwt_factory=lambda *a: "j"))\n'
            )
            out_file = Path(directory) / 'env.txt'
            res = subprocess.run(
                [
                    sys.executable,
                    '-c',
                    script,
                    str(ROOT),
                    str(HELPER_PATH),
                    '--installation',
                    '777',
                    'run',
                    '--',
                    sys.executable,
                    '-c',
                    f'import os, pathlib; pathlib.Path({str(out_file)!r}).write_text(os.environ["GITHUB_TOKEN"])',
                ],
                env=_clean_env(MIRROR_APP_ID='42', MIRROR_APP_PRIVATE_KEY_FILE=str(key)),
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(out_file.read_text(), 'ghs_subproc_ok')


if __name__ == '__main__':
    unittest.main()
