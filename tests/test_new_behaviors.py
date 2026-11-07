import io
import unittest
from unittest.mock import patch
import mirror

class NewBehaviorTests(unittest.TestCase):
    def setUp(self):
        secret = patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'})
        secret.start()
        self.addCleanup(secret.stop)

    def test_forgejo_assets_not_paginated(self):
        source = {'type':'forgejo','base_url':'https://src.example','owner':'alice'}
        class Src:
            def request(self, method, url, *args):
                if url.endswith('/releases/latest'):
                    raise __import__('urllib.error', fromlist=['HTTPError']).HTTPError(url, 404, '', {}, None)
                if url.endswith('/assets'): return []
                return {'clone_url':'https://src.example/alice/demo.git'}
            def pages(self, url):
                if url.endswith('/assets'): raise AssertionError('Forgejo ignores asset pagination')
                return [{'id': 1, 'tag_name':'v1'}]
        class Dst:
            def request(self, method, url, *args):
                if url.endswith('/repos/dest/mirror-alice-demo'): return {'description':mirror.source_marker(source,'demo')}
                if '/releases/tags/' in url: return {'id':2}
                return {}
            def pages(self, url): return []
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(source,'demo','dest',Src(),Dst(),'token')

    def test_stream_limit_and_chunk_reads(self):
        class Stream:
            def __init__(self): self.remaining = 11
            def read(self, size):
                self.assert_size = size
                n = min(size, self.remaining)
                self.remaining -= n
                return b'x' * n
        with patch.object(mirror, 'MAX_ASSET_SIZE', 10), patch.object(mirror, 'CHUNK_SIZE', 4):
            with self.assertRaisesRegex(ValueError, 'size limit'):
                mirror.copy_asset(Stream(), io.BytesIO())

    def test_push_switch_prune_order(self):
        from types import SimpleNamespace
        calls=[]
        def run(args, **kw):
            if 'push' in args: calls.append('prune' if '--prune' in args else 'push')
            return SimpleNamespace(stdout=b'refs/heads/main\n')
        with patch('subprocess.run', side_effect=run):
            mirror.mirror_git('https://src/a','https://github.com/x/a','token', lambda: calls.append('switch'))
        self.assertEqual(calls, ['push','switch','prune'])

    def test_download_streams_to_file_and_upload_sets_length(self):
        class Response:
            def __init__(self): self.data=io.BytesIO(b'abc')
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self, size=-1): return self.data.read(size)
        api=mirror.API('token')
        output=io.BytesIO()
        with patch.object(api.opener,'open',return_value=Response()):
            api.download('https://api.github.com/asset', output)
        self.assertEqual(output.getvalue(),b'abc')
        captured=[]
        def open_request(req, **kw):
            captured.append(req)
            return Response()
        output.seek(0)
        with patch.object(api.opener,'open',side_effect=open_request):
            api.upload('https://uploads.github.com/asset',output,'application/octet-stream')
        self.assertEqual(captured[0].get_header('Content-length'),'3')
        self.assertIs(captured[0].data,output)

class LocalIntegrationTests(unittest.TestCase):
    def test_real_git_prunes_stale_refs_after_callback(self):
        import os, subprocess, tempfile
        real_run = subprocess.run
        with tempfile.TemporaryDirectory() as tmp:
            src, dst, work = [os.path.join(tmp, x) for x in ('src.git','dst.git','work')]
            def git(*args):
                return real_run(['git', *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
            git('init','--bare',src); git('init','--bare',dst); git('init',work)
            git('-C',work,'config','user.email','test@example.invalid'); git('-C',work,'config','user.name','Test')
            git('-C',work,'commit','--allow-empty','-m','initial')
            git('-C',work,'branch','-M','main'); git('-C',work,'tag','keep')
            git('-C',work,'push',src,'main','keep')
            git('-C',work,'branch','stale'); git('-C',work,'tag','stale')
            git('-C',work,'push',dst,'main','refs/heads/stale','refs/tags/stale')
            git('--git-dir='+dst,'symbolic-ref','HEAD','refs/heads/main')
            def local_run(args, **kwargs):
                args = list(args)
                args[args.index('protocol.file.allow=never')] = 'protocol.file.allow=always'
                return real_run(args, **kwargs)
            observed=[]
            def switch():
                refs=git('--git-dir='+dst,'for-each-ref','--format=%(refname)').decode()
                observed.append(refs)
            with patch('subprocess.run', side_effect=local_run):
                mirror.mirror_git(src,dst,'token',switch)
            self.assertIn('refs/heads/stale',observed[0])
            refs=git('--git-dir='+dst,'for-each-ref','--format=%(refname)').decode().splitlines()
            self.assertEqual(refs,['refs/heads/main','refs/tags/keep'])

    def test_real_http_stream_upload_and_bounded_download(self):
        import http.client, threading, tempfile
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        payload=b'z'*(2*1024*1024+17)
        received=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                self.send_response(200); self.send_header('Content-Length',str(len(payload))); self.end_headers()
                self.wfile.write(payload)
            def do_POST(self):
                length=int(self.headers['Content-Length'])
                received.append((length,self.rfile.read(length),self.headers.get('Transfer-Encoding')))
                self.send_response(200); self.end_headers()
        server=ThreadingHTTPServer(('127.0.0.1',0), Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
            conn.request('GET','/asset'); response=conn.getresponse()
            with tempfile.TemporaryFile() as f:
                mirror.copy_asset(response,f); conn.close(); f.seek(0)
                class Opener:
                    def open(self, req, timeout):
                        c=http.client.HTTPConnection('127.0.0.1',server.server_port)
                        c.request(req.get_method(),'/upload',body=req.data,headers=dict(req.header_items()))
                        return c.getresponse()
                api=mirror.API('token'); api.opener=Opener()
                api.upload('https://uploads.github.com/asset',f,'application/octet-stream')
            self.assertEqual(received,[(len(payload),payload,None)])
        finally:
            server.shutdown(); server.server_close(); thread.join()
