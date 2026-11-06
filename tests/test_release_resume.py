import unittest
from unittest.mock import patch
import mirror


class ReleaseTests(unittest.TestCase):
    def test_existing_release_is_ignored(self):
        source = {'type':'github', 'base_url':'https://github.com', 'owner':'alice'}
        class Src:
            def request(self, *args): return {'clone_url':'https://github.com/alice/demo.git'}
            def pages(self, url):
                if url.endswith('/releases'): return [{'id':1,'tag_name':'v1','name':'new','body':'edited'}]
                raise AssertionError('existing source assets must not be fetched')
        class Dst:
            def __init__(self): self.calls=[]
            def request(self, method, url, data=None):
                self.calls.append((method,url,data))
                if url.endswith('/repos/dest/mirror-alice-demo'): return {'description':mirror.source_marker(source,'demo')}
                if '/releases/tags/' in url: return {'id':2,'name':'old','body':'original'}
                return {}
            def pages(self, url): raise AssertionError('existing destination assets must not be fetched')
        dst=Dst()
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(source,'demo','dest',Src(),dst,'token')
        self.assertFalse(any(method == 'PATCH' and '/releases/' in url for method,url,_ in dst.calls))

    def test_interrupted_release_resumes_then_ignores_later_changes(self):
        from urllib.error import HTTPError
        import io
        source = {'type':'github', 'base_url':'https://github.com', 'owner':'alice'}
        class Src:
            def __init__(self): self.assets = ['a', 'b']; self.name = 'original'
            def request(self, *args): return {'clone_url':'https://github.com/alice/demo.git'}
            def pages(self, url):
                if url.endswith('/releases'): return [{'id':1,'tag_name':'v1','name':self.name,'body':'notes'}]
                return [{'name':n,'size':1,'browser_download_url':'https://github.com/'+n} for n in self.assets]
            def download(self, url, out): out.write(url[-1:].encode())
        class Dst:
            def __init__(self): self.release=None; self.assets={}; self.fail=True
            def request(self, method, url, data=None):
                if url.endswith('/repos/dest/mirror-alice-demo'): return {'description':mirror.source_marker(source,'demo')}
                if '/releases/tags/' in url:
                    if self.release is None: raise HTTPError(url,404,'',{},io.BytesIO())
                    return self.release.copy()
                if method == 'POST' and url.endswith('/releases'):
                    self.release = dict(data, id=2, upload_url='https://uploads.github.com/x{?name}')
                    return self.release.copy()
                if method == 'PATCH' and url.endswith('/releases/2'):
                    self.release.update(data); return self.release.copy()
                return {}
            def pages(self, url):
                if url.endswith('/releases'): return [self.release.copy()] if self.release else []
                return [{'name':n,'size':len(v),'state':'uploaded','url':'https://api.github.com/'+n} for n,v in self.assets.items()]
            def upload(self, url, data, content_type):
                name=url.split('name=')[1]
                if name == 'b' and self.fail: self.fail=False; raise RuntimeError('interrupted')
                self.assets[name]=data.read()
            def download(self, url, out): out.write(self.assets[url[-1:]])
        src,dst=Src(),Dst()
        with patch.object(mirror, 'mirror_git'):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                mirror.mirror_repository(source,'demo','dest',src,dst,'token')
            self.assertEqual(dst.assets, {'a':b'a'})
            mirror.mirror_repository(source,'demo','dest',src,dst,'token')
            self.assertEqual(dst.assets, {'a':b'a','b':b'b'})
            self.assertEqual(dst.release['name'], 'original')
            self.assertEqual(dst.release['body'], 'notes')
            src.name='edited'; src.assets.append('c')
            mirror.mirror_repository(source,'demo','dest',src,dst,'token')
            self.assertEqual(dst.assets, {'a':b'a','b':b'b'})
            self.assertEqual(dst.release['name'], 'original')

    def test_repository_failure_does_not_prevent_next_repository(self):
        import json, os, tempfile
        from contextlib import redirect_stderr
        import io
        config={'destination_account_type':'personal','sources':[{'type':'github','base_url':'https://github.com','owner':'alice','repos':['one','two']}]}
        called=[]
        def run(source,name,*args):
            called.append(name)
            if name == 'one': raise RuntimeError('broken')
        with tempfile.NamedTemporaryFile(mode='w') as f:
            json.dump(config,f); f.flush()
            with patch.dict(os.environ,{'GH_TOKEN':'token','DESTINATION_OWNER':'dest'}), patch.object(mirror,'get_existing',return_value=None), patch.object(mirror,'mirror_repository',side_effect=run), redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError,'failed'):
                    mirror.main(['--config',f.name])
        self.assertEqual(called,['one','two'])

    def test_truncated_source_asset_does_not_finish_release(self):
        source={'type':'github','base_url':'https://github.com','owner':'alice'}
        class Src:
            def request(self,*args): return {'clone_url':'https://github.com/alice/demo.git'}
            def pages(self,url):
                if url.endswith('/releases'): return [{'id':1,'tag_name':'v1'}]
                return [{'name':'a','size':10,'browser_download_url':'https://github.com/a'}]
            def download(self,url,out): out.write(b'a')
        class Dst:
            def __init__(self): self.calls=[]
            def request(self,method,url,data=None):
                self.calls.append((method,url,data))
                if url.endswith('/repos/dest/mirror-alice-demo'): return {'description':mirror.source_marker(source,'demo')}
                if '/releases/tags/' in url: return {'id':2,'name':__import__('test_mirror').pending('a',10,'https://github.com/a'),'upload_url':'https://uploads.github.com/x{?name}'}
                return {}
            def pages(self,url): return []
            def upload(self,*args): pass
        dst=Dst()
        with patch.object(mirror,'mirror_git'):
            with self.assertRaisesRegex(RuntimeError,'size'):
                mirror.mirror_repository(source,'demo','dest',Src(),dst,'token')
        self.assertFalse(any(m == 'PATCH' and '/releases/' in u for m,u,_ in dst.calls))

    def test_removed_source_asset_during_retry_is_not_silently_completed(self):
        from urllib.error import HTTPError
        import io
        source={'type':'github','base_url':'https://github.com','owner':'alice'}
        class Src:
            def __init__(self): self.assets=['a','b']
            def request(self,*args): return {'clone_url':'https://github.com/alice/demo.git'}
            def pages(self,url):
                if url.endswith('/releases'): return [{'id':1,'tag_name':'v1','name':'release'}]
                return [{'name':x,'size':1,'browser_download_url':'https://github.com/'+x} for x in self.assets]
            def download(self,url,out): out.write(b'x')
        class Dst:
            def __init__(self): self.release=None
            def request(self,method,url,data=None):
                if url.endswith('/repos/dest/mirror-alice-demo'): return {'description':mirror.source_marker(source,'demo')}
                if '/releases/tags/' in url:
                    if self.release is None: raise HTTPError(url,404,'',{},io.BytesIO())
                    return self.release
                if method=='POST':
                    self.release=dict(data,id=2,upload_url='https://uploads.github.com/x{?name}'); return self.release
                if method=='PATCH': self.release.update(data)
                return {}
            def pages(self,url): return []
            def upload(self,*args): raise RuntimeError('interrupted')
        src,dst=Src(),Dst()
        with patch.object(mirror,'mirror_git'):
            with self.assertRaisesRegex(RuntimeError,'interrupted'): mirror.mirror_repository(source,'demo','dest',src,dst,'token')
            src.assets=[]
            with self.assertRaisesRegex(RuntimeError,'asset.*changed'): mirror.mirror_repository(source,'demo','dest',src,dst,'token')
