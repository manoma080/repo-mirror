import copy
import unittest
from unittest.mock import patch

from repo_mirror.forgejo_destination_adapter import ForgejoDestinationAdapter
from repo_mirror.journal import FailureJournal
from repo_mirror.repository import mirror_releases

UP = 'https://source/repo'
TARGET = 'http://destination/repo'
SOURCE = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice'}


class Adapter:
    def __init__(self, destination=False):
        self.destination = destination
        self.release = dict(id=20 if destination else 10, tag_name='v1', name='title', body='',
                            draft=False, prerelease=False, target_commitish='main')
        self.assets = [dict(id=(200 if destination else 100) + i, name=name, size=len(data),
                            state='uploaded', digest=None, url=f'{"d" if destination else "s"}{i}')
                       for i, (name, data) in enumerate([('package', b'abc'), ('manifest', b'xyz')])]
        self.data = {a['url']: data for a, data in zip(self.assets, (b'abc', b'xyz'))}
        self.calls = []
        self.list_count = 0
        self.drift = False
        if destination:
            self.verify_empty_body_read_only = True
            self.validate_release_source = ForgejoDestinationAdapter.validate_release_source.__get__(self)

    def pages(self, url):
        self.calls.append(('GET', url))
        if url.endswith('/assets'):
            self.list_count += 1
            assets = copy.deepcopy(self.assets)
            if self.drift and self.list_count > 1:
                assets[0]['id'] += 1
            return assets
        return [copy.deepcopy(self.release)]

    def request(self, method, url, body=None):
        self.calls.append((method, url))
        if method != 'GET':
            raise AssertionError('unexpected mutation')
        if url.endswith('/latest'):
            return copy.deepcopy(self.release) if not self.destination else {'id': self.release['id']}
        return copy.deepcopy(self.release)

    def download(self, url, output):
        self.calls.append(('DOWNLOAD', url))
        output.write(self.data[url])


class EmptyConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.src, self.dst = Adapter(), Adapter(True)

    def run_sync(self):
        # Source latest is deliberately another release: this release is non-latest.
        with patch('repo_mirror.repository.fetch_source_latest', return_value=None):
            return mirror_releases(SOURCE, 'repo', 'team', self.src, self.dst, UP, TARGET,
                                   'marker', initializing=True)

    def assert_read_only(self):
        for method, _ in self.src.calls + self.dst.calls:
            self.assertIn(method, ('GET', 'DOWNLOAD'))

    def test_matching_empty_release_is_read_only(self):
        self.run_sync()
        self.assert_read_only()
        self.assertEqual(sum(c[0] == 'DOWNLOAD' for c in self.src.calls), 4)
        self.assertGreaterEqual(sum(c[0] == 'DOWNLOAD' for c in self.dst.calls), 2)

    def test_single_space_is_empty_on_either_side(self):
        for source in (None, '', ' '):
            for destination in (None, '', ' '):
                with self.subTest(source=source, destination=destination):
                    self.setUp()
                    self.src.release['body'] = source
                    self.dst.release['body'] = destination
                    self.run_sync()
                    self.assert_read_only()

    def test_source_duplicate_tag_after_discovery_fails_without_mutation(self):
        original = self.src.pages
        release_reads = 0
        def pages(url):
            nonlocal release_reads
            items = original(url)
            if url.endswith('/releases'):
                release_reads += 1
                if release_reads > 1:
                    duplicate = copy.deepcopy(items[0])
                    duplicate['id'] = 11
                    items.append(duplicate)
            return items
        self.src.pages = pages
        with self.assertRaises(Exception):
            self.run_sync()
        self.assertGreaterEqual(release_reads, 2)
        self.assert_read_only()

    def test_latest_is_verified_without_mutation(self):
        with patch('repo_mirror.repository.fetch_source_latest', return_value=self.src.release):
            mirror_releases(SOURCE, 'repo', 'team', self.src, self.dst, UP, TARGET,
                            'marker', initializing=True)
        self.assertIn(('GET', TARGET + '/releases/latest'), self.dst.calls)
        self.assert_read_only()

    def test_latest_mismatch_fails_without_mutation(self):
        original = self.dst.request
        def request(method, url, body=None):
            if url.endswith('/latest'):
                self.dst.calls.append((method, url))
                return {'id': 999}
            return original(method, url, body)
        self.dst.request = request
        with patch('repo_mirror.repository.fetch_source_latest', return_value=self.src.release):
            with self.assertRaises(Exception):
                mirror_releases(SOURCE, 'repo', 'team', self.src, self.dst, UP, TARGET,
                                'marker', initializing=True)
        self.assert_read_only()

    def test_journaled_failure_blocks_read_only_convergence(self):
        class Journal:
            key = staticmethod(FailureJournal.key)
            failures = {FailureJournal.key(SOURCE, 'repo', 'team', 'v1', 'package')}
        with patch('repo_mirror.repository.fetch_source_latest', return_value=None):
            with self.assertRaises(Exception):
                mirror_releases(SOURCE, 'repo', 'team', self.src, self.dst, UP, TARGET,
                                'marker', initializing=True, journal=Journal())
        self.assert_read_only()

    def test_mismatches_fail_closed_without_mutations(self):
        mutations = [
            lambda s, d: d.release.update(body='[repo-mirror:pending:v3]'),
            lambda s, d: d.release.update(draft=True),
            lambda s, d: d.release.update(prerelease='false'),
            lambda s, d: d.release.update(target_commitish='other'),
            lambda s, d: d.release.update(name='other'),
            lambda s, d: d.data.update(d0=b'bad'),
            lambda s, d: d.assets.append(copy.deepcopy(d.assets[0])),
            lambda s, d: d.assets.pop(),
            lambda s, d: s.assets[0].update(state='starter'),
            lambda s, d: d.assets[0].update(id=True),
            lambda s, d: d.assets[0].update(size=True),
            lambda s, d: setattr(d, 'drift', True),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.setUp()
                mutate(self.src, self.dst)
                with self.assertRaises(Exception):
                    self.run_sync()
                self.assert_read_only()


class ConvergenceRegressionTests(unittest.TestCase):
    def setUp(self):
        self.src, self.dst = Adapter(), Adapter(True)

    def verify(self, src=None, upstream=UP):
        from repo_mirror.repository import verify_empty_body_converged
        return verify_empty_body_converged(src or self.src, self.dst, upstream, TARGET, self.src.release)

    def test_forgejo_raw_body_drift_during_transfer_fails_closed(self):
        from test_forgejo_destination_adapter import make_adapter
        from repo_mirror.repository import verify_empty_body_converged

        dst, client = make_adapter()
        client.release = copy.deepcopy(self.dst.release)
        client.releases = [client.release]
        client.items = copy.deepcopy(self.dst.assets)
        def download(asset, output):
            output.write(self.dst.data[asset['url']])
            client.release['body'] = ' '
        client.download = download
        with patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'configured-secret'}):
            result = verify_empty_body_converged(self.src, dst, UP, dst.target, self.src.release)
        self.assertIsNone(result)
        self.assertEqual(client.release['body'], ' ')
        self.assertFalse(any(call[0] in ('POST', 'PATCH', 'DELETE', 'upload') for call in client.calls))

    def test_published_sha256_mismatch_fails_closed_without_writes(self):
        self.src.assets[0]['digest'] = 'sha256:' + '0' * 64
        self.assertIsNone(self.verify())
        self.assertTrue(all(method in ('GET', 'DOWNLOAD') for method, _ in self.src.calls + self.dst.calls))

    def test_correct_published_sha256_converges(self):
        import hashlib
        self.src.assets[0]['digest'] = 'sha256:' + hashlib.sha256(b'abc').hexdigest().upper()
        self.assertEqual(self.verify(), 20)

    def test_malformed_published_sha256_fails_closed(self):
        self.src.assets[0]['digest'] = 'sha256:not-a-digest'
        self.assertIsNone(self.verify())

    def test_github_browser_url_is_bound_to_loaded_asset_id(self):
        from repo_mirror.forgejo_source_adapter import ForgejoSourceAdapter
        base = 'https://api.github.com/repos/alice/repo'
        for i, asset in enumerate(self.src.assets):
            asset['url'] = base + '/releases/assets/' + str(asset['id'])
            asset['browser_download_url'] = f'https://github.com/alice/repo/releases/download/v1/{asset["name"]}'
        class API:
            def pages(inner, url):
                return self.src.pages(url)
            def request(inner, method, url, data=None, **kwargs):
                return self.src.request(method, url, data)
        class Client:
            def _download_once(inner, asset, output, **kwargs):
                output.write((b'abc', b'xyz')[asset['id'] - 100])
        adapter = ForgejoSourceAdapter('alice/repo', 'secret', api=API(), client=Client())
        self.assertEqual(self.verify(adapter, base), 20)

    def test_nested_volatile_asset_metadata_changes_during_download_are_ignored(self):
        for adapter in (self.src, self.dst):
            original_pages, original_download = adapter.pages, adapter.download
            counter = [0]
            def pages(url, pages=original_pages, count=counter, owner=adapter):
                items = pages(url)
                if url.endswith('/releases'):
                    items[0]['assets'] = copy.deepcopy(owner.assets)
                    for asset in items[0]['assets']:
                        asset['download_count'] = count[0]
                        asset['updated_at'] = str(count[0])
                return items
            def download(url, output, download=original_download, count=counter):
                download(url, output)
                count[0] += 1
            adapter.pages, adapter.download = pages, download
        self.assertEqual(self.verify(), 20)

    def test_listing_drift_and_duplicates_fail_closed_on_either_side(self):
        for side in ('src', 'dst'):
            for change in ('id', 'tag_name', 'name', 'body', 'draft', 'prerelease', 'target_commitish',
                           'asset_id', 'asset_name', 'asset_size', 'asset_state', 'asset_digest',
                           'duplicate_id', 'duplicate_tag'):
                with self.subTest(side=side, change=change):
                    self.setUp()
                    adapter = getattr(self, side)
                    original = adapter.pages
                    reads = [0]
                    def pages(url):
                        items = original(url)
                        if url.endswith('/releases'):
                            reads[0] += 1
                            items[0]['assets'] = copy.deepcopy(adapter.assets)
                            if reads[0] == 2:
                                if change.startswith('duplicate_'):
                                    other = copy.deepcopy(items[0])
                                    other['id'] += 1
                                    other['tag_name'] = 'other'
                                    other[change.removeprefix('duplicate_') if change == 'duplicate_id' else 'tag_name'] = items[0]['id' if change == 'duplicate_id' else 'tag_name']
                                    items.append(other)
                                elif change.startswith('asset_'):
                                    key = change.removeprefix('asset_')
                                    items[0]['assets'][0][key] = {'id': 999, 'name': 'other', 'size': 999, 'state': 'starter', 'digest': 'changed'}[key]
                                else:
                                    items[0][change] = {'id': 999, 'tag_name': 'other', 'name': 'other', 'body': 'changed', 'draft': True, 'prerelease': True, 'target_commitish': 'other'}[change]
                        return items
                    adapter.pages = pages
                    self.assertIsNone(self.verify())

    def test_same_size_bytes_change_between_downloads_fails_closed(self):
        original = self.dst.download
        count = [0]
        def download(url, output):
            count[0] += 1
            if count[0] > 2:
                self.dst.data[url] = b'bad'
            original(url, output)
        self.dst.download = download
        self.assertIsNone(self.verify())

    def test_duplicate_destination_tag_fails_closed(self):
        original = self.dst.pages
        def pages(url):
            items = original(url)
            if url.endswith('/releases'):
                duplicate = copy.deepcopy(items[0]); duplicate.update(id=21, draft=True)
                items.append(duplicate)
            return items
        self.dst.pages = pages
        self.assertIsNone(self.verify())

    def test_mutation_during_second_download_fails_closed(self):
        original = self.dst.download
        count = 0
        def download(url, output):
            nonlocal count
            count += 1
            original(url, output)
            if count == 3:
                self.dst.release['body'] = '[repo-mirror:pending:v3]'
        self.dst.download = download
        self.assertIsNone(self.verify())
