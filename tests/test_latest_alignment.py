import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import mirror


class Source:
    def __init__(self, releases, latest):
        self.releases, self.latest = releases, latest

    def request(self, method, url, data=None):
        if url.endswith('/releases/latest'):
            if isinstance(self.latest, Exception):
                raise self.latest
            return self.latest
        return {'clone_url': 'https://github.com/alice/demo.git'}

    def pages(self, url):
        return self.releases if url.endswith('/releases') else []


class Destination:
    def __init__(self, initializing):
        self.initializing = initializing
        self.releases = {}
        self.posts = []
        self.patches = []

    def request(self, method, url, data=None):
        if url.endswith('/repos/dest/mirror-alice-demo'):
            return {'description': mirror.source_marker(SOURCE, 'demo') + (' [repo-mirror:initializing:v1]' if self.initializing else '')}
        if '/releases/tags/' in url:
            tag = url.rsplit('/', 1)[-1]
            if tag not in self.releases:
                raise HTTPError(url, 404, '', {}, io.BytesIO())
            return self.releases[tag].copy()
        if method == 'POST' and url.endswith('/releases'):
            self.posts.append(data.copy())
            release = dict(data, id=len(self.releases) + 1)
            self.releases[data['tag_name']] = release
            return release.copy()
        if method == 'PATCH' and '/releases/' in url:
            self.patches.append((url, data.copy()))
            for release in self.releases.values():
                if url.endswith('/' + str(release['id'])):
                    release.update(data)
            return {}
        return {}

    def pages(self, url):
        return list(self.releases.values()) if url.endswith('/releases') else []


SOURCE = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice'}


def release(n, prerelease=False):
    return {'id': n, 'tag_name': str(n), 'name': str(n), 'draft': False,
            'prerelease': prerelease, 'created_at': f'2024-01-{n:02d}T00:00:00Z'}


def run(src, dst):
    with patch.object(mirror, 'mirror_git'):
        mirror.mirror_repository(SOURCE, 'demo', 'dest', src, dst, 'token')


class LatestAlignmentTests(unittest.TestCase):
    def test_initial_order_and_authoritative_latest(self):
        releases = [release(3, True), release(2), release(1)]
        dst = Destination(True)
        run(Source(releases, releases[2]), dst)
        self.assertEqual([p['tag_name'] for p in dst.posts], ['1', '2', '3'])
        self.assertTrue(all(p['make_latest'] == 'false' for p in dst.posts))
        self.assertEqual(dst.patches[-1][1]['make_latest'], 'true')
        self.assertTrue(dst.patches[-1][0].endswith('/1'))

    def test_initial_mirror_processes_all_releases_beyond_window(self):
        releases = [release(n) for n in range(12, 0, -1)]
        dst = Destination(True)
        run(Source(releases, releases[-1]), dst)
        self.assertEqual(len(dst.posts), 12)
        self.assertEqual(set(dst.releases), {str(n) for n in range(1, 13)})

    def test_existing_latest_outside_window_is_repaired(self):
        releases = [release(n) for n in range(12, 0, -1)]
        dst = Destination(False)
        run(Source(releases, releases[-1]), dst)
        self.assertEqual(set(dst.releases), {'12', '11', '10', '1'})
        self.assertEqual(dst.patches[-1][1]['make_latest'], 'true')
        self.assertTrue(dst.patches[-1][0].endswith('/' + str(dst.releases['1']['id'])))

    def test_source_latest_404_never_promotes_prereleases(self):
        dst = Destination(True)
        run(Source([release(1, True)], HTTPError('https://x', 404, '', {}, io.BytesIO())), dst)
        self.assertEqual(dst.posts[0]['make_latest'], 'false')
        self.assertFalse(any(data.get('make_latest') == 'true' for _, data in dst.patches))

    def test_source_latest_server_error_is_not_ignored(self):
        dst = Destination(True)
        with self.assertRaises(HTTPError) as error:
            run(Source([release(1)], HTTPError('https://x', 503, '', {}, io.BytesIO())), dst)
        error.exception.close()
        self.assertEqual(dst.posts, [])

    def test_invalid_latest_response_is_not_treated_as_404(self):
        with self.assertRaisesRegex(ValueError, 'invalid source latest release'):
            run(Source([release(1)], None), Destination(True))

    def test_initial_order_prefers_published_at_over_created_at(self):
        first, second = release(1), release(2)
        first['published_at'] = '2024-02-02T00:00:00Z'
        second['published_at'] = '2024-02-01T00:00:00Z'
        dst = Destination(True)
        run(Source([second, first], second), dst)
        self.assertEqual([p['tag_name'] for p in dst.posts], ['2', '1'])

    def test_initial_order_accounts_for_timezone_offsets(self):
        first, second = release(1), release(2)
        first['created_at'] = '2024-01-01T01:00:00+02:00'
        second['created_at'] = '2024-01-01T00:00:00Z'
        dst = Destination(True)
        run(Source([second, first], second), dst)
        self.assertEqual([p['tag_name'] for p in dst.posts], ['1', '2'])

    def test_promotion_failure_retried_even_when_old(self):
        releases = [release(n) for n in range(12, 0, -1)]
        class FailingDestination(Destination):
            failed = False
            def request(self, method, url, data=None):
                if data and data.get('make_latest') == 'true' and not self.failed:
                    self.failed = True
                    raise RuntimeError('interrupted promotion')
                return super().request(method, url, data)
        dst = FailingDestination(False)
        with self.assertRaisesRegex(RuntimeError, 'interrupted promotion'):
            run(Source(releases, releases[-1]), dst)
        run(Source(releases, releases[-1]), dst)
        self.assertEqual(dst.patches[-1][1]['make_latest'], 'true')

    def test_forgejo_latest_endpoint_and_expected_404(self):
        forge = {'type': 'forgejo', 'base_url': 'https://forge.example', 'owner': 'alice'}
        class ForgeSource(Source):
            def __init__(self, latest):
                super().__init__([release(1)], latest)
                self.latest_url = None
            def request(self, method, url, data=None):
                if url.endswith('/releases/latest'):
                    self.latest_url = url
                    return super().request(method, url, data)
                if url.endswith('/assets'):
                    return []
                return {'clone_url': 'https://forge.example/alice/demo.git'}
        class ForgeDestination(Destination):
            def request(self, method, url, data=None):
                if method == 'GET' and url.endswith('/repos/dest/mirror-alice-demo'):
                    return {'description': mirror.source_marker(forge, 'demo')}
                return super().request(method, url, data)
        for latest in (release(1), HTTPError('https://x', 404, '', {}, io.BytesIO())):
            src, dst = ForgeSource(latest), ForgeDestination(False)
            with patch.object(mirror, 'mirror_git'):
                mirror.mirror_repository(forge, 'demo', 'dest', src, dst, 'token')
            self.assertEqual(src.latest_url, 'https://forge.example/api/v1/repos/alice/demo/releases/latest')
            self.assertEqual(any(data.get('make_latest') == 'true' for _, data in dst.patches),
                             not isinstance(latest, HTTPError))
