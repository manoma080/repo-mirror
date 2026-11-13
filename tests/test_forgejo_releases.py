import unittest

from repo_mirror.forgejo_releases import SyncConflict


class TestForgejoReleases(unittest.TestCase):
    def test_release_pagination_continues_after_short_pages(self):
        from repo_mirror.forgejo_releases import ForgejoReleases
        client = ForgejoReleases('owner/repo', 'secret')
        pages = [[{'id': i} for i in range(1, 51)], [{'id': 51}], []]
        calls = []
        def get(method, path):
            calls.append(path)
            return pages[len(calls) - 1]
        client._json = get
        self.assertEqual(len(client.list_releases()), 51)
        self.assertEqual(len(calls), 3)


    def test_release_pagination_rejects_duplicate_ids(self):
        from repo_mirror.forgejo_releases import ForgejoReleases
        client = ForgejoReleases('owner/repo', 'secret')
        client._json = lambda method, path: [{'id': 1}]
        with self.assertRaisesRegex(SyncConflict, 'duplicate'):
            client.list_releases()


    def test_release_pagination_is_bounded(self):
        from repo_mirror.forgejo_releases import ForgejoReleases
        client = ForgejoReleases('owner/repo', 'secret')
        count = iter(range(1, 10002))
        client._json = lambda method, path: [{'id': next(count)}]
        with self.assertRaisesRegex(SyncConflict, 'pagination limit'):
            client.list_releases()


    def test_forgejo_17_asset_listing_uses_single_unpaginated_request(self):
        # v17.0.0 ListReleaseAttachments loads all attachments and ignores page/limit.
        from repo_mirror.forgejo_releases import ForgejoReleases
        client = ForgejoReleases('owner/repo', 'secret')
        calls = []
        assets = [{'id': i} for i in range(1, 151)]
        def get(method, path):
            calls.append((method, path))
            return assets
        client._json = get
        self.assertEqual(client.list_assets({'id': 9}), assets)
        self.assertEqual(calls, [('GET', '/api/v1/repos/owner/repo/releases/9/assets')])


    def test_streaming_transfer(self):
        from repo_mirror.forgejo_releases import GitHubReleases
        class Response:
            status = 200
            headers = {'Content-Length': '6'}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size=-1):
                assert 0 < size <= 1024 * 1024
                result, self.data = self.data[:2], self.data[2:]
                return result
            data = b'abcdef'
        class Opener:
            def open(self, *args, **kwargs): return Response()
        import io
        client = GitHubReleases('owner/repo', None)
        client.api.opener = Opener()
        out = io.BytesIO()
        client.download({'id': 2, 'size': 6}, out)
        self.assertEqual(out.getvalue(), b'abcdef')


    def test_http_upload_is_iterable_and_bounded(self):
        import io
        from repo_mirror.forgejo_releases import ForgejoReleases, CHUNK_SIZE
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b'{}'
        class Opener:
            def open(self, request, timeout):
                self.chunks = list(request.data)
                self.length = int(request.get_header('Content-length'))
                return Response()
        class Stream(io.BytesIO):
            def read(self, size=-1):
                assert 0 < size <= CHUNK_SIZE
                return super().read(size)
        client = ForgejoReleases('owner/repo', 'secret')
        client.opener = Opener()
        payload = b'x' * (CHUNK_SIZE + 7)
        client.upload({'id': 1}, 'x', Stream(payload), len(payload))
        chunks = client.opener.chunks
        self.assertEqual(b''.join(chunks[1:-1]), payload)
        self.assertEqual(sum(map(len, chunks)), client.opener.length)


    def test_http_download_rejects_truncation_and_bad_length(self):
        import io
        from repo_mirror.forgejo_releases import _download
        class Response:
            status = 200
            def __init__(self, length):
                self.headers = {'Content-Length': length}
                self.stream = io.BytesIO(b'abc')
            def read(self, size): return self.stream.read(min(size, 2))
        for length in ('4', '3'):
            with self.subTest(length=length), self.assertRaises(SyncConflict):
                _download(Response(length), {'size': 4}, io.BytesIO())
