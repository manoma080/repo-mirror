import io
import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from repo_mirror import repository
from repo_mirror.journal import FailureJournal
from repo_mirror.repository import ReleaseAggregateError, mirror_repository, _IdentityGuardedDestination
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


def _make_release_fixture(*, source_releases=None, asset_payloads=None, initializing=True, ids=None,
                          on_source_list=None, on_release_patch=None, replacement_releases=None):
    """Build a GitHub-to-GitHub fixture.

    ``ids`` holds the live repository IDs (``source``/``destination``); a test
    may change them mid-run to simulate a replaced repository. While the
    destination ID differs from the pinned 202, release reads answer from the
    replacement repository (``replacement_releases``).
    """
    ids = ids if ids is not None else {'source': 101, 'destination': 202}
    source = {
        'type': 'github',
        'base_url': 'https://github.com',
        'owner': 'alice',
        'name': 'demo',
        'mirror': {'name': 'demo', 'source_id': 101, 'destination_id': 202},
    }
    marker = source_marker(source, 'demo')
    default_payloads = {
        'https://github.com/alice/demo/releases/download/v2/app-v2.tar.gz': b'v2-archive-bytes',
        'https://github.com/alice/demo/releases/download/v2/app-v2.sha256': b'v2-checksum-bytes',
        'https://github.com/alice/demo/releases/download/v1/app-v1.tar.gz': b'v1-archive-bytes',
        'https://github.com/alice/demo/releases/download/v1/app-v1.sha256': b'v1-checksum-bytes',
    }
    asset_payloads = default_payloads if asset_payloads is None else asset_payloads
    default_releases = [
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
                    'size': len(default_payloads['https://github.com/alice/demo/releases/download/v2/app-v2.tar.gz']),
                    'browser_download_url': 'https://github.com/alice/demo/releases/download/v2/app-v2.tar.gz',
                    'content_type': 'application/gzip',
                },
                {
                    'id': 202,
                    'name': 'app-v2.sha256',
                    'size': len(default_payloads['https://github.com/alice/demo/releases/download/v2/app-v2.sha256']),
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
                    'size': len(default_payloads['https://github.com/alice/demo/releases/download/v1/app-v1.tar.gz']),
                    'browser_download_url': 'https://github.com/alice/demo/releases/download/v1/app-v1.tar.gz',
                    'content_type': 'application/gzip',
                },
                {
                    'id': 102,
                    'name': 'app-v1.sha256',
                    'size': len(default_payloads['https://github.com/alice/demo/releases/download/v1/app-v1.sha256']),
                    'browser_download_url': 'https://github.com/alice/demo/releases/download/v1/app-v1.sha256',
                    'content_type': 'text/plain',
                },
            ],
        },
    ]
    source_releases = default_releases if source_releases is None else source_releases

    class FakeSource:
        def request(self, method, url, data=None, **kwargs):
            if url.endswith('/repos/alice/demo'):
                return {
                    'id': ids['source'],
                    'clone_url': 'https://github.com/alice/demo.git',
                    'default_branch': 'main',
                }
            if url.endswith('/releases/latest'):
                published = [r for r in source_releases if not r['draft'] and not r['prerelease']]
                if not published:
                    raise HTTPError(url, 404, 'not found', {}, None)
                return dict(published[0])
            if '/releases/' in url:
                rel_id = int(url.rsplit('/', 1)[1])
                for item in source_releases:
                    if item['id'] == rel_id:
                        return dict(item)
            raise HTTPError(url, 404, 'not found', {}, None)

        def pages(self, url):
            if url.endswith('/releases'):
                if on_source_list is not None:
                    on_source_list()
                return [dict(item) for item in source_releases]
            for item in source_releases:
                if url.endswith(f"/releases/{item['id']}/assets"):
                    return [dict(asset) for asset in item['assets']]
            return []

        def download(self, url, output, **kwargs):
            output.write(asset_payloads[url])

    class FakeDestination:
        def __init__(self):
            self.description = marker + (' [repo-mirror:initializing:v1]' if initializing else '')
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
                    'id': ids['destination'],
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
                if ids['destination'] != 202:
                    raise HTTPError(url, 404, 'not found', {}, None)
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
                if on_release_patch is not None:
                    on_release_patch(data)
                return dict(rel)
            if method == 'GET' and '/releases/' in url:
                rel_id = int(url.rsplit('/releases/', 1)[1])
                if rel_id in self.releases:
                    return dict(self.releases[rel_id])
                raise HTTPError(url, 404, 'not found', {}, None)
            return {}

        def pages(self, url):
            if url.endswith('/releases'):
                if ids['destination'] != 202:
                    return [dict(rel) for rel in (replacement_releases or [])]
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

    def test_unrecognised_request_methods_fail_closed(self):
        # Only a GET string skips the guard; everything else must be checked.
        cases = [
            ('HEAD', ('HEAD', '/releases'), {}),
            ('empty string', ('', '/releases'), {}),
            ('None positional', (None, '/releases'), {}),
            ('None keyword', (), {'method': None, 'url': '/releases'}),
            ('missing method', (), {'url': '/releases'}),
            ('no arguments', (), {}),
            ('int', (123, '/releases'), {}),
            ('bytes GET', (b'GET', '/releases'), {}),
            ('padded GET', (' GET', '/releases'), {}),
            ('unknown verb', ('UNKNOWN', '/releases'), {}),
            ('lowercase post', ('post', '/releases', {}), {}),
            ('mixed-case Patch', ('Patch', '/releases/1', {}), {}),
            ('mixed-case dElEtE', ('dElEtE', '/releases/assets/1'), {}),
            ('lowercase head', ('head', '/releases'), {}),
        ]
        for label, args, kwargs in cases:
            with self.subTest(method=label):
                dst = Mock()
                check = Mock(side_effect=ValueError('changed'))
                guarded = _IdentityGuardedDestination(dst, check)
                with self.assertRaisesRegex(ValueError, 'changed'):
                    guarded.request(*args, **kwargs)
                check.assert_called_once_with()
                dst.request.assert_not_called()

    def test_concluding_read_verifies_pins_after_the_read(self):
        from repo_mirror.repository import _concluding_read, _verify_transition_pins

        events = []
        dst = Mock()
        dst.request.side_effect = lambda *args: events.append('read') or {'id': 1}
        guarded = _IdentityGuardedDestination(dst, lambda: events.append('check'))
        self.assertEqual(_concluding_read(guarded, lambda: guarded.request('GET', '/releases/1')), {'id': 1})
        self.assertEqual(events, ['read', 'check'])

        # A pin mismatch wins over the read's own failure.
        failing = Mock()
        failing.request.side_effect = HTTPError('/releases/1', 404, 'not found', {}, None)
        guarded = _IdentityGuardedDestination(failing, Mock(side_effect=ValueError('pin changed')))
        with self.assertRaisesRegex(ValueError, 'pin changed'):
            _concluding_read(guarded, lambda: guarded.request('GET', '/releases/1'))

        # Unwrapped destinations (self-guarding adapters, mocks) are not touched.
        plain = Mock()
        _verify_transition_pins(plain)
        self.assertEqual(plain.mock_calls, [])

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

    def test_source_replaced_after_final_patch_blocks_journal_clearing(self):
        # Reviewer A reproduction (a): a published prerelease with a null body
        # (so latest_id stays None) on a non-initializing destination. The
        # source is replaced right after the final clearing PATCH; the
        # verification GET that authorises journal clearing must detect it.
        tag = 'v3-rc'
        asset_url = 'https://github.com/alice/demo/releases/download/v3-rc/app.tar.gz'
        payloads = {asset_url: b'rc-archive-bytes'}
        releases = [{
            'id': 30, 'tag_name': tag, 'name': 'Release candidate', 'body': None,
            'draft': False, 'prerelease': True, 'target_commitish': 'main',
            'published_at': '2026-03-01T00:00:00Z',
            'assets': [{'id': 301, 'name': 'app.tar.gz', 'size': len(payloads[asset_url]),
                        'browser_download_url': asset_url, 'content_type': 'application/gzip'}],
        }]
        ids = {'source': 101, 'destination': 202}
        final_patches = []

        def on_release_patch(data):
            if data.get('body') == '' and 'name' in data:
                final_patches.append(data)
                ids['source'] = 999

        source, _, src, dst = _make_release_fixture(
            source_releases=releases, asset_payloads=payloads, initializing=False, ids=ids,
            on_release_patch=on_release_patch)
        with tempfile.TemporaryDirectory() as tmp:
            journal = FailureJournal(os.path.join(tmp, 'failures.json'))
            key = FailureJournal.id_key('https://github.com', 101, 'https://api.github.com', 202, tag, 'app.tar.gz')
            journal.set(key, True)
            with (
                patch.dict(os.environ, {'MIRROR_TIMESTAMP_SECRET': 'fixture-secret'}),
                patch('repo_mirror.repository.mirror_git'),
            ):
                with self.assertRaisesRegex(ValueError, '^source repository id mismatch$'):
                    mirror_repository(source, 'demo', 'dest', src, dst, 'token', journal=journal)
            self.assertEqual(len(final_patches), 1)
            self.assertIn(key, journal.failures)

    def test_destination_replaced_after_last_recheck_reports_pin_mismatch(self):
        # Reviewer A reproduction (b): the destination is replaced right after
        # recheck_pins() #3 (at source release discovery) by a repository that
        # holds an unauthenticated untagged draft. The guarded concluding reads
        # must report the pin mismatch, as origin/main does, instead of
        # "ambiguous untagged destination draft".
        foreign = [{'id': 777, 'tag_name': 'untagged-0123abcd', 'name': 'foreign', 'body': 'foreign draft',
                    'draft': True, 'prerelease': False}]
        for initializing in (False, True):
            with self.subTest(initializing=initializing):
                ids = {'source': 101, 'destination': 202}
                source, _, src, dst = _make_release_fixture(
                    initializing=initializing, ids=ids, replacement_releases=foreign,
                    on_source_list=lambda: ids.update(destination=999))
                with (
                    patch.dict(os.environ, {'MIRROR_TIMESTAMP_SECRET': 'fixture-secret'}),
                    patch('repo_mirror.repository.mirror_git'),
                ):
                    with self.assertRaises(Exception) as raised:
                        mirror_repository(source, 'demo', 'dest', src, dst, 'token')
                exc = raised.exception
                if initializing:
                    # Per-release tag lookups (concluding reads) fail in each
                    # release's isolation boundary, as on origin/main.
                    self.assertIsInstance(exc, ReleaseAggregateError)
                    errors = exc.release_errors
                    self.assertEqual(len(errors), 2)
                else:
                    # The release snapshot (concluding read) fails before selection.
                    errors = (exc,)
                for error in errors:
                    self.assertIs(type(error), ValueError)
                    self.assertEqual(str(error), 'destination repository id mismatch or missing')
                self.assertFalse([m for m in dst.mutations if '/releases' in m[1]])

    def test_release_fixture_guard_accounting_equivalence_and_reduction(self):
        def run_with_guard(guard_cls):
            source, _, src, dst = _make_release_fixture()
            counts = {'guard_calls': 0, 'reads': 0, 'writes': 0, 'transition_checks': 0}

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

                def verify_pins(self):
                    before = counts['guard_calls']
                    try:
                        return super().verify_pins()
                    finally:
                        counts['transition_checks'] += 1
                        delta = counts['guard_calls'] - before
                        assert delta == 1, f'expected 1 guard call for verify_pins, got {delta}'

                if not hasattr(guard_cls, 'verify_pins'):
                    del verify_pins

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
        # Concluding reads on this fixture: one tag lookup per release (2)
        # and one destination release snapshot (1).
        self.assertEqual(baseline_counts['transition_checks'], 0)
        self.assertEqual(new_counts['transition_checks'], 3)
        self.assertEqual(new_counts['guard_calls'], new_counts['writes'] + new_counts['transition_checks'])
        self.assertLess(new_counts['guard_calls'], baseline_counts['guard_calls'])
