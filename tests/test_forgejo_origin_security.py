import http.server
import json
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from repo_mirror.forgejo_source_adapter import PublicForgejoSourceAdapter


class ForgejoOriginSecurityTests(unittest.TestCase):
    def test_asset_paths_and_default_port(self):
        source = PublicForgejoSourceAdapter('https://code.example/alice/repo')
        prefix = 'https://code.example/alice/repo/releases/download/'
        for suffix in ('../other', '%2e%2e/other', '%252e%252e/other', 'v1/%2e%2e/other', 'v1%2f..%2fother'):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                source._normalize_assets([{'id': 1, 'browser_download_url': prefix + suffix}])
        url = 'https://code.example:443/alice/repo/releases/download/v1/file'
        self.assertEqual(source._normalize_assets([{'id': 2, 'browser_download_url': url}])[0]['browser_download_url'], url)

    def test_real_tls_api_redirects_are_origin_bound(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            hits = []
            def do_GET(self):
                self.hits.append((self.server.server_port, self.path))
                if self.path == '/same':
                    self.send_response(302); self.send_header('Location', '/ok'); self.end_headers()
                elif self.path == '/cross':
                    self.send_response(302); self.send_header('Location', self.server.other + '/stolen'); self.end_headers()
                elif self.path == '/downgrade':
                    self.send_response(302); self.send_header('Location', 'http://127.0.0.1:1/stolen'); self.end_headers()
                else:
                    body = json.dumps({'ok': True}).encode()
                    self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self, *args):
                pass
        with tempfile.TemporaryDirectory() as directory:
            cert, key = Path(directory) / 'cert.pem', Path(directory) / 'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(cert, key)
            servers = [http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler) for _ in range(2)]
            threads = []
            try:
                for server in servers:
                    server.socket = server_context.wrap_socket(server.socket, server_side=True)
                    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start(); threads.append(thread)
                base = f'https://127.0.0.1:{servers[0].server_port}'
                other = f'https://127.0.0.1:{servers[1].server_port}'
                servers[0].other = other
                trusted = ssl.create_default_context(cafile=str(cert))
                with patch('ssl._create_default_https_context', return_value=trusted):
                    source = PublicForgejoSourceAdapter(base + '/alice/repo')
                    self.assertEqual(source.request('GET', base + '/same'), {'ok': True})
                    with self.assertRaises(ValueError):
                        source.request('GET', base + '/cross')
                    with self.assertRaises(ValueError):
                        source.request('GET', base + '/downgrade')
                    with self.assertRaises(ValueError):
                        source.request('GET', other + '/direct')
                self.assertEqual(Handler.hits, [(servers[0].server_port, '/same'), (servers[0].server_port, '/ok'), (servers[0].server_port, '/cross'), (servers[0].server_port, '/downgrade')])
            finally:
                for server in servers:
                    server.shutdown(); server.server_close()
                for thread in threads:
                    thread.join()
