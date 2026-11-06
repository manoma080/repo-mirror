import unittest
from unittest.mock import patch
import mirror


class TokenRefreshTests(unittest.TestCase):
    def test_installation_provider_refreshes_before_expiry(self):
        provider = mirror.AppTokenProvider('123', 'private', 'owner')
        with patch.object(provider, '_mint', side_effect=[('one', 5000), ('two', 9000)]) as mint, patch('mirror.time.time', return_value=1000):
            self.assertEqual(provider(), 'one')
            self.assertEqual(provider(), 'one')
            with patch('mirror.time.time', return_value=4800):
                self.assertEqual(provider(), 'two')
            self.assertEqual(mint.call_count, 2)

    def test_mint_signs_jwt_and_targets_explicit_installation(self):
        import base64, io, json, subprocess
        key = subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout
        public = subprocess.run(['openssl', 'pkey', '-pubout'], input=key, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout
        calls = []
        class Opener:
            def open(self, request, timeout):
                calls.append(request)
                response = {'id': 123, 'account': {'login': 'dest'}} if len(calls) == 1 else {'token': 'fake-installation-token', 'expires_at': '2099-01-01T00:00:00Z'}
                return io.BytesIO(json.dumps(response).encode())
        with patch('mirror.build_opener', return_value=Opener()):
            token, expires = mirror.AppTokenProvider('456', key.decode(), 'dest')._mint()
        self.assertEqual(token, 'fake-installation-token')
        self.assertEqual([r.full_url for r in calls], ['https://api.github.com/users/dest/installation', 'https://api.github.com/app/installations/123/access_tokens'])
        jwt = calls[0].get_header('Authorization').split()[1]
        header, payload, signature = jwt.split('.')
        claims = json.loads(base64.urlsafe_b64decode(payload + '==='))
        self.assertEqual(claims['iss'], '456')
        self.assertLessEqual(claims['exp'] - claims['iat'], 600)
        # Verification uses only a public-key temporary file; the private key stays in memory.
        import tempfile
        with tempfile.NamedTemporaryFile() as f, tempfile.NamedTemporaryFile() as sig:
            f.write(public); f.flush()
            sig.write(base64.urlsafe_b64decode(signature + '===')); sig.flush()
            verified = subprocess.run(['openssl', 'dgst', '-sha256', '-verify', f.name, '-signature', sig.name], input=(header + '.' + payload).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(verified.returncode, 0)
        self.assertNotEqual(expires, 0)

    def test_workflow_passes_app_credentials_and_always_saves_journal(self):
        from pathlib import Path
        text = Path('.github/workflows/mirror.yml').read_text()
        self.assertIn('MIRROR_APP_ID: ${{ secrets.MIRROR_APP_ID }}', text)
        self.assertIn('MIRROR_APP_PRIVATE_KEY: ${{ secrets.MIRROR_APP_PRIVATE_KEY }}', text)
        self.assertNotIn('actions/create-github-app-token@', text)
        self.assertIn('if: ${{ always() }}', text)

    def test_log_redaction_accepts_refreshable_provider(self):
        provider = mirror.AppTokenProvider('123', 'private', 'dest')
        provider.token = 'abcdefghij'
        context = mirror.LOG_SECRETS.set((provider,))
        try:
            self.assertEqual(mirror.safe_label('abcdefghij'), '[redacted]')
        finally:
            mirror.LOG_SECRETS.reset(context)

    def test_main_uses_app_provider_without_static_token(self):
        import json, os, tempfile
        sources = [{'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice', 'repos': ['demo']}]
        with tempfile.NamedTemporaryFile(mode='w') as f:
            json.dump({'destination_account_type': 'personal', 'sources': sources}, f); f.flush()
            env = {'DESTINATION_OWNER': 'dest', 'MIRROR_APP_ID': '123', 'MIRROR_APP_PRIVATE_KEY': 'private'}
            with patch.dict(os.environ, env, clear=True), patch.object(mirror, 'discover', return_value=['demo']), patch.object(mirror, 'get_existing', return_value=None), patch.object(mirror, 'mirror_repository') as run:
                mirror.main(['--config', f.name])
                self.assertIsInstance(run.call_args.args[5], mirror.AppTokenProvider)
                self.assertNotIn('MIRROR_APP_PRIVATE_KEY', os.environ)

    def test_git_refreshes_after_fetch_and_between_pushes(self):
        import subprocess
        commands = []
        def run(args, **kwargs):
            commands.append((args, kwargs['env']))
            return subprocess.CompletedProcess(args, 0, stdout=b'refs/heads/main\n', stderr=b'')
        tokens = iter(['one', 'two'])
        with patch('mirror.subprocess.run', side_effect=run):
            mirror.mirror_git('https://example.com/a', 'https://github.com/a/b.git', lambda: next(tokens))
        pushes = [env for args, env in commands if 'push' in args]
        import base64
        self.assertEqual(len(pushes), 2)
        for env, token in zip(pushes, ('one', 'two')):
            self.assertIn(base64.b64encode(('x-access-token:' + token).encode()).decode(), env['GIT_CONFIG_VALUE_0'])
        self.assertNotIn('GIT_CONFIG_VALUE_0', commands[1][1])

    def test_github_source_resolves_refreshed_token(self):
        api = mirror.SourceAPI(lambda: 'fresh')
        import io
        seen = []
        def open_request(request, timeout):
            seen.append(request.get_header('Authorization'))
            return io.BytesIO(b'{}')
        api.opener.open = open_request
        api.request('GET', 'https://api.github.com/repos/a/b')
        self.assertEqual(seen, ['Bearer fresh'])

    def test_api_resolves_token_again_for_each_request(self):
        tokens = iter(['first', 'second'])
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{}'
        api = mirror.API(lambda: next(tokens))
        seen = []
        def open_request(request, timeout):
            seen.append(request.get_header('Authorization'))
            return Response()
        api.opener.open = open_request
        api.request('GET', 'https://api.github.com/repos/a/b')
        api.request('GET', 'https://api.github.com/repos/a/b')
        self.assertEqual(seen, ['Bearer first', 'Bearer second'])


    def test_credentials_refresh_on_each_api_retry_for_both_roles(self):
        import io
        from urllib.error import URLError
        for make_api in (mirror.API, mirror.SourceAPI):
            seen = []
            tokens = iter(['first', 'second'])
            api = make_api(lambda: next(tokens))
            def open_request(request, timeout):
                seen.append(request.get_header('Authorization'))
                if len(seen) == 1:
                    raise URLError('private detail')
                return io.BytesIO(b'{}')
            api.opener.open = open_request
            with self.subTest(api=make_api), patch('mirror.time.sleep'):
                api.request('GET', 'https://api.github.com/repos/a/b')
                self.assertEqual(seen, ['Bearer first', 'Bearer second'])

    def test_app_lookup_and_mint_transient_retries_are_scoped(self):
        import io, json, subprocess
        from urllib.error import HTTPError, URLError
        calls = []
        class Opener:
            def open(self, request, timeout):
                calls.append(request.get_method())
                if len(calls) == 1:
                    raise HTTPError(request.full_url, 503, 'secret', {}, None)
                if len(calls) == 3:
                    raise URLError('secret')
                value = {'id': 7, 'account': {'login': 'DEST'}} if request.get_method() == 'GET' else {'token': 'fake', 'expires_at': '2099-01-01T00:00:00Z'}
                return io.BytesIO(json.dumps(value).encode())
        result = subprocess.CompletedProcess([], 0, stdout=b'signature')
        with patch('mirror.build_opener', return_value=Opener()), patch('mirror.subprocess.run', return_value=result), patch('mirror.time.sleep') as sleep:
            self.assertEqual(mirror.AppTokenProvider('1', 'key', 'dest')(), 'fake')
        self.assertEqual(calls, ['GET', 'GET', 'POST', 'POST'])
        self.assertEqual([x.args[0] for x in sleep.call_args_list], [1, 1])

    def test_installation_owner_mismatch_never_mints(self):
        import io, json, subprocess
        calls = []
        class Opener:
            def open(self, request, timeout):
                calls.append(request.get_method())
                return io.BytesIO(json.dumps({'id': 7, 'account': {'login': 'other'}}).encode())
        with patch('mirror.build_opener', return_value=Opener()), patch('mirror.subprocess.run', return_value=subprocess.CompletedProcess([], 0, stdout=b'sig')):
            with self.assertRaises(ValueError):
                mirror.AppTokenProvider('1', 'key', 'dest')()
        self.assertEqual(calls, ['GET'])

    def test_invalid_token_response_is_not_cached(self):
        provider = mirror.AppTokenProvider('1', 'key', 'dest')
        with patch.object(provider, '_mint', return_value=(123, 9999999999)):
            with self.assertRaises(ValueError):
                provider()
        self.assertIsNone(provider.token)


    def test_app_auth_permanent_and_exhausted_failures(self):
        import contextlib, io, subprocess
        from urllib.error import HTTPError
        for status, expected in ((401, 1), (503, 4)):
            calls = []
            class Opener:
                def open(self, request, timeout):
                    calls.append(request.get_method())
                    raise HTTPError(request.full_url, status, 'secret-private-data', {}, None)
            logs = io.StringIO()
            with self.subTest(status=status), patch('mirror.build_opener', return_value=Opener()), patch('mirror.subprocess.run', return_value=subprocess.CompletedProcess([], 0, stdout=b'sig')), patch('mirror.time.sleep') as sleep, contextlib.redirect_stderr(logs):
                with self.assertRaises(HTTPError) as raised:
                    mirror.AppTokenProvider('1', 'key', 'dest')()
                raised.exception.close()
                self.assertEqual(calls, ['GET'] * expected)
                self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2, 4][:expected - 1])
                self.assertNotIn('secret-private-data', logs.getvalue())

    def test_unrelated_post_is_not_retried_after_transient_failure(self):
        from urllib.error import URLError
        api = mirror.API('token')
        with patch.object(api.opener, 'open', side_effect=URLError('secret')) as opened, patch('mirror.time.sleep') as sleep:
            with self.assertRaises(URLError):
                api.request('POST', 'https://api.github.com/user/repos', {'name': 'x'})
        self.assertEqual(opened.call_count, 1)
        sleep.assert_not_called()
