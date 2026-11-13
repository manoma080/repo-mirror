import os
import unittest
from unittest.mock import patch


class FakeClient:
    origin = 'http://127.0.0.1:3300'
    base = '/api/v1/repos/team/project'

    def __init__(self):
        self.calls = []
        self.releases = []
        self.items = []
        self.latest = None
        self.release = {'id': 7, 'tag_name': 'v1'}

    def list_releases(self):
        return self.releases

    def list_assets(self, release):
        self.calls.append(('list', release))
        return self.items

    def _json(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith('/latest'):
            return self.latest
        if method == 'POST':
            self.release = dict(body, id=7)
            return dict(self.release)
        if '/assets/' in path:
            return dict(self.items[0], **body)
        if method == 'PATCH':
            self.release.update(body or {})
        return dict(self.release)

    def _request(self, method, path):
        self.calls.append((method, path))

    def download(self, asset, output):
        self.calls.append(('download', asset))
        output.write(b'abc')

    def upload(self, release, name, data, size, content_type='application/octet-stream'):
        self.calls.append(('upload', release, name, data.read(), size, content_type))
        return {'id': 9, 'name': name, 'size': size, 'browser_download_url': self.origin + '/a'}


def make_adapter():
    from repo_mirror.forgejo_destination_adapter import ForgejoDestinationAdapter
    client = FakeClient()
    return ForgejoDestinationAdapter('team/project', 'token', client=client), client


class ForgejoDestinationAdapterTests(unittest.TestCase):
    def test_asset_listing_maps_forgejo_without_pagination(self):
        from repo_mirror.forgejo_destination_adapter import ForgejoDestinationAdapter

        class Client:
            origin = 'http://127.0.0.1:3300'
            base = '/api/v1/repos/team/project'
            def list_assets(client_self, release):
                self.assertEqual(release, {'id': 7})
                return [{'id': 9, 'name': 'a', 'size': 3, 'browser_download_url': 'http://127.0.0.1:3300/a'}]

        adapter = ForgejoDestinationAdapter('team/project', 'token', client=Client())
        asset = adapter.pages(adapter.target + '/releases/7/assets')[0]
        self.assertEqual((asset['id'], asset['state'], asset['digest']), (9, 'uploaded', None))
        self.assertEqual(asset['url'], adapter.target + '/releases/assets/9')

    def test_patch_rejects_ignored_metadata_on_fresh_read(self):
        from repo_mirror.forgejo_releases import SyncConflict
        for field, expected, actual in [('name', 'new', 'old'), ('body', 'new', 'old'),
                                        ('draft', False, True), ('prerelease', False, True)]:
            with self.subTest(field=field):
                adapter, client = make_adapter()
                def request(method, path, body=None):
                    client.calls.append((method, path, body))
                    return {'id': 7, 'tag_name': 'v1', field: actual if method == 'GET' else expected}
                client._json = request
                with self.assertRaises(SyncConflict):
                    adapter.request('PATCH', adapter.target + '/releases/7', {field: expected})

    def test_patch_checks_identity_and_rejects_uncleared_name(self):
        from repo_mirror.forgejo_releases import SyncConflict
        for changed in ({'id': 8}, {'tag_name': 'v2'}, {'name': 'wrong'}):
            with self.subTest(changed=changed):
                adapter, client = make_adapter()
                calls = []
                def request(method, path, body=None):
                    calls.append(method)
                    item = {'id': 7, 'tag_name': 'v1', 'name': 'v1'}
                    if calls == ['GET', 'PATCH', 'GET']:
                        item.update(changed)
                    return item
                client._json = request
                with self.assertRaises(SyncConflict):
                    adapter.request('PATCH', adapter.target + '/releases/7', {'name': ''})
        adapter, client = make_adapter()
        original = client._json
        def normalize(method, path, body=None):
            result = original(method, path, body)
            if method == 'GET' and result.get('name') == '':
                result['name'] = result['tag_name']
            return result
        client._json = normalize
        with self.assertRaises(SyncConflict):
            adapter.request('PATCH', adapter.target + '/releases/7', {'name': ''})
        with self.assertRaises(SyncConflict):
            adapter.request('PATCH', adapter.target + '/releases/7', {'tag_name': 'v2'})

    def test_patch_checks_only_requested_fields(self):
        adapter, client = make_adapter()
        client.release.update(name='unchanged', draft=True)
        result = adapter.request('PATCH', adapter.target + '/releases/7', {'body': 'marker'})
        self.assertEqual((result['body'], result['name'], result['draft']), ('marker', 'unchanged', True))

    def test_create_patch_and_latest_policy(self):
        from repo_mirror.forgejo_releases import SyncConflict
        adapter, client = make_adapter()
        created = adapter.request('POST', adapter.target + '/releases', {'tag_name': 'v1', 'name': 'Title', 'body': 'text', 'draft': False, 'prerelease': True, 'target_commitish': 'main', 'make_latest': 'false'})
        self.assertEqual(created['upload_url'], adapter.target + '/releases/7/assets{?name,label}')
        self.assertEqual(client.calls[-1][2], {'tag_name': 'v1', 'name': 'Title', 'body': 'text', 'draft': False, 'prerelease': True, 'target_commitish': 'main'})
        adapter.request('PATCH', adapter.target + '/releases/7', {'name': 'new', 'body': '', 'make_latest': 'false'})
        self.assertEqual(client.calls[-2][2], {'name': 'new', 'body': ''})
        self.assertEqual(client.calls[-1][0], 'GET')
        client.latest = {'id': 8, 'tag_name': 'other'}
        with self.assertRaises(SyncConflict):
            adapter.request('PATCH', adapter.target + '/releases/7', {'make_latest': 'true'})
        client.latest = {'id': 7, 'tag_name': 'v1'}
        self.assertEqual(adapter.request('PATCH', adapter.target + '/releases/7', {'make_latest': 'true'})['id'], 7)
        self.assertEqual(client.calls[-1][0], 'GET')


    def test_asset_roundtrip_and_id_scoped_mutations(self):
        import io
        adapter, client = make_adapter()
        client.items = [{'id': 9, 'name': 'old', 'size': 3, 'state': 'starter', 'digest': 'sha256:x', 'browser_download_url': client.origin + '/a'}]
        item = adapter.pages(adapter.target + '/releases/7/assets')[0]
        self.assertEqual((item['state'], item['digest']), ('starter', 'sha256:x'))
        out = io.BytesIO()
        adapter.download(item['url'], out)
        self.assertEqual(out.getvalue(), b'abc')
        adapter.request('PATCH', item['url'], {'name': 'new'})
        self.assertEqual(client.calls[-1], ('PATCH', client.base + '/releases/7/assets/9', {'name': 'new'}))
        adapter.request('DELETE', item['url'])
        self.assertEqual(client.calls[-1], ('DELETE', client.base + '/releases/7/assets/9'))
        uploaded = adapter.upload(adapter.target + '/releases/7/assets?name=hello%20world', io.BytesIO(b'abc'), 'text/plain')
        self.assertEqual(uploaded['name'], 'hello world')
        self.assertEqual(client.calls[-1], ('upload', {'id': 7}, 'hello world', b'abc', 3, 'text/plain'))
        adapter.upload(adapter.target + '/releases/7/assets?name=x', io.BytesIO(b''), None)
        self.assertEqual(client.calls[-1][-1], 'application/octet-stream')

    def test_authenticated_publication_is_explicit_and_id_bound(self):
        from repo_mirror.forgejo_releases import SyncConflict
        adapter, client = make_adapter()
        client.release.update(tag_name='untagged-7', draft=True)
        payload = {'tag_name': 'v1', 'draft': False}
        with self.assertRaises(SyncConflict):
            adapter.request('PATCH', adapter.target + '/releases/7', payload)
        result = adapter.publish_authenticated_draft(7, 'untagged-7', payload)
        self.assertEqual((result['id'], result['tag_name'], result['draft']), (7, 'v1', False))
        with self.assertRaises(SyncConflict):
            adapter.publish_authenticated_draft(7, 'untagged-7', payload)

    def test_shared_publication_authenticates_synthetic_draft(self):
        import io
        from urllib.error import HTTPError
        from repo_mirror.repository import mirror_releases, draft_identity_signature, IDENTITY_SENTINEL
        for valid in (True, False):
            with self.subTest(valid=valid), patch.dict(os.environ, {'MIRROR_TIMESTAMP_SECRET': 'test-secret'}):
                adapter, client = make_adapter()
                signature = draft_identity_signature('test-secret', 'marker', 'v1', adapter.target, 7)
                client.release = {'id': 7, 'tag_name': 'untagged-7', 'draft': True,
                                  'name': 'name', 'body': 'body\n' + IDENTITY_SENTINEL + (signature if valid else '0' * 64),
                                  'prerelease': False, 'target_commitish': 'old'}
                client.releases = [client.release]
                source_release = {'id': 1, 'tag_name': 'v1', 'name': 'name', 'body': 'body',
                                  'draft': False, 'prerelease': False, 'target_commitish': 'main'}
                class Source:
                    def pages(self, url):
                        return [] if url.endswith('/assets') else [source_release]
                    def request(self, method, url):
                        return source_release
                original = client._json
                def request(method, path, body=None):
                    if method == 'GET' and '/tags/' in path:
                        raise HTTPError(path, 404, 'missing', {}, io.BytesIO())
                    if method == 'GET' and path.endswith('/latest'):
                        return dict(client.release)
                    return original(method, path, body)
                client._json = request
                def run():
                    mirror_releases({'type': 'github', 'owner': 'alice'}, 'demo', 'team', Source(), adapter,
                                    'https://source/repos/alice/demo', adapter.target, 'marker', initializing=True)
                if valid:
                    run()
                    self.assertEqual((client.release['id'], client.release['tag_name'], client.release['draft'],
                                      client.release['target_commitish']), (7, 'v1', False, 'main'))
                else:
                    with self.assertRaises(Exception):
                        run()
                    self.assertFalse(any(call[0] == 'PATCH' for call in client.calls))


    def test_rejects_untrusted_urls_and_unknown_asset(self):
        from repo_mirror.forgejo_releases import SyncConflict
        adapter, client = make_adapter()
        with self.assertRaises(ValueError):
            adapter.pages('http://evil.example/releases')
        with self.assertRaises(SyncConflict):
            adapter.request('DELETE', adapter.target + '/releases/assets/9')
        with self.assertRaises(ValueError):
            adapter.upload(adapter.target + '/releases/7/assets?name=x&name=y', None, 'x')


    def test_constructor_rejects_unsafe_token_and_origin(self):
        from repo_mirror.forgejo_destination_adapter import ForgejoDestinationAdapter
        with self.assertRaises(ValueError):
            ForgejoDestinationAdapter('team/project', 'bad\r\ntoken', client=FakeClient())
        client = FakeClient()
        client.origin = 'http://example.com:3300'
        with self.assertRaises(ValueError):
            ForgejoDestinationAdapter('team/project', 'token', client=client)


    def test_shared_orchestrator_uses_adapter(self):
        import io
        from urllib.error import HTTPError
        from repo_mirror.repository import mirror_releases

        env_patch = patch.dict(os.environ, {'MIRROR_TIMESTAMP_SECRET': 'test-secret'})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        adapter, client = make_adapter()
        source_release = {'id': 1, 'tag_name': 'v1', 'name': 'name', 'body': 'body', 'draft': False, 'prerelease': False, 'target_commitish': 'main'}
        class Source:
            def pages(self, url):
                return [] if url.endswith('/assets') else [source_release]
            def request(self, method, url):
                if url.endswith('/latest'):
                    return source_release
                raise AssertionError(url)
        original = client._json
        def request(method, path, body=None):
            if method == 'GET' and '/tags/' in path:
                raise HTTPError(path, 404, 'missing', {}, io.BytesIO())
            if method == 'GET' and path.endswith('/latest'):
                return {'id': 7, 'tag_name': 'v1'}
            return original(method, path, body)
        client._json = request
        mirror_releases({'type': 'github', 'owner': 'alice'}, 'demo', 'team', Source(), adapter, 'https://source/repos/alice/demo', adapter.target, 'marker', initializing=True)
        self.assertTrue(any(call[0] == 'POST' for call in client.calls))
        self.assertTrue(any(call[0] == 'PATCH' for call in client.calls))
        self.assertEqual(next(call[2]['target_commitish'] for call in client.calls if call[0] == 'POST'), 'main')
        self.assertEqual(client.release['target_commitish'], 'main')
