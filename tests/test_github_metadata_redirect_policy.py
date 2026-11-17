import json
import http.server
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from repo_mirror import cli
from repo_mirror.forgejo_source_adapter import ForgejoSourceAdapter
from repo_mirror.github_api import SourceAPI, OriginRedirect


class GitHubMetadataRedirectPolicyTests(unittest.TestCase):
    def test_cli_restricts_metadata_client_before_discovery(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'old', 'repos': ['repo']}
        client = SourceAPI('secret')
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps({'destination_account_type': 'personal', 'sources': [source]}))
            with patch.dict('os.environ', {'GH_TOKEN': 'secret', 'DESTINATION_OWNER': 'dest', 'MIRROR_TIMESTAMP_SECRET': 'fixture-secret'}, clear=True), \
                 patch.object(cli, 'SourceAPI', return_value=client), \
                 patch.object(cli, 'discover', side_effect=RuntimeError('stop')):
                try:
                    cli.main(['--config', str(config)])
                except RuntimeError:
                    pass
        self.assertEqual(getattr(client, 'trusted_api_origin', None), ('https', 'api.github.com', 443))
        self.assertTrue(any(isinstance(handler, OriginRedirect) for handler in client.opener.handlers))

    def test_default_forgejo_github_adapter_restricts_metadata_client(self):
        adapter = ForgejoSourceAdapter('owner/repo', 'secret', client=Mock())
        self.assertEqual(getattr(adapter.api, 'trusted_api_origin', None), ('https', 'api.github.com', 443))

    def test_real_tls_redirect_rejects_forged_identity_without_credential_leak(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            hits = []
            def do_GET(self):
                self.hits.append((self.server.server_port, self.path, self.headers.get('Authorization')))
                if self.path == '/first':
                    self.send_response(302)
                    self.send_header('Location', '/second')
                    self.end_headers()
                elif self.path == '/second':
                    self.send_response(302)
                    self.send_header('Location', self.server.target)
                    self.end_headers()
                else:
                    body = json.dumps({'id': 1, 'full_name': 'forged/repo'}).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as directory:
            cert, key = Path(directory) / 'cert.pem', Path(directory) / 'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            servers = [http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler) for _ in range(2)]
            threads = []
            try:
                for server in servers:
                    server.socket = context.wrap_socket(server.socket, server_side=True)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    threads.append(thread)
                base = f'https://127.0.0.1:{servers[0].server_port}'
                servers[0].target = f'https://127.0.0.1:{servers[1].server_port}/forged'
                trust = ssl.create_default_context(cafile=str(cert))
                client = SourceAPI('secret')
                with patch('ssl._create_default_https_context', return_value=trust), \
                     patch.object(client, '_request_token', return_value='secret'):
                    client.restrict_origin(base)
                    with self.assertRaisesRegex(ValueError, 'redirect outside configured origin'):
                        client.request('GET', base + '/first')
                    self.assertEqual(Handler.hits, [(servers[0].server_port, '/first', 'Bearer secret'),
                                                    (servers[0].server_port, '/second', 'Bearer secret')])
                    Handler.hits.clear()
                    servers[0].target = '/ok'
                    self.assertEqual(client.request('GET', base + '/first')['id'], 1)
                    self.assertEqual([path for _, path, _ in Handler.hits], ['/first', '/second', '/ok'])
            finally:
                for server in servers:
                    server.shutdown()
                    server.server_close()
                for thread in threads:
                    thread.join()
