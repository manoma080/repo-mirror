import io
import os
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from repo_mirror import repository
from repo_mirror.repository import mirror_repository, _IdentityGuardedDestination
from repo_mirror.sources import source_marker


class _AllOpsGuardedDestination:
    """Reference pre-PR3 guard that checks pins before every operation."""

    def __init__(self, destination, check):
        self._destination = destination
        self._check = check

    def __getattr__(self, name):
        return getattr(self._destination, name)

    def request(self, *args, **kwargs):
        self._check()
        return self._destination.request(*args, **kwargs)

    def pages(self, url):
        self._check()
        return self._destination.pages(url)

    def download(self, *args, **kwargs):
        self._check()
        return self._destination.download(*args, **kwargs)

    def upload(self, *args, **kwargs):
        self._check()
        return self._destination.upload(*args, **kwargs)


def _make_release_fixture():
    source = {
        'type': 'github',
        'base_url': 'https://github.com',
        'owner': 'alice',
        'name': 'demo',
        'mirror': {'name': 'demo', 'source_id': 101, 'destination_id': 202},
    }
    marker = source_marker(source, 'demo')
    asset_payloads = {
        'https://github.com/alice/demo/releases/download/v2/app-v2.tar.gz': b'v2-archive-bytes',
        'https://github.com/alice/demo/releases/download/v2/app-v2.sha256': b'v2-checksum-bytes',
        'https://github.com/alice/demo/releases/download/v1/app-v1.tar.gz': b'v1-archive-bytes',
        'https://github.com/alice/demo/releases/download/v1/app-v1.sha256': b'v1-checksum-bytes',
    }
    source_releases = [
        {
            'id': 20,
            'tag_name': 'v2',
            'name': 'Release 2',
            'body': 'notes for v2',
            'draft': False,
            'prerelease': False,
            'target_commitish': 'main',
            'published_at': '2026-02-01T00:00:00Z',
            'assets': [
                {
                    'id': 201,
                    'name': 'app-v2.tar.gz',
                    'size': len(asset_payloads['https://github.com/alice/demo/releases/download/v2/app-v2.tar.gz']),
                    'browser_download_url': 'https://github.com/alice/demo/releases/download/v2/app-v2.tar.gz',
                    'content_type': 'application/gzip',
                },
                {
                    'id': 202,
                    'name': 'app-v2.sha256',
                    'size': len(asset_payloads['https://github.com/alice/demo/releases/download/v2/app-v2.sha256']),
                    'browser_download_url': 'https://github.com/alice/demo/releases/download/v2/app-v2.sha256',
                    'content_type': 'text/plain',
                },
            ],
        },
        {
            'id': 10,
            'tag_name': 'v1',
            'name': 'Release 1',
            'body': 'notes for v1',
            'draft': False,
            'prerelease': False,
            'target_commitish': 'main',
            'published_at': '2026-01-01T00:00:00Z',
            'assets': [
                {
                    'id': 101,
                    'name': 'app-v1.tar.gz',
                    'size': len(asset_payloads['https://github.com/alice/demo/releases/download/v1/app-v1.tar.gz']),
                    'browser_download_url': 'https://github.com/alice/demo/releases/download/v1/app-v1.tar.gz',
                    'content_type': 'application/gzip',
                },
                {
                    'id': 102,
                    'name': 'app-v1.sha256',
                    'size': len(asset_payloads['https://github.com/alice/demo/releases/download/v1/app-v1.sha256']),
                    'browser_download_url': 'https://github.com/alice/demo/releases/download/v1/app-v1.sha256',
                    'content_type': 'text/plain',
                },
            ],
        },
    ]

    class FakeSource:
        def request(self, method, url, data=None, **kwargs):
            if url.endswith('/repos/alice/demo'):
                return {
                    'id': 101,
                    'clone_url': 'https://github.com/alice/demo.git',
                    'default_branch': 'main',
                }
            if url.endswith('/releases/latest'):
                return dict(source_releases[0])
            if '/releases/' in url:
                rel_id = int(url.rsplit('/', 1)[1])
                for item in source_releases:
                    if item['id'] == rel_id:
                        return dict(item)
            raise HTTPError(url, 404, 'not found', {}, None)

        def pages(self, url):
            if url.endswith('/releases'):
                return [dict(item) for item in source_releases]
            for item in source_releases:
                if url.endswith(f"/releases/{item['id']}/assets"):
                    return [dict(asset) for asset in item['assets']]
            return []

        def download(self, url, output, **kwargs):
            output.write(asset_payloads[url])

    class FakeDestination:
        def __init__(self):
            self.description = marker + ' [repo-mirror:initializing:v1]'
            self.releases = {}
            self.assets = {}
            self.next_release_id = 500
            self.next_asset_id = 900
            self.latest_id = None
            self.mutations = []

        def request(self, method, url, data=None, **kwargs):
            if method != 'GET':
                self.mutations.append((method, url, data))
            if method == 'GET' and url.endswith('/repos/dest/demo'):
                return {
                    'id': 202,
                    'description': self.description,
                    'default_branch': 'main',
                }
            if method == 'PATCH' and url.endswith('/repos/dest/demo'):
                if isinstance(data, dict) and 'description' in data:
                    self.description = data['description']
                return {'id': 202, 'description': self.description, 'default_branch': 'main'}
            if method == 'PUT' and url.endswith('/actions/permissions'):
                return {}
            if method == 'GET' and url.endswith('/releases/latest'):
                if self.latest_id is None:
                    raise HTTPError(url, 404, 'not found', {}, None)
                return dict(self.releases[self.latest_id])
            if method == 'GET' and '/releases/tags/' in url:
                tag = url.rsplit('/releases/tags/', 1)[1]
                for rel in self.releases.values():
                    if rel.get('tag_name') == tag:
                        return dict(rel)
                raise HTTPError(url, 404, 'not found', {}, None)
            if method == 'POST' and url.endswith('/releases'):
                rel_id = self.next_release_id
                self.next_release_id += 1
                rel = {
                    'id': rel_id,
                    'tag_name': data.get('tag_name'),
                    'name': data.get('name'),
                    'body': data.get('body'),
                    'draft': bool(data.get('draft', False)),
                    'prerelease': bool(data.get('prerelease', False)),
                    'target_commitish': data.get('target_commitish'),
                    'upload_url': f'https://uploads.github.com/repos/dest/demo/releases/{rel_id}/assets{{?name,label}}',
                }
                self.releases[rel_id] = rel
                self.assets[rel_id] = []
                if not rel['draft'] and not rel['prerelease']:
                    self.latest_id = rel_id
                return dict(rel)
            if method == 'PATCH' and '/releases/' in url:
                rel_id = int(url.rsplit('/releases/', 1)[1])
                rel = self.releases[rel_id]
                for key in ('tag_name', 'name', 'body', 'draft', 'prerelease', 'target_commitish'):
                    if key in data:
                        rel[key] = data[key]
                if data.get('make_latest') == 'true':
                    self.latest_id = rel_id
                elif not rel['draft'] and not rel['prerelease'] and self.latest_id is None:
                    self.latest_id = rel_id
                return dict(rel)
            if method == 'GET' and '/releases/' in url:
                rel_id = int(url.rsplit('/releases/', 1)[1])
                if rel_id in self.releases:
                    return dict(self.releases[rel_id])
                raise HTTPError(url, 404, 'not found', {}, None)
            return {}

        def pages(self, url):
            if url.endswith('/releases'):
                return [dict(rel) for rel in self.releases.values()]
            if '/releases/' in url and url.endswith('/assets'):
                rel_id = int(url.rsplit('/releases/', 1)[1].split('/', 1)[0])
                return [dict(asset) for asset in self.assets.get(rel_id, [])]
            return []

        def upload(self, url, data, content_type):
            parsed = urlparse(url)
            name = parse_qs(parsed.query)['name'][0]
            rel_id = int(parsed.path.split('/releases/', 1)[1].split('/', 1)[0])
            payload = data.read()
            self.mutations.append(('UPLOAD', url, name, content_type, payload))
            asset_id = self.next_asset_id
            self.next_asset_id += 1
            self.assets[rel_id].append({
                'id': asset_id,
                'name': name,
                'size': len(payload),
                'state': 'uploaded',
                'url': f'https://api.github.com/repos/dest/demo/releases/assets/{asset_id}',
                'payload': payload,
            })

        def download(self, url, output):
            asset_id = int(url.rsplit('/', 1)[1])
            for items in self.assets.values():
                for asset in items:
                    if asset['id'] == asset_id:
                        output.write(asset['payload'])
                        return
            raise HTTPError(url, 404, 'not found', {}, None)

    return source, marker, FakeSource(), FakeDestination()


class ReleaseIdentityGuardTests(unittest.TestCase):
    def test_each_destination_operation_checks_before_delegating(self):
        for method, args in [
            ('request', ('POST', '/releases', {})),
            ('request', ('PATCH', '/releases/1', {})),
            ('request', ('PUT', '/actions/permissions', {})),
            ('request', ('DELETE', '/asset')),
            ('upload', ('/upload', object(), 'x')),
        ]:
            with self.subTest(write_method=method, args=args):
                dst = Mock()
                check = Mock(side_effect=ValueError('changed'))
                guarded = _IdentityGuardedDestination(dst, check)
                with self.assertRaisesRegex(ValueError, 'changed'):
                    getattr(guarded, method)(*args)
                getattr(dst, method).assert_not_called()

        for method, args in [
            ('request', ('GET', '/releases')),
            ('request', ('get', '/releases')),
            ('pages', ('/releases',)),
            ('download', ('/asset', object())),
        ]:
            with self.subTest(read_method=method, args=args):
                dst = Mock()
                check = Mock(side_effect=ValueError('changed'))
                guarded = _IdentityGuardedDestination(dst, check)
                getattr(guarded, method)(*args)
                check.assert_not_called()
                getattr(dst, method).assert_called_once_with(*args)

    def test_write_only_guard_call_accounting_on_reads_and_writes(self):
        dst = Mock()
        check = Mock()
        guarded = _IdentityGuardedDestination(dst, check)

        guarded.request('GET', '/releases')
        guarded.request('get', '/releases/1')
        guarded.request(method='GET', url='/releases/latest')
        guarded.pages('/releases')
        guarded.download('/asset', io.BytesIO())
        self.assertEqual(check.call_count, 0)

        guarded.request('POST', '/releases', {'tag_name': 'v1'})
        guarded.request('PATCH', '/releases/1', {'body': 'notes'})
        guarded.request('PUT', '/actions/permissions', {'enabled': False})
        guarded.request('DELETE', '/releases/assets/1')
        guarded.request(method='PATCH', url='/releases/1', data={'draft': False})
        guarded.upload('/upload', io.BytesIO(b'x'), 'application/octet-stream')
        self.assertEqual(check.call_count, 6)

    def test_initialization_marker_is_not_patched_after_source_replacement(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice',
                  'name': 'repo', 'mirror': {'name': 'repo', 'source_id': 1, 'destination_id': 2}}
        marker = source_marker(source, 'repo')
        state = {'id': 1}
        src, dst = Mock(), Mock()
        src.request.side_effect = lambda *args: {'id': state['id'], 'clone_url': 'https://github.com/alice/repo.git', 'default_branch': 'main'}
        dst.request.return_value = {'id': 2, 'description': marker + ' [repo-mirror:initializing:v1]', 'default_branch': 'main'}
        def releases(*args, **kwargs):
            state['id'] = 3
        with patch('repo_mirror.repository.mirror_git'), patch('repo_mirror.repository.mirror_releases', releases):
            with self.assertRaisesRegex(ValueError, 'source repository id mismatch'):
                mirror_repository(source, 'repo', 'dest', src, dst, 'token')
        self.assertFalse(any(c.args[0] == 'PATCH' and c.args[2].get('description') == marker
                             for c in dst.request.call_args_list))

    def test_pin_mismatch_blocks_upload_and_patch_with_same_value_error(self):
        for write_kind in ('upload', 'patch'):
            with self.subTest(write_kind=write_kind):
                source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice',
                          'name': 'repo', 'mirror': {'name': 'repo', 'source_id': 1, 'destination_id': 2}}
                marker = source_marker(source, 'repo')
                src = Mock()
                src.request.return_value = {'id': 1, 'clone_url': 'https://github.com/alice/repo.git', 'default_branch': 'main'}
                dst = Mock()
                state = {'id': 2}

                def request(method, url, *args):
                    if method == 'GET' and url.endswith('/repos/dest/repo'):
                        return {'id': state['id'], 'description': marker, 'default_branch': 'main'}
                    return {}

                dst.request.side_effect = request

                def releases(source, name, owner, src, guarded, upstream, target, marker, **kwargs):
                    state['id'] = 3
                    if write_kind == 'upload':
                        guarded.upload('https://uploads.github.com/asset?name=a.bin', io.BytesIO(b'x'), 'application/octet-stream')
                    else:
                        guarded.request('PATCH', target + '/releases/1', {'body': 'final'})

                with patch('repo_mirror.repository.mirror_git'), patch('repo_mirror.repository.mirror_releases', releases):
                    with self.assertRaisesRegex(ValueError, 'destination repository id mismatch or missing'):
                        mirror_repository(source, 'repo', 'dest', src, dst, 'token')
                if write_kind == 'upload':
                    dst.upload.assert_not_called()
                else:
                    self.assertFalse(any(c.args[0] == 'PATCH' and c.args[1].endswith('/releases/1')
                                         for c in dst.request.call_args_list))

    def test_destination_change_allows_reads_and_blocks_next_write(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice',
                  'name': 'repo', 'mirror': {'name': 'repo', 'source_id': 1, 'destination_id': 2}}
        marker = source_marker(source, 'repo')
        src = Mock()
        src.request.return_value = {'id': 1, 'clone_url': 'https://github.com/alice/repo.git', 'default_branch': 'main'}
        dst = Mock()
        dst.pages.return_value = []
        state = {'id': 2}
        def request(method, url, *args):
            if method == 'GET' and url.endswith('/repos/dest/repo'):
                return {'id': state['id'], 'description': marker, 'default_branch': 'main'}
            return {}
        dst.request.side_effect = request
        reads_completed = []

        def releases(source, name, owner, src, guarded, upstream, target, marker, **kwargs):
            guarded.request('GET', target + '/releases/1')
            state['id'] = 3
            guarded.request('GET', target + '/releases/2')
            reads_completed.append('get')
            guarded.pages(target + '/releases')
            reads_completed.append('pages')
            guarded.download(target + '/releases/assets/1', io.BytesIO())
            reads_completed.append('download')
            guarded.request('PATCH', target + '/releases/2', {'body': 'updated'})

        with patch('repo_mirror.repository.mirror_git'), patch('repo_mirror.repository.mirror_releases', releases):
            with self.assertRaisesRegex(ValueError, 'destination repository id mismatch or missing'):
                mirror_repository(source, 'repo', 'dest', src, dst, 'token')
        self.assertEqual(reads_completed, ['get', 'pages', 'download'])
        self.assertTrue(any(c.args[0] == 'GET' and c.args[1].endswith('/releases/2') for c in dst.request.call_args_list))
        self.assertFalse(any(c.args[0] == 'PATCH' and c.args[1].endswith('/releases/2') for c in dst.request.call_args_list))

    def test_release_fixture_guard_accounting_equivalence_and_reduction(self):
        def run_with_guard(guard_cls):
            source, _, src, dst = _make_release_fixture()
            counts = {'guard_calls': 0, 'reads': 0, 'writes': 0}

            class CountingGuard(guard_cls):
                def __init__(self, destination, check):
                    def counted_check():
                        counts['guard_calls'] += 1
                        return check()
                    super().__init__(destination, counted_check)

                def request(self, *args, **kwargs):
                    method = args[0] if args else kwargs.get('method')
                    before = counts['guard_calls']
                    try:
                        return super().request(*args, **kwargs)
                    finally:
                        delta = counts['guard_calls'] - before
                        if isinstance(method, str) and method.upper() == 'GET':
                            counts['reads'] += 1
                            if guard_cls is _IdentityGuardedDestination:
                                assert delta == 0, f'expected 0 guard calls for GET, got {delta}'
                        else:
                            counts['writes'] += 1
                            assert delta == 1, f'expected 1 guard call for {method}, got {delta}'

                def pages(self, url):
                    before = counts['guard_calls']
                    try:
                        return super().pages(url)
                    finally:
                        delta = counts['guard_calls'] - before
                        counts['reads'] += 1
                        if guard_cls is _IdentityGuardedDestination:
                            assert delta == 0, f'expected 0 guard calls for pages, got {delta}'

                def download(self, *args, **kwargs):
                    before = counts['guard_calls']
                    try:
                        return super().download(*args, **kwargs)
                    finally:
                        delta = counts['guard_calls'] - before
                        counts['reads'] += 1
                        if guard_cls is _IdentityGuardedDestination:
                            assert delta == 0, f'expected 0 guard calls for download, got {delta}'

                def upload(self, *args, **kwargs):
                    before = counts['guard_calls']
                    try:
                        return super().upload(*args, **kwargs)
                    finally:
                        delta = counts['guard_calls'] - before
                        counts['writes'] += 1
                        assert delta == 1, f'expected 1 guard call for upload, got {delta}'

            with (
                patch.dict(os.environ, {'MIRROR_TIMESTAMP_SECRET': 'fixture-secret'}),
                patch('repo_mirror.repository.mirror_git'),
                patch.object(repository, '_IdentityGuardedDestination', CountingGuard),
            ):
                mirror_repository(source, 'demo', 'dest', src, dst, 'token')
            return dst, counts

        baseline_dst, baseline_counts = run_with_guard(_AllOpsGuardedDestination)
        new_dst, new_counts = run_with_guard(_IdentityGuardedDestination)

        self.assertEqual(new_dst.mutations, baseline_dst.mutations)
        self.assertEqual(new_dst.releases, baseline_dst.releases)
        self.assertEqual(new_dst.assets, baseline_dst.assets)
        self.assertEqual(new_dst.description, baseline_dst.description)
        self.assertEqual(new_dst.latest_id, baseline_dst.latest_id)
        self.assertEqual(new_counts['reads'], baseline_counts['reads'])
        self.assertEqual(new_counts['writes'], baseline_counts['writes'])
        self.assertGreater(new_counts['reads'], 0)
        self.assertGreater(new_counts['writes'], 0)
        self.assertEqual(baseline_counts['guard_calls'], baseline_counts['reads'] + baseline_counts['writes'])
        self.assertEqual(new_counts['guard_calls'], new_counts['writes'])
        self.assertLess(new_counts['guard_calls'], baseline_counts['guard_calls'])
