import copy
import http.server
import json
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from repo_mirror import adhoc
from repo_mirror.github_api import API


class AutoPinForgejoRedirectTests(unittest.TestCase):
    def test_cross_origin_metadata_redirect_cannot_pin_or_write(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            hits = []
            def do_GET(self):
                self.hits.append((self.server.server_port, self.path, self.headers.get('Authorization')))
                if self.server.redirect and self.path != '/ok':
                    self.send_response(302)
                    self.send_header('Location', self.server.redirect)
                    self.end_headers()
                else:
                    body = json.dumps({'id': 999, 'clone_url': self.server.clone}).encode()
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
                servers[0].redirect = f'https://127.0.0.1:{servers[1].server_port}/forged'
                servers[1].redirect = None
                servers[1].clone = base + '/alice/project.git'
                servers[0].clone = servers[1].clone
                cfg = {'destination_account_type': 'personal', 'sources': []}
                original = copy.deepcopy(cfg)
                class Destination:
                    def request(self, *args):
                        raise AssertionError('destination must not be contacted')
                # Production manual URLs disallow explicit ports. Permit only this
                # local fixture URL so the production redirect transport is exercised.
                url = base + '/alice/project'
                original_parse = adhoc.parse_repo_url
                def parse(value):
                    if value == url:
                        return ('forgejo', base, 'alice', 'project')
                    return original_parse(value)
                trust = ssl.create_default_context(cafile=str(cert))
                with patch.object(adhoc, 'parse_repo_url', side_effect=parse), patch('ssl._create_default_https_context', return_value=trust):
                    with self.assertRaisesRegex(ValueError, 'redirect outside configured origin'):
                        adhoc.add_to_config_pinned(cfg, url, API(), API(), Destination(), owner='bot', account_type='personal')
                self.assertEqual(cfg, original)
                self.assertEqual(Handler.hits, [(servers[0].server_port, '/api/v1/repos/alice/project', None)])
                # Hostname changes and HTTPS downgrades are rejected before a
                # connection; explicit ports are covered by the first case.
                for redirect in (f'https://localhost:{servers[0].server_port}/forged',
                                 f'http://127.0.0.1:{servers[0].server_port}/forged'):
                    servers[0].redirect = redirect
                    Handler.hits.clear()
                    with patch.object(adhoc, 'parse_repo_url', side_effect=parse), patch('ssl._create_default_https_context', return_value=trust):
                        with self.assertRaises(ValueError):
                            adhoc.add_to_config_pinned(cfg, url, API(), API(), Destination(), owner='bot', account_type='personal')
                    self.assertEqual(Handler.hits, [(servers[0].server_port, '/api/v1/repos/alice/project', None)])
                    self.assertEqual(cfg, original)
                # A same-origin redirect is accepted during all auto-pin reads.
                # Port-bearing fixture URLs are rejected by the production clone
                # validator, so isolate only that unrelated validation here.
                from test_adhoc_autopin import Destination as WorkingDestination
                servers[0].redirect = '/ok'
                Handler.hits.clear()
                with patch.object(adhoc, 'parse_repo_url', side_effect=parse), patch('ssl._create_default_https_context', return_value=trust), patch('repo_mirror.repository.validate_source_clone_url'):
                    self.assertTrue(adhoc.add_to_config_pinned(cfg, url, API(), API(), WorkingDestination(), owner='bot', account_type='personal'))
                self.assertEqual(cfg['sources'][0]['destinations']['project']['source_id'], 999)
                self.assertTrue(any(path == '/ok' for _, path, _ in Handler.hits))
                self.assertTrue(all(port == servers[0].server_port and auth is None for port, _, auth in Handler.hits))
            finally:
                for server in servers:
                    server.shutdown()
                    server.server_close()
                for thread in threads:
                    thread.join()
