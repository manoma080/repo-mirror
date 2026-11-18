import copy
import unittest
from unittest.mock import patch

from repo_mirror.repository import mirror_releases
from repo_mirror.journal import ReleaseRecoveryError
from test_forgejo_space_body_integration import GitHubAPI, GitHubShapedDestination


class TagSource:
    release_identity_kind = 'tag'
    release_scope = ('https://gitlab.example', 42)

    def __init__(self):
        self.item = dict(tag_name='v/1', name='title', body='notes', draft=False,
                         prerelease=False, target_commitish=None, released_at='2025-01-01T00:00:00Z')

    def list_releases(self):
        return [copy.deepcopy(self.item)]

    def latest_release(self, releases):
        return copy.deepcopy(releases[0])

    def get_release(self, tag):
        assert tag == self.item['tag_name']
        return copy.deepcopy(self.item)

    def list_release_assets(self, tag):
        return []

    def verify_release_tag(self, tag, dst, target):
        return ('commit', 'a' * 40)


class TagReleaseEngineTest(unittest.TestCase):
    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'})
    def test_tag_object_drift_retains_pending_marker(self):
        src = TagSource()
        reads = []
        def verify(tag, dst, target):
            reads.append(tag)
            return ('commit', ('a' if len(reads) == 1 else 'b') * 40)
        src.verify_release_tag = verify
        api = GitHubAPI(existing=False)
        dst = GitHubShapedDestination('team/project', 'token', client=api)
        with self.assertRaisesRegex(ValueError, 'tag verification failed'):
            mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src,
                            dst, 'unused', dst.target, 'marker', initializing=True)
        self.assertIn('[repo-mirror:pending:v3]', api.release['body'])

    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'})
    def test_late_link_appearance_prevents_finalization(self):
        src = TagSource()
        calls = []
        def links(tag):
            calls.append(tag)
            return [] if len(calls) < 4 else [{'id': 5, 'name': 'file', 'url': 'https://example.com/file'}]
        src.list_release_assets = links
        api = GitHubAPI(existing=False)
        dst = GitHubShapedDestination('team/project', 'token', client=api)
        with self.assertRaises(ReleaseRecoveryError):
            mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src,
                            dst, 'unused', dst.target, 'marker', initializing=True)
        self.assertIn('[repo-mirror:pending:v3]', api.release['body'])

    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'})
    def test_release_metadata_drift_retains_signed_pending_marker(self):
        src = TagSource()
        get_release = src.get_release
        reads = []
        def changed(tag):
            reads.append(tag)
            result = get_release(tag)
            if len(reads) > 1:
                result['released_at'] = '2026-01-01T00:00:00Z'
            return result
        src.get_release = changed
        api = GitHubAPI(existing=False)
        dst = GitHubShapedDestination('team/project', 'token', client=api)
        with self.assertRaisesRegex(ValueError, 'source release changed'):
            mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src,
                            dst, 'unused', dst.target, 'marker', initializing=True)
        self.assertIn('[repo-mirror:pending:v3]', api.release['body'])

    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'})
    def test_pending_retry_rejects_release_metadata_drift(self):
        src = TagSource()
        api = GitHubAPI(existing=False)
        dst = GitHubShapedDestination('team/project', 'token', client=api)
        original_verify = src.verify_release_tag
        calls = []
        def interrupted(tag, destination, target):
            calls.append(tag)
            if len(calls) > 1:
                raise ValueError('simulated interruption')
            return original_verify(tag, destination, target)
        src.verify_release_tag = interrupted
        with self.assertRaisesRegex(ValueError, 'simulated interruption'):
            mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src,
                            dst, 'unused', dst.target, 'marker', initializing=True)
        self.assertIn('[repo-mirror:pending:v3]', api.release['body'])
        src.verify_release_tag = original_verify
        src.item['body'] = 'changed after interruption'
        with self.assertRaises(ReleaseRecoveryError):
            mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src,
                            dst, 'unused', dst.target, 'marker', initializing=True)
        self.assertIn('[repo-mirror:pending:v3]', api.release['body'])

    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'})
    def test_small_link_is_transferred_to_destination(self):
        from contextlib import contextmanager
        from io import BytesIO
        from repo_mirror.gitlab_assets import Download

        src = TagSource()
        src.list_release_assets = lambda tag: [dict(id=5, name='file', url='https://example.com/file', link_type='other')]
        api = GitHubAPI(existing=False)
        dst = GitHubShapedDestination('team/project', 'token', client=api)

        @contextmanager
        def download(url, max_bytes):
            yield Download(False, BytesIO(b'asset bytes'), len(b'asset bytes'))

        with patch('repo_mirror.gitlab_assets.download_asset', download):
            mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src,
                            dst, 'unused', dst.target, 'marker', initializing=True)
        self.assertEqual(list(api.assets.values()), [('file', b'asset bytes')])
        self.assertEqual(api.release['body'], 'notes')

    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'})
    def test_unsafe_links_fail_closed_before_destination_writes(self):
        src = TagSource()
        src.list_release_assets = lambda tag: [{'id': 5, 'name': 'file', 'url': 'http://127.0.0.1/file'}]
        api = GitHubAPI(existing=False)
        dst = GitHubShapedDestination('team/project', 'token', client=api)
        with self.assertRaises(ValueError):
            mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src,
                            dst, 'unused', dst.target, 'marker', initializing=True)
        self.assertFalse(any(method != 'GET' for method, _, _ in api.calls))

    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'})
    def test_mixed_links_preserve_oversized_and_upload_small(self):
        from contextlib import contextmanager
        from io import BytesIO
        from repo_mirror.gitlab_assets import Download
        src = TagSource()
        src.list_release_assets = lambda tag: [
            dict(id=5, name='small', url='https://example.com/small', link_type='other'),
            dict(id=6, name='big', url='https://example.com/big', link_type='package')]
        api = GitHubAPI(existing=False)
        dst = GitHubShapedDestination('team/project', 'token', client=api)
        @contextmanager
        def download(url, cap):
            yield Download(True) if url.endswith('big') else Download(False, BytesIO(b'ok'), 2)
        with patch('repo_mirror.gitlab_assets.download_asset', download):
            mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src,
                            dst, 'unused', dst.target, 'marker', initializing=True)
        self.assertEqual(list(api.assets.values()), [('small', b'ok')])
        self.assertIn('notes\n\nOriginal oversized release asset links:', api.release['body'])
        self.assertIn('[big](https://example.com/big)', api.release['body'])

    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'})
    def test_oversized_link_conflicting_with_destination_fails_closed(self):
        from contextlib import contextmanager
        from repo_mirror.gitlab_assets import Download
        from repo_mirror.forgejo_destination_adapter import ForgejoDestinationAdapter
        from test_forgejo_space_body_integration import ForgejoAPI
        src = TagSource()
        src.list_release_assets = lambda tag: [dict(id=5, name='file', url='https://example.com/file')]
        @contextmanager
        def download(url, cap):
            yield Download(True)
        for cls, adapter in ((GitHubAPI, GitHubShapedDestination), (ForgejoAPI, ForgejoDestinationAdapter)):
            with self.subTest(destination=cls.__name__), patch.dict('os.environ', {'MIRROR_FORGEJO_ASSET_MAX_BYTES': '1000'}):
                api = cls(); api.release['tag_name'] = 'v/1'; api.assets = {201: ('file', b'OLD')}
                dst = adapter('team/project', 'token', client=api)
                with patch('repo_mirror.gitlab_assets.download_asset', download):
                    with self.assertRaises(ReleaseRecoveryError):
                        mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src,
                                        dst, 'unused', dst.target, 'marker', initializing=True)
                self.assertEqual(api.assets[201], ('file', b'OLD'))
                self.assertFalse(any(method == 'DELETE' for method, _, _ in api.calls))

    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'})
    def test_four_links_require_linear_downloads(self):
        from contextlib import contextmanager
        from io import BytesIO
        from repo_mirror.gitlab_assets import Download
        src = TagSource()
        src.list_release_assets = lambda tag: [dict(id=i, name=str(i), url='https://example.com/' + str(i)) for i in range(1, 5)]
        api = GitHubAPI(existing=False)
        dst = GitHubShapedDestination('team/project', 'token', client=api)
        downloads = []
        @contextmanager
        def download(url, cap):
            downloads.append(url)
            yield Download(False, BytesIO(b'ok'), 2)
        with patch('repo_mirror.gitlab_assets.download_asset', download):
            mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src,
                            dst, 'unused', dst.target, 'marker', initializing=True)
        self.assertEqual(len(downloads), 12, downloads)
        self.assertEqual(len(api.assets), 4)

    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'})
    def test_interrupted_link_upload_recovers_without_deletion(self):
        from contextlib import contextmanager
        from io import BytesIO
        from repo_mirror.gitlab_assets import Download
        src = TagSource()
        src.list_release_assets = lambda tag: [dict(id=5, name='file', url='https://example.com/file')]
        api = GitHubAPI(existing=False)
        dst = GitHubShapedDestination('team/project', 'token', client=api)
        verify = src.verify_release_tag
        calls = []
        def interrupted(*args):
            calls.append(1)
            if len(calls) > 1:
                raise ValueError('interrupted')
            return verify(*args)
        src.verify_release_tag = interrupted
        @contextmanager
        def download(url, cap):
            yield Download(False, BytesIO(b'ok'), 2)
        with patch('repo_mirror.gitlab_assets.download_asset', download):
            with self.assertRaisesRegex(ValueError, 'interrupted'):
                mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src,
                                dst, 'unused', dst.target, 'marker', initializing=True)
            src.verify_release_tag = verify
            mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src,
                            dst, 'unused', dst.target, 'marker', initializing=True)
        self.assertEqual(api.release['body'], 'notes')
        self.assertEqual(list(api.assets.values()), [('file', b'ok')])
        self.assertFalse(any(method == 'DELETE' for method, _, _ in api.calls))

    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'})
    def test_reordered_links_between_reads_do_not_change_snapshot(self):
        from contextlib import contextmanager
        from io import BytesIO
        from repo_mirror.gitlab_assets import Download
        src = TagSource()
        links = [dict(id=i, name=str(i), url='https://example.com/' + str(i)) for i in (2, 1)]
        reads, downloads = [], []
        def list_links(tag):
            reads.append(1)
            result = links if len(reads) % 2 else list(reversed(links))
            return [dict(link, download_count=len(reads)) for link in result]
        src.list_release_assets = list_links
        api = GitHubAPI(existing=False)
        dst = GitHubShapedDestination('team/project', 'token', client=api)
        @contextmanager
        def download(url, cap):
            downloads.append(url)
            yield Download(False, BytesIO(b'ok'), 2)
        with patch('repo_mirror.gitlab_assets.download_asset', download):
            mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src,
                            dst, 'unused', dst.target, 'marker', initializing=True)
        self.assertEqual(api.release['body'], 'notes')
        self.assertEqual(set(api.assets.values()), {('1', b'ok'), ('2', b'ok')})
        self.assertEqual(len(downloads), 6)

    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'})
    def test_reordered_links_recover_bound_release(self):
        from contextlib import contextmanager
        from io import BytesIO
        from repo_mirror.gitlab_assets import Download
        src = TagSource()
        links = [dict(id=i, name=str(i), url='https://example.com/' + str(i)) for i in (2, 1)]
        src.list_release_assets = lambda tag: copy.deepcopy(links)
        api = GitHubAPI(existing=False)
        dst = GitHubShapedDestination('team/project', 'token', client=api)
        verify = src.verify_release_tag
        calls, downloads = [], []
        def interrupted(*args):
            calls.append(1)
            if len(calls) > 1:
                raise ValueError('interrupted')
            return verify(*args)
        src.verify_release_tag = interrupted
        @contextmanager
        def download(url, cap):
            downloads.append(url)
            yield Download(False, BytesIO(b'ok'), 2)
        with patch('repo_mirror.gitlab_assets.download_asset', download):
            with self.assertRaisesRegex(ValueError, 'interrupted'):
                mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src,
                                dst, 'unused', dst.target, 'marker', initializing=True)
            self.assertIn('[repo-mirror:pending:v3]', api.release['body'])
            links.reverse()
            src.verify_release_tag = verify
            mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src,
                            dst, 'unused', dst.target, 'marker', initializing=True)
        self.assertEqual(api.release['body'], 'notes')
        self.assertEqual(set(api.assets.values()), {('1', b'ok'), ('2', b'ok')})
        self.assertFalse(any(method == 'DELETE' for method, _, _ in api.calls))
        self.assertLessEqual(len(downloads), 10)

    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'})
    def test_retry_rejects_changed_link_content(self):
        from contextlib import contextmanager
        from io import BytesIO
        from repo_mirror.gitlab_assets import Download
        src = TagSource()
        src.list_release_assets = lambda tag: [dict(id=5, name='file', url='https://example.com/file')]
        api = GitHubAPI(existing=False)
        dst = GitHubShapedDestination('team/project', 'token', client=api)
        verify = src.verify_release_tag
        calls, content = [], [b'old']
        def interrupted(*args):
            calls.append(1)
            if len(calls) > 1:
                raise ValueError('interrupted')
            return verify(*args)
        src.verify_release_tag = interrupted
        @contextmanager
        def download(url, cap):
            yield Download(False, BytesIO(content[0]), len(content[0]))
        with patch('repo_mirror.gitlab_assets.download_asset', download):
            with self.assertRaisesRegex(ValueError, 'interrupted'):
                mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src,
                                dst, 'unused', dst.target, 'marker', initializing=True)
            content[0] = b'new'
            src.verify_release_tag = verify
            with self.assertRaises(ReleaseRecoveryError):
                mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src,
                                dst, 'unused', dst.target, 'marker', initializing=True)
        self.assertIn('[repo-mirror:pending:v3]', api.release['body'])
        self.assertEqual(list(api.assets.values()), [('file', b'old')])
        self.assertFalse(any(method == 'DELETE' for method, _, _ in api.calls))

    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'})
    def test_retry_rejects_link_url_drift(self):
        from contextlib import contextmanager
        from io import BytesIO
        from repo_mirror.gitlab_assets import Download
        src = TagSource()
        link = dict(id=5, name='file', url='https://example.com/file', link_type='other')
        src.list_release_assets = lambda tag: [copy.deepcopy(link)]
        api = GitHubAPI(existing=False)
        dst = GitHubShapedDestination('team/project', 'token', client=api)
        verify = src.verify_release_tag
        calls = []
        def interrupted(*args):
            calls.append(1)
            if len(calls) > 1:
                raise ValueError('interrupted')
            return verify(*args)
        src.verify_release_tag = interrupted
        @contextmanager
        def download(url, cap):
            yield Download(False, BytesIO(b'ok'), 2)
        with patch('repo_mirror.gitlab_assets.download_asset', download):
            with self.assertRaisesRegex(ValueError, 'interrupted'):
                mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src,
                                dst, 'unused', dst.target, 'marker', initializing=True)
            link['url'] = 'https://example.com/changed'
            src.verify_release_tag = verify
            with self.assertRaises(ReleaseRecoveryError):
                mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src,
                                dst, 'unused', dst.target, 'marker', initializing=True)
        self.assertIn('[repo-mirror:pending:v3]', api.release['body'])
        self.assertEqual(list(api.assets.values()), [('file', b'ok')])

    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'})
    def test_invalid_tag_release_flags_refused_before_destination_write(self):
        src = TagSource()
        src.item['draft'] = 'false'
        api = GitHubAPI(existing=False)
        dst = GitHubShapedDestination('team/project', 'token', client=api)
        with self.assertRaises(ValueError):
            mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src,
                            dst, 'unused', dst.target, 'marker', initializing=True)
        self.assertFalse(any(method != 'GET' for method, _, _ in api.calls))

    def test_latest_uses_released_at_not_listing_order(self):
        from repo_mirror.repository import select_tag_source_latest
        old = dict(tag_name='old', released_at='2024-01-01T00:00:00Z')
        new = dict(tag_name='new', released_at='2025-01-01T00:00:00Z')
        self.assertEqual(select_tag_source_latest([old, new]), new)

    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'})
    def test_release_without_numeric_id_is_mirrored(self):
        source = TagSource()
        api = GitHubAPI(existing=False)
        dst = GitHubShapedDestination('team/project', 'token', client=api)
        mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', source,
                        dst, 'unused', dst.target, 'marker', initializing=True)
        self.assertEqual(api.release['tag_name'], 'v/1')
        self.assertEqual(api.release['body'], 'notes')


    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret', 'MIRROR_FORGEJO_ASSET_MAX_BYTES': '1000'})
    def test_review_regressions(self):
        from contextlib import contextmanager
        from io import BytesIO
        from repo_mirror.gitlab_assets import Download
        from repo_mirror.forgejo_destination_adapter import ForgejoDestinationAdapter
        from test_forgejo_space_body_integration import ForgejoAPI

        for cls, adapter in ((GitHubAPI, GitHubShapedDestination), (ForgejoAPI, ForgejoDestinationAdapter)):
            for version in ('v1', 'v2'):
                with self.subTest(destination=cls.__name__, case='legacy', version=version):
                    import base64, hashlib, json
                    src = TagSource(); api = cls(); api.release['tag_name'] = 'v/1'
                    digest = hashlib.sha256(b'[]').hexdigest()
                    encoded = base64.urlsafe_b64encode(json.dumps(['title', digest]).encode()).decode()
                    api.release['name'] = '[repo-mirror:pending:' + version + ']' + encoded
                    dst = adapter('team/project', 'token', client=api)
                    with self.assertRaises(ReleaseRecoveryError):
                        mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src, dst, 'unused', dst.target, 'marker', initializing=True)
                    self.assertFalse(any(m != 'GET' for m, _, _ in api.calls))
            for field in ('name', 'prerelease'):
                with self.subTest(destination=cls.__name__, case=field):
                    src = TagSource(); api = cls(existing=False)
                    dst = adapter('team/project', 'token', client=api)
                    request = dst.request
                    finalized = [False]
                    def tamper(method, url, body=None):
                        result = request(method, url, body)
                        if method == 'PATCH' and body and 'name' in body:
                            finalized[0] = True
                        elif method == 'GET' and finalized[0] and url.endswith('/releases/20'):
                            result[field] = 'wrong' if field == 'name' else True
                        return result
                    dst.request = tamper
                    with self.assertRaises(ReleaseRecoveryError):
                        mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src, dst, 'unused', dst.target, 'marker', initializing=True)
            with self.subTest(destination=cls.__name__, case='bytes drift'):
                src = TagSource(); src.list_release_assets = lambda tag: [dict(id=5,name='file',url='https://example.com/file')]
                api = cls(existing=False); dst = adapter('team/project','token',client=api)
                value = [b'old']; upload = api.upload
                def change(*a, **kw):
                    result = upload(*a, **kw); value[0] = b'new'; return result
                api.upload = change
                @contextmanager
                def download(url, cap):
                    yield Download(False, BytesIO(value[0]), len(value[0]))
                with patch('repo_mirror.gitlab_assets.download_asset', download):
                    with self.assertRaises(ReleaseRecoveryError):
                        mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src, dst, 'unused', dst.target, 'marker', initializing=True)
                self.assertIn('[repo-mirror:pending:v3]', api.release['body'])
            with self.subTest(destination=cls.__name__, case='replace interrupted'):
                src = TagSource(); src.list_release_assets = lambda tag: [dict(id=5,name='file',url='https://example.com/file')]
                api = cls(); api.release['tag_name'] = 'v/1'; api.assets = {201: ('file', b'old')}; api.fail_upload = True
                dst = adapter('team/project','token',client=api)
                @contextmanager
                def download(url, cap):
                    yield Download(False, BytesIO(b'new'), 3)
                with patch('repo_mirror.gitlab_assets.download_asset', download):
                    with self.assertRaisesRegex(RuntimeError, 'simulated interruption'):
                        mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src, dst, 'unused', dst.target, 'marker', initializing=True)
                    self.assertIn('[repo-mirror:pending:v3]', api.release['body'])
                    mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src, dst, 'unused', dst.target, 'marker', initializing=True)
                self.assertEqual(list(api.assets.values()), [('file', b'new')])

    @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'test-secret'})
    def test_forgejo_empty_body(self):
        from repo_mirror.forgejo_destination_adapter import ForgejoDestinationAdapter
        from test_forgejo_space_body_integration import ForgejoAPI
        src = TagSource(); src.item['body'] = ''
        api = ForgejoAPI(existing=False); dst = ForgejoDestinationAdapter('team/project', 'token', client=api)
        mirror_releases({'type': 'gitlab', 'owner': 'group'}, 'repo', 'team', src, dst, 'unused', dst.target, 'marker', initializing=True)
        self.assertEqual(api.release['body'], ' ')
