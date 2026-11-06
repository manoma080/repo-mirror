import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import mirror


class FinalVerificationTests(unittest.TestCase):
    def run_scenario(self, *, source_change=False, destination='uploaded'):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice'}

        class Src:
            def __init__(self): self.changed = False
            def request(self, *args): return {'clone_url': 'https://github.com/alice/demo.git'}
            def pages(self, url):
                if url.endswith('/releases'): return [{'id': 1, 'tag_name': 'v1', 'name': 'release'}]
                assets = [{'name': 'a', 'size': 1, 'id': 1, 'browser_download_url': 'https://github.com/a'}]
                if self.changed: assets.append({'name': 'b', 'size': 1, 'id': 2, 'browser_download_url': 'https://github.com/b'})
                return assets
            def download(self, url, output): output.write(b'a')

        src = Src()
        class Dst:
            def __init__(self): self.release = None; self.uploaded = False; self.patches = []
            def request(self, method, url, data=None):
                if url.endswith('/repos/dest/mirror-alice-demo'): return {'description': mirror.source_marker(source, 'demo')}
                if '/releases/tags/' in url: raise HTTPError(url, 404, '', {}, io.BytesIO())
                if method == 'POST' and url.endswith('/releases'):
                    self.release = dict(data, id=2, upload_url='https://uploads.github.com/x{?name}')
                    return self.release.copy()
                if method == 'PATCH' and url.endswith('/releases/2'):
                    self.patches.append(data); self.release.update(data)
                return {}
            def pages(self, url):
                if url.endswith('/releases') or not self.uploaded or destination == 'missing': return []
                return [{'name': 'a', 'size': 0 if destination == 'wrong-size' else 1,
                         'state': destination, 'url': 'https://api.github.com/a'}]
            def upload(self, url, data, content_type):
                self.uploaded = True
                if source_change: src.changed = True
                return {'name': 'a', 'size': 1, 'state': 'uploaded'}

        dst = Dst()
        with patch.object(mirror, 'mirror_git'):
            with self.assertRaises(RuntimeError):
                mirror.mirror_repository(source, 'demo', 'dest', src, dst, 'token')
        self.assertTrue(dst.uploaded)
        self.assertTrue(dst.release['name'].startswith('[repo-mirror:pending:v1]'))
        self.assertEqual(dst.patches, [])

    def test_source_manifest_changes_during_upload(self):
        self.run_scenario(source_change=True)

    def test_destination_missing_after_successful_upload(self):
        self.run_scenario(destination='missing')

    def test_destination_starter_after_successful_upload(self):
        self.run_scenario(destination='starter')

    def test_destination_wrong_size_after_successful_upload(self):
        self.run_scenario(destination='wrong-size')

    def test_destination_nonuploaded_state_after_successful_upload(self):
        self.run_scenario(destination='processing')
