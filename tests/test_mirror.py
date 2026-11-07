import unittest
import base64
import json
import mirror


def pending(asset_name, size, url, name=None):
    import hashlib
    manifest = [[asset_name, size, None, None, url]]
    digest = hashlib.sha256(json.dumps(manifest, separators=(',', ':')).encode()).hexdigest()
    return '[repo-mirror:pending:v1]' + base64.urlsafe_b64encode(json.dumps([name, digest]).encode()).decode()


class DiscoveryTests(unittest.TestCase):
    def test_forgejo_all_discovery_paginates(self):
        class API:
            def pages(self, url):
                self.url = url
                return [{'name': 'a'}, {'name': 'b'}]
        api = API()
        source = {'type': 'forgejo', 'base_url': 'https://code.example', 'owner': 'alice', 'repos': 'all'}
        self.assertEqual(mirror.discover(source, api), ['a', 'b'])
        self.assertEqual(api.url, 'https://code.example/api/v1/users/alice/repos')


class EngineTests(unittest.TestCase):
    def test_existing_repo_is_secured_and_release_assets_reconciled(self):
        class API:
            def __init__(self): self.calls = []; self.uploaded = False
            def request(self, method, url, data=None, **kwargs):
                if url.endswith('/releases/latest'):
                    raise __import__('urllib.error', fromlist=['HTTPError']).HTTPError(url, 404, '', {}, None)
                self.calls.append((method, url, data))
                if url.endswith('/repos/dest/pre-demo') and method == 'GET': return {'private': True, 'description': '[repo-mirror: forgejo:https://src.example/alice/demo]'}
                if url.endswith('/repos/alice/demo') and method == 'GET': return {'clone_url': 'https://src.example/alice/demo.git'}
                if url.endswith('/releases/tags/v1'): return {'id': 11, 'name': pending('bin.zip', 4, 'https://src.example/bin.zip'), 'upload_url': 'https://uploads.github.com/repos/dest/pre-demo/releases/11/assets{?name,label}'}
                if url.endswith('/releases/4/assets'): return [{'name': 'bin.zip', 'browser_download_url': 'https://src.example/bin.zip', 'size': 4}]
                if url.endswith('/assets?per_page=100&page=1'): return []
                return {}
            def pages(self, url):
                if url.endswith('/releases'): return [{'id': 4, 'tag_name': 'v1', 'name': 'one', 'body': 'notes', 'draft': False, 'prerelease': False, 'assets': [], 'tarball_url': 'https://src.example/tar'}]
                if url.endswith('/releases/4/assets'): return [{'name': 'bin.zip', 'browser_download_url': 'https://src.example/bin.zip', 'size': 4}]
                if url.endswith('/releases/11/assets') and self.uploaded: return [{'name': 'bin.zip', 'size': 4, 'state': 'uploaded', 'url': 'https://api.github.com/bin.zip'}]
                return []
            def download(self, url, output): output.write(b'data')
            def upload(self, url, data, content_type): self.calls.append(('UPLOAD', url, data)); self.uploaded = True
        src, dst = API(), API()
        source = {'type': 'forgejo', 'base_url': 'https://src.example', 'owner': 'alice', 'repos': ['demo'], 'prefix': 'pre-'}
        import unittest.mock
        with unittest.mock.patch.object(mirror, 'mirror_git') as git:
            mirror.mirror_repository(source, 'demo', 'dest', src, dst, 'secret')
        self.assertTrue(any(c[0] == 'PUT' and c[1].endswith('/actions/permissions') and c[2] == {'enabled': False} for c in dst.calls))
        self.assertTrue(any(c[0] == 'UPLOAD' and 'name=bin.zip' in c[1] for c in dst.calls))
        git.assert_called_once()

    def test_rejects_non_https_and_credentialed_sources(self):
        for base in ('http://evil.example', 'https://secret@evil.example'):
            with self.assertRaises(ValueError): mirror.validate_source({'type':'github', 'base_url':base, 'owner':'a', 'repos':['b']})

    def test_forgejo_source_assets_are_loaded_directly(self):
        class API:
            def __init__(self): self.visited = []; self.uploaded = False
            def request(self, method, url, data=None, **kw):
                if url.endswith('/releases/latest'):
                    raise __import__('urllib.error', fromlist=['HTTPError']).HTTPError(url, 404, '', {}, None)
                if url.endswith('/repos/dest/mirror-alice-demo'): return {'private': False, 'description': '[repo-mirror: forgejo:https://src.example/alice/demo]'}
                if url.endswith('/repos/alice/demo'): return {'clone_url': 'https://src.example/alice/demo.git'}
                if url.endswith('/releases/tags/v1'): return {'id': 9, 'name': pending('file', 1, 'https://src.example/file'), 'tag_name': 'v1', 'upload_url': 'https://uploads.github.com/x{?name}'}
                if url.endswith('/releases/4/assets'):
                    self.visited.append(url)
                    return [{'name':'file', 'size':1, 'browser_download_url':'https://src.example/file'}]
                return {}
            def pages(self, url):
                self.visited.append(url)
                if url.endswith('/releases'): return [{'id': 4, 'tag_name': 'v1', 'assets': []}]
                if url.endswith('/releases/4/assets'): return [{'name':'file', 'size':1, 'browser_download_url':'https://src.example/file'}]
                if url.endswith('/releases/9/assets') and self.uploaded: return [{'name':'file', 'size':1, 'state':'uploaded', 'url':'https://api.github.com/file'}]
                return []
            def download(self, url, output): output.write(b'x')
            def upload(self, url, data, content_type): self.visited.append(url); self.uploaded = True
        src, dst = API(), API()
        import unittest.mock
        with unittest.mock.patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository({'type':'forgejo', 'base_url':'https://src.example', 'owner':'alice'}, 'demo', 'dest', src, dst, 'token')
        self.assertIn('https://src.example/api/v1/repos/alice/demo/releases/4/assets', src.visited)
        self.assertTrue(any('name=file' in url for url in dst.visited))

    def test_git_does_not_receive_destination_token_for_source_fetch(self):
        import os
        from unittest.mock import patch
        calls = []
        def run(args, **kwargs):
            from types import SimpleNamespace
            calls.append((args, kwargs['env']))
            return SimpleNamespace(stdout=b'refs/heads/main\n')
        with patch.dict(os.environ, {'GH_TOKEN': 'secret'}), patch('subprocess.run', side_effect=run):
            mirror.mirror_git('https://src.example/a/b.git', 'https://github.com/d/b.git', 'secret')
        fetch_env = next(env for args, env in calls if 'fetch' in args)
        self.assertNotIn('GH_TOKEN', fetch_env)
        self.assertNotIn('GIT_CONFIG_VALUE_0', fetch_env)


class SafetyTests(unittest.TestCase):
    def test_default_destination_name_and_marker(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'Alice', 'repos': ['demo']}
        self.assertEqual(mirror.destination_name(source, 'demo'), 'mirror-Alice-demo')
        self.assertIn('Alice/demo', mirror.source_marker(source, 'demo'))

    def test_rejects_unrelated_existing_repository(self):
        from unittest.mock import patch
        class API:
            def request(self, method, url, data=None):
                if url.endswith('/releases/latest'):
                    raise __import__('urllib.error', fromlist=['HTTPError']).HTTPError(url, 404, '', {}, None)
                return {'private': True, 'description': 'unrelated'}
        source = {'type':'github', 'base_url':'https://github.com', 'owner':'Alice', 'repos':['demo']}
        with patch.object(mirror, 'mirror_git') as git:
            with self.assertRaisesRegex(RuntimeError, 'collision'):
                mirror.mirror_repository(source, 'demo', 'dest', API(), API(), 'token')
            git.assert_not_called()

    def test_personal_creation_never_queries_user(self):
        import io
        from urllib.error import HTTPError
        from unittest.mock import patch
        class Destination:
            def __init__(self): self.calls = []
            def request(self, method, url, data=None):
                self.calls.append((method, url, data))
                if method == 'GET': raise HTTPError(url, 404, 'not found', {}, io.BytesIO())
                if method == 'POST': return {'full_name':'dest/mirror-Alice-demo'}
                return {}
            def pages(self, url): return []
        class Source:
            def request(self, *a):
                if a[1].endswith('/releases/latest'):
                    raise __import__('urllib.error', fromlist=['HTTPError']).HTTPError(a[1], 404, '', {}, None)
                return {'clone_url':'https://github.com/Alice/demo.git'}
            def pages(self, url): return []
        dst = Destination()
        source = {'type':'github','base_url':'https://github.com','owner':'Alice','repos':['demo']}
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(source, 'demo', 'dest', Source(), dst, 'token', 'personal')
        self.assertTrue(any(method == 'POST' and url.endswith('/user/repos') for method,url,_ in dst.calls))
        self.assertFalse(any(method == 'GET' and url.endswith('/user') for method,url,_ in dst.calls))

    def test_asset_same_size_different_bytes_fails_without_deletion(self):
        from unittest.mock import patch
        source = {'type':'github','base_url':'https://github.com','owner':'Alice','repos':['demo']}
        class Src:
            def request(self, *a):
                if a[1].endswith('/releases/latest'):
                    raise __import__('urllib.error', fromlist=['HTTPError']).HTTPError(a[1], 404, '', {}, None)
                return {'clone_url':'https://github.com/Alice/demo.git'}
            def pages(self, url):
                if url.endswith('/releases'): return [{'id':1,'tag_name':'v1'}]
                return [{'name':'bin','size':1,'browser_download_url':'https://github.com/bin'}]
            def download(self, url, output): output.write(b'a')
        class Dst:
            def __init__(self): self.calls=[]
            def request(self, method, url, data=None):
                self.calls.append((method,url))
                if url.endswith('/repos/dest/mirror-Alice-demo'): return {'description':mirror.source_marker(source,'demo')}
                if '/releases/tags/' in url: return {'id':2,'name':pending('bin', 1, 'https://github.com/bin'),'tag_name':'v1'}
                return {}
            def pages(self, url): return [{'name':'bin','size':1,'url':'https://api.github.com/asset'}]
            def download(self, url, output): output.write(b'b')
        dst=Dst()
        with patch.object(mirror, 'mirror_git'):
            with self.assertRaisesRegex(RuntimeError, 'mismatch'):
                mirror.mirror_repository(source,'demo','dest',Src(),dst,'token')
        self.assertFalse(any(method == 'DELETE' for method,url in dst.calls))

    def test_public_marked_mirror_with_no_releases_succeeds_without_visibility_change(self):
        from unittest.mock import patch
        source = {'type':'github','base_url':'https://github.com','owner':'Alice','repos':['demo']}
        class Src:
            def request(self, *args):
                if args[1].endswith('/releases/latest'):
                    raise __import__('urllib.error', fromlist=['HTTPError']).HTTPError(args[1], 404, '', {}, None)
                return {'clone_url':'https://github.com/Alice/demo.git'}
            def pages(self, url): return []
        class Dst:
            def __init__(self): self.calls=[]
            def request(self, method, url, data=None):
                self.calls.append((method,url,data))
                if method == 'GET': return {'private':False,'description':mirror.source_marker(source,'demo')}
                return {}
        dst=Dst()
        with patch.object(mirror, 'mirror_git') as git:
            mirror.mirror_repository(source, 'demo', 'dest', Src(), dst, 'token')
        git.assert_called_once()
        self.assertFalse(any(method == 'PATCH' and isinstance(data, dict) and 'private' in data for method,url,data in dst.calls))
