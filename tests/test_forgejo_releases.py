import hashlib

import unittest

from repo_mirror.forgejo_releases import SyncConflict, sync_releases


class Fake:
    def __init__(self, releases, assets, blobs):
        self.releases = releases
        self.assets = assets
        self.blobs = blobs

    def list_releases(self):
        return list(self.releases)

    def list_assets(self, release):
        return list(self.assets.get(release['id'], []))

    def download(self, asset, output):
        output.write(self.blobs[asset['id']])

    def create_release(self, release):
        obj = dict(release, id=10)
        self.releases.append(obj)
        return obj

    def upload(self, release, name, data, size):
        data = data.read()
        asset = {'id': 20, 'name': name, 'size': len(data)}
        self.assets.setdefault(release['id'], []).append(asset)
        self.blobs[20] = data
        return asset


class TestForgejoReleases(unittest.TestCase):
    def test_blank_name_normalized_on_create_and_existing_release(self):
        rel = {'id': 1, 'tag_name': 'v1', 'name': '', 'body': 'notes', 'draft': False, 'prerelease': False, 'target_commitish': 'main'}
        asset = {'id': 2, 'name': 'x', 'size': 3, 'state': 'uploaded'}
        class NormalizingDestination(Fake):
            def create_release(self, release):
                self.sent = dict(release)
                return super().create_release(dict(release, name=release['tag_name']))
        src = Fake([rel], {1: [asset]}, {2: b'abc'})
        dst = NormalizingDestination([], {}, {})
        sync_releases(src, dst)
        self.assertEqual(dst.sent['name'], '')
        sync_releases(src, dst)
        self.assertEqual(dst.releases[0]['body'], 'notes')
        self.assertEqual(dst.blobs[20], b'abc')
        self.assertEqual(len(dst.assets[10]), 1)

    def test_blank_name_requires_blank_or_tag_at_every_destination_gate(self):
        rel = {'id': 1, 'tag_name': 'v1', 'name': '', 'body': 'notes', 'draft': False, 'prerelease': False, 'target_commitish': 'main'}
        class Destination(Fake):
            def __init__(self, name, change_at=None):
                super().__init__([dict(rel, id=10, name=name)], {}, {})
                self.change_at = change_at
                self.calls = 0
            def list_releases(self):
                self.calls += 1
                if self.calls == self.change_at:
                    self.releases[0]['name'] = 'other'
                return super().list_releases()
        for name in ('', 'v1'):
            with self.subTest(name=name):
                sync_releases(Fake([dict(rel)], {}, {}), Destination(name))
        for name, change_at, message in [('other', None, 'metadata collision'),
                                          ('v1', 2, 'release verification'),
                                          ('v1', 3, 'release verification')]:
            with self.subTest(name=name, change_at=change_at):
                dst = Destination(name, change_at)
                with self.assertRaisesRegex(SyncConflict, message):
                    sync_releases(Fake([dict(rel)], {}, {}), dst)
                self.assertEqual(dst.releases[0]['body'], 'notes')
        for source_name, target_name in [('title', 'v1'), (' ', 'v1'), ('title', '')]:
            with self.subTest(source_name=source_name, target_name=target_name):
                with self.assertRaisesRegex(SyncConflict, 'metadata collision'):
                    sync_releases(Fake([dict(rel, name=source_name)], {}, {}), Destination(target_name))

    def test_release_added_during_transfer_aborts(self):
        rel = {'id': 1, 'tag_name': 'v1', 'name': 'n', 'body': '', 'draft': False, 'prerelease': False, 'target_commitish': 'main'}
        class Source(Fake):
            def download(self, asset, output):
                super().download(asset, output)
                self.releases.append(dict(rel, id=3, tag_name='v2'))
        src = Source([rel], {1: [{'id': 2, 'name': 'x', 'size': 3, 'state': 'uploaded'}]}, {2: b'abc'})
        with self.assertRaisesRegex(SyncConflict, 'source release'):
            sync_releases(src, Fake([], {}, {}))

    def test_final_source_snapshot_detects_deletion_and_stable_changes(self):
        rel = {'id': 1, 'tag_name': 'v1', 'name': 'n', 'body': '', 'draft': False, 'prerelease': False, 'target_commitish': 'main'}
        class Source(Fake):
            def __init__(self, releases, change):
                super().__init__(releases, {}, {})
                self.change = change
            def list_releases(self):
                self.calls = getattr(self, 'calls', 0) + 1
                if self.calls == 3:
                    self.change(self.releases)
                return super().list_releases()
        for change in (lambda releases: releases.clear(),
                       lambda releases: releases[0].update(id=3),
                       lambda releases: releases[0].update(tag_name='v2'),
                       lambda releases: releases[0].update(body='changed')):
            with self.subTest(change=change):
                src = Source([dict(rel)], change)
                with self.assertRaisesRegex(SyncConflict, 'source release'):
                    sync_releases(src, Fake([], {}, {}))

    def test_volatile_release_metadata_churn_is_ignored(self):
        rel = {'id': 1, 'tag_name': 'v1', 'name': 'n', 'body': '', 'draft': False, 'prerelease': False, 'target_commitish': 'main'}
        class Source(Fake):
            def list_releases(self):
                self.calls = getattr(self, 'calls', 0) + 1
                return [dict(r, updated_at=str(self.calls), url=str(self.calls)) for r in self.releases]
        sync_releases(Source([rel], {}, {}), Fake([], {}, {}))

    def test_final_destination_snapshot_rejects_late_drift(self):
        rel = {'id': 1, 'tag_name': 'v1', 'name': 'n', 'body': '', 'draft': False, 'prerelease': False, 'target_commitish': 'main'}
        class Destination(Fake):
            def __init__(self, change):
                super().__init__([], {}, {})
                self.change = change
            def list_releases(self):
                self.calls = getattr(self, 'calls', 0) + 1
                if self.calls == 3:
                    self.change(self.releases)
                return super().list_releases()
        for change in (lambda releases: releases.clear(),
                       lambda releases: releases[0].update(id=11),
                       lambda releases: releases[0].update(body='changed')):
            with self.subTest(change=change):
                dst = Destination(change)
                with self.assertRaisesRegex(SyncConflict, 'release verification'):
                    sync_releases(Fake([dict(rel)], {}, {}), dst)

    def test_release_added_to_initially_empty_source_aborts(self):
        rel = {'id': 1, 'tag_name': 'v1', 'name': 'n', 'body': '', 'draft': False, 'prerelease': False, 'target_commitish': 'main'}
        class Source(Fake):
            def list_releases(self):
                self.calls = getattr(self, 'calls', 0) + 1
                return [] if self.calls == 1 else [rel]
        with self.assertRaisesRegex(SyncConflict, 'source release'):
            sync_releases(Source([], {}, {}), Fake([], {}, {}))

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

    def test_copy_and_repeat_including_literal_manifest(self):
        data = b'{"not": "mirror metadata"}'
        rel = {'id': 1, 'tag_name': 'v1', 'name': 'n', 'body': 'body', 'draft': False, 'prerelease': True, 'target_commitish': 'main'}
        asset = {'id': 2, 'name': 'REPO-MIRROR-MANIFEST.json', 'size': len(data), 'state': 'uploaded', 'digest': 'sha256:' + hashlib.sha256(data).hexdigest()}
        src = Fake([rel], {1: [asset]}, {2: data})
        dst = Fake([], {}, {})
        sync_releases(src, dst)
        sync_releases(src, dst)
        self.assertEqual(len(dst.releases), 1)
        self.assertIs(dst.releases[0]['prerelease'], True)
        self.assertEqual(dst.blobs[20], data)
        self.assertEqual(len(dst.assets[10]), 1)


    def test_large_metadata_and_streaming_transfer(self):
        from repo_mirror.forgejo_releases import _assets, GitHubReleases
        _assets([{'id': 2, 'name': 'large', 'size': 2 * 1024**3, 'state': 'uploaded'}], source=True)
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

    def test_truncated_source_never_uploads(self):
        rel = {'id': 1, 'tag_name': 'v1', 'name': 'n', 'body': '', 'draft': False, 'prerelease': False, 'target_commitish': 'main'}
        src = Fake([rel], {1: [{'id': 2, 'name': 'x', 'size': 4, 'state': 'uploaded'}]}, {2: b'abc'})
        dst = Fake([], {}, {})
        with self.assertRaises(SyncConflict):
            sync_releases(src, dst)
        self.assertFalse(dst.assets)

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

    def test_failed_upload_does_not_retry_or_delete_partial_asset(self):
        rel = {'id': 1, 'tag_name': 'v1', 'name': 'n', 'body': '', 'draft': False, 'prerelease': False, 'target_commitish': 'main'}
        src = Fake([rel], {1: [{'id': 2, 'name': 'x', 'size': 3, 'state': 'uploaded'}]}, {2: b'abc'})
        class Partial(Fake):
            def upload(self, release, name, data, size):
                self.assets[release['id']] = [{'id': 20, 'name': name, 'size': 1}]
                self.blobs[20] = b'a'
                raise SyncConflict('upload failed')
        dst = Partial([], {}, {})
        with self.assertRaises(SyncConflict): sync_releases(src, dst)
        with self.assertRaises(SyncConflict): sync_releases(src, dst)
        self.assertEqual(dst.blobs[20], b'a')

    def test_asset_collision_never_overwrites(self):
        rel = {'id': 1, 'tag_name': 'v1', 'name': 'n', 'body': '', 'draft': False, 'prerelease': False, 'target_commitish': 'main'}
        src = Fake([rel], {1: [{'id': 2, 'name': 'x', 'size': 3, 'state': 'uploaded'}]}, {2: b'abc'})
        dstrel = dict(rel, id=10)
        dst = Fake([dstrel], {10: [{'id': 20, 'name': 'x', 'size': 3}]}, {20: b'bad'})
        with self.assertRaises(SyncConflict):
            sync_releases(src, dst)
        self.assertEqual(dst.blobs[20], b'bad')


    def test_new_asset_race_cannot_upload_duplicate(self):
        rel = {'id': 1, 'tag_name': 'v1', 'name': 'n', 'body': '', 'draft': False, 'prerelease': False, 'target_commitish': 'main'}
        src = Fake([rel], {1: [{'id': 2, 'name': 'x', 'size': 3, 'state': 'uploaded'}]}, {2: b'abc'})
        class Racing(Fake):
            def list_assets(self, release):
                self.calls = getattr(self, 'calls', 0) + 1
                if self.calls == 2:
                    return [{'id': 21, 'name': 'x', 'size': 3}]
                return super().list_assets(release)
        dst = Racing([dict(rel, id=10)], {}, {21: b'bad'})
        with self.assertRaises(SyncConflict):
            sync_releases(src, dst)
        self.assertFalse(dst.assets)


    def test_final_asset_gate_detects_early_release_drift(self):
        rel = {'id': 1, 'tag_name': 'v1', 'name': 'n', 'body': '', 'draft': False, 'prerelease': False, 'target_commitish': 'main'}
        releases = [rel, dict(rel, id=3, tag_name='v2')]
        class Source(Fake):
            def download(self, asset, output):
                super().download(asset, output)
                if asset['id'] == 4:
                    self.change(self.assets[1])
        changes = [lambda assets: assets.append({'id': 5, 'name': 'new', 'size': 0, 'state': 'uploaded'}),
                   lambda assets: assets.clear()]
        for field, value in [('id', 9), ('name', 'other'), ('size', 4),
                             ('state', 'pending'), ('digest', 'sha256:' + '0' * 64)]:
            changes.append(lambda assets, field=field, value=value: assets[0].update({field: value}))
        for change in changes:
            with self.subTest(change=change):
                src = Source([dict(r) for r in releases], {1: [{'id': 2, 'name': 'x', 'size': 3, 'state': 'uploaded'}], 3: [{'id': 4, 'name': 'y', 'size': 3, 'state': 'uploaded'}]}, {2: b'abc', 4: b'def'})
                src.change = change
                dst = Fake([dict(rel, id=10), dict(rel, id=11, tag_name='v2')], {}, {})
                with self.assertRaises(SyncConflict):
                    sync_releases(src, dst)

    def test_final_asset_gate_detects_destination_drift(self):
        rel = {'id': 1, 'tag_name': 'v1', 'name': 'n', 'body': '', 'draft': False, 'prerelease': False, 'target_commitish': 'main'}
        class Source(Fake):
            def download(self, asset, output):
                super().download(asset, output)
                if asset['id'] == 4:
                    self.change(dst.assets[10])
        for change in (lambda assets: assets.clear(), lambda assets: assets[0].update(id=21),
                       lambda assets: assets[0].update(size=4)):
            with self.subTest(change=change):
                src = Source([dict(rel), dict(rel, id=3, tag_name='v2')], {1: [{'id': 2, 'name': 'x', 'size': 3, 'state': 'uploaded'}], 3: [{'id': 4, 'name': 'y', 'size': 3, 'state': 'uploaded'}]}, {2: b'abc', 4: b'def'})
                src.change = change
                dst = Fake([dict(rel, id=10), dict(rel, id=11, tag_name='v2')], {}, {})
                with self.assertRaises(SyncConflict):
                    sync_releases(src, dst)

    def test_source_volatile_asset_metadata_changes_do_not_abort(self):
        rel = {'id': 1, 'tag_name': 'v1', 'name': 'n', 'body': '', 'draft': False, 'prerelease': False, 'target_commitish': 'main'}
        class Source(Fake):
            def list_assets(self, release):
                self.calls = getattr(self, 'calls', 0) + 1
                return [dict(a, updated_at=str(self.calls), url=str(self.calls),
                             uploader={'login': str(self.calls)}, extra=self.calls)
                        for a in super().list_assets(release)]
        src = Source([rel], {1: [{'id': 2, 'name': 'x', 'size': 3, 'state': 'uploaded'}]}, {2: b'abc'})
        dst = Fake([], {}, {})
        sync_releases(src, dst)
        self.assertEqual(dst.blobs[20], b'abc')

    def test_stable_source_asset_changes_abort(self):
        rel = {'id': 1, 'tag_name': 'v1', 'name': 'n', 'body': '', 'draft': False, 'prerelease': False, 'target_commitish': 'main'}
        class Source(Fake):
            def __init__(self, releases, assets, blobs, change):
                super().__init__(releases, assets, blobs)
                self.change = change
            def list_assets(self, release):
                self.calls = getattr(self, 'calls', 0) + 1
                return [dict(a, **(self.change if self.calls > 1 else {}))
                        for a in super().list_assets(release)]
        for field, value in [('name', 'y'), ('id', 3), ('size', 4),
                             ('state', 'pending'), ('digest', 'sha256:' + '0' * 64)]:
            with self.subTest(field=field):
                src = Source([rel], {1: [{'id': 2, 'name': 'x', 'size': 3, 'state': 'uploaded'}]}, {2: b'abc'}, {field: value})
                with self.assertRaises(SyncConflict):
                    sync_releases(src, Fake([], {}, {}))

    def test_no_digest_content_change_is_detected_on_repeat(self):
        rel = {'id': 1, 'tag_name': 'v1', 'name': 'n', 'body': '', 'draft': False, 'prerelease': False, 'target_commitish': 'main'}
        src = Fake([rel], {1: [{'id': 2, 'name': 'x', 'size': 3, 'state': 'uploaded'}]}, {2: b'abc'})
        dst = Fake([], {}, {})
        sync_releases(src, dst)
        src.blobs[2] = b'def'
        with self.assertRaisesRegex(SyncConflict, 'asset collision'):
            sync_releases(src, dst)
        self.assertEqual(dst.blobs[20], b'abc')

    def test_source_download_count_changes_do_not_abort(self):
        rel = {'id': 1, 'tag_name': 'v1', 'name': 'n', 'body': '', 'draft': False, 'prerelease': False, 'target_commitish': 'main'}
        class Source(Fake):
            def download(self, asset, output):
                self.assets[1][0]['download_count'] += 1
                return super().download(asset, output)
            def list_assets(self, release):
                return [dict(a) for a in super().list_assets(release)]
        src = Source([rel], {1: [{'id': 2, 'name': 'x', 'size': 3, 'state': 'uploaded', 'download_count': 0}]}, {2: b'abc'})
        sync_releases(src, Fake([], {}, {}))


    def test_source_release_metadata_change_during_transfer_fails(self):
        rel = {'id': 1, 'tag_name': 'v1', 'name': 'n', 'body': 'old', 'draft': False, 'prerelease': False, 'target_commitish': 'main'}
        class Source(Fake):
            def list_releases(self):
                return [dict(release) for release in self.releases]
            def download(self, asset, output):
                super().download(asset, output)
                self.releases[0]['body'] = 'new'
        src = Source([rel], {1: [{'id': 2, 'name': 'x', 'size': 3, 'state': 'uploaded'}]}, {2: b'abc'})
        with self.assertRaises(SyncConflict):
            sync_releases(src, Fake([], {}, {}))
