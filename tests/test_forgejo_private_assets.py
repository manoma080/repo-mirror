import io
import http.server
import json
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import HTTPSHandler, ProxyHandler, build_opener

from repo_mirror.forgejo_source_adapter import PublicForgejoSourceAdapter
from repo_mirror.github_api import SourceAPI


class PrivateAssetsTest(unittest.TestCase):
    def test_private_https_asset_and_cross_origin_redirect(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            requests = []
            def do_GET(self):
                self.requests.append((self.path, dict(self.headers)))
                if self.path.endswith('/assets'):
                    base = f'https://127.0.0.1:{self.server.server_port}'
                    data = json.dumps([{'id': i + 1, 'browser_download_url': base + '/alice/repo/releases/download/v1/' + name} for i, name in enumerate(('asset', 'redirect', 'same'))]).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.path.endswith('/redirect'):
                    self.send_response(302)
                    self.send_header('Location', 'https://example.com/elsewhere')
                    self.end_headers()
                elif self.path.endswith('/same'):
                    self.send_response(302)
                    self.send_header('Location', '/alice/repo/releases/download/v1/asset')
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header('Content-Length', '4')
                    self.end_headers()
                    self.wfile.write(b'data')
            def log_message(self, format, *args):
                pass
        with tempfile.TemporaryDirectory() as directory:
            cert, key = Path(directory) / 'cert.pem', Path(directory) / 'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f'https://127.0.0.1:{server.server_port}'
                source = PublicForgejoSourceAdapter(base + '/alice/repo')
                trusted = ssl.create_default_context(cafile=str(cert))
                source.api.opener = build_opener(ProxyHandler({}), HTTPSHandler(context=trusted))
                with patch('ssl.create_default_context', return_value=trusted):
                    items = source.request('GET', source.base + '/releases/5/assets')
                    output = io.BytesIO()
                    source.download_verified_asset(items[0], output)
                    self.assertEqual(output.getvalue(), b'data')
                    redirected = io.BytesIO()
                    source.download_verified_asset(items[2], redirected)
                    self.assertEqual(redirected.getvalue(), b'data')
                    with self.assertRaises(ValueError):
                        source.download_verified_asset(items[1], io.BytesIO())
                    with self.assertRaisesRegex(ValueError, 'not public'):
                        SourceAPI(None).download(items[0]['browser_download_url'], io.BytesIO())
                self.assertEqual(len(Handler.requests), 5)
                self.assertTrue(all('Authorization' not in headers for _, headers in Handler.requests))
            finally:
                server.shutdown(); server.server_close(); thread.join()
