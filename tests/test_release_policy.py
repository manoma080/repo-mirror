import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import mirror

SOURCE = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice'}


class ReleasePolicyTests(unittest.TestCase):
    def test_existing_release_gets_new_assets_and_updated_metadata(self):
        class Source:
            def request(self, *args): return {'clone_url': 'https://github.com/alice/demo.git'}
            def pages(self, url):
                if url.endswith('/releases'): return [{'id': 1, 'tag_name': 'v1', 'name': 'new', 'body': 'notes'}]
                return [{'name': 'a', 'size': 1, 'browser_download_url': 'https://github.com/a'}]
            def download(self, url, out): out.write(b'a')
        class Destination:
            def __init__(self):
                self.release = {'id': 2, 'name': 'old', 'body': 'old', 'upload_url': 'https://uploads.github.com/x{?name}'}
                self.assets = []
            def request(self, method, url, data=None):
                if url.endswith('/repos/dest/mirror-alice-demo'): return {'description': mirror.source_marker(SOURCE, 'demo')}
                if '/releases/tags/' in url: return self.release.copy()
                if method == 'PATCH' and url.endswith('/releases/2'): self.release.update(data)
                return {}
            def pages(self, url): return self.assets.copy()
            def upload(self, url, data, content_type):
                self.assets.append({'name': 'a', 'size': len(data.read()), 'state': 'uploaded'})
        dst = Destination()
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', Source(), dst, 'token')
        self.assertEqual(dst.release['name'], 'new')
        self.assertEqual(dst.release['body'], 'notes')
        self.assertEqual([a['name'] for a in dst.assets], ['a'])

    def test_existing_mirror_only_considers_ten_latest_releases(self):
        class Source:
            def request(self, *args): return {'clone_url': 'https://github.com/alice/demo.git'}
            def pages(self, url): return [{'id': n, 'tag_name': str(n)} for n in range(12)] if url.endswith('/releases') else []
        class Destination:
            def __init__(self): self.tags = []
            def pages(self, url): return []
            def request(self, method, url, data=None):
                if url.endswith('/repos/dest/mirror-alice-demo'):
                    return {'description': mirror.source_marker(SOURCE, 'demo')}
                if '/releases/tags/' in url:
                    self.tags.append(url.rsplit('/', 1)[-1])
                    return {'id': 1, 'name': 'old'}
                return {}
        dst = Destination()
        with patch.object(mirror, 'mirror_git'):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', Source(), dst, 'token')
        self.assertEqual(dst.tags, [str(n) for n in range(10)])

    def test_new_repository_persists_initialization_until_history_is_complete(self):
        class Source:
            def request(self, *args): return {'clone_url': 'https://github.com/alice/demo.git'}
            def pages(self, url): raise RuntimeError('interrupted')
        class Destination:
            def __init__(self): self.description = None
            def request(self, method, url, data=None):
                if method == 'GET': raise HTTPError(url, 404, '', {}, io.BytesIO())
                if method == 'POST':
                    self.description = data['description']
                    return {'full_name': 'dest/mirror-alice-demo'}
                return {}
        dst = Destination()
        with patch.object(mirror, 'mirror_git'), self.assertRaisesRegex(RuntimeError, 'interrupted'):
            mirror.mirror_repository(SOURCE, 'demo', 'dest', Source(), dst, 'token')
        self.assertNotEqual(dst.description, mirror.source_marker(SOURCE, 'demo'))
        mirror.check_existing({'description': dst.description}, mirror.source_marker(SOURCE, 'demo'))
