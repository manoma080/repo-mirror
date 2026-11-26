import copy
import unittest
from unittest.mock import Mock, patch

from repo_mirror.forgejo_source_adapter import PublicForgejoSourceAdapter
from repo_mirror.forgejo_destination_adapter import ForgejoDestinationAdapter

from test_empty_body_convergence import Adapter, UP, TARGET
from repo_mirror.repository import mirror_releases

SOURCE = {'type': 'forgejo', 'base_url': 'https://source', 'owner': 'alice'}


class ForgejoEmptyTargetConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.src, self.dst = Adapter(), Adapter(True)
        self.src.release['target_commitish'] = ''
        self.dst.release['target_commitish'] = 'main'
        self.tag_reads = 0
        self.tag_drift = False
        def verify(tag):
            self.assertEqual(tag, 'v1')
            self.tag_reads += 1
            return ('tag', ('b' if self.tag_drift and self.tag_reads > 1 else 'a') * 40)
        self.dst.verify_source_tag = verify

    def sync(self, source=SOURCE):
        with patch('repo_mirror.repository.fetch_source_latest', return_value=None):
            return mirror_releases(source, 'repo', 'team', self.src, self.dst, UP, TARGET,
                                   'marker', initializing=True)

    def test_matching_release_with_empty_forgejo_target_is_read_only(self):
        self.sync()
        self.sync()
        self.assertGreaterEqual(self.tag_reads, 4)
        self.assertTrue(all(method in ('GET', 'DOWNLOAD') for method, _ in self.src.calls + self.dst.calls))
        self.assertEqual(sum(method == 'DOWNLOAD' for method, _ in self.src.calls), 8)

    def test_real_adapters_converge_without_writes(self):
        release = dict(id=10, tag_name='v1', name='title', body='',
                       draft=False, prerelease=False, target_commitish='')
        destination = dict(release, id=20, target_commitish='main')
        api = Mock()
        api.pages.return_value = [release]
        api.request.side_effect = lambda method, url, *a, **kw: [] if url.endswith('/assets') else copy.deepcopy(release)
        src = PublicForgejoSourceAdapter('https://source/alice/repo', api=api)
        client = Mock(origin='http://127.0.0.1:3000', base='/api/v1/repos/team/repo')
        client.list_releases.return_value = [destination]
        client.list_assets.return_value = []
        client._json.return_value = destination
        dst = ForgejoDestinationAdapter('team/repo', 'token', client=client)
        dst.verify_source_tag = Mock(return_value=('tag', 'a' * 40))
        with patch('repo_mirror.repository.fetch_source_latest', return_value=None):
            for _ in range(2):
                mirror_releases(SOURCE, 'repo', 'team', src, dst, src.base, dst.target,
                                'marker', initializing=True)
        self.assertEqual(dst.verify_source_tag.call_count, 4)
        self.assertTrue(all(call.args[0] == 'GET' for call in client._json.call_args_list))
        self.assertTrue(all(call.args[0] == 'GET' for call in api.request.call_args_list))
        client._request.assert_not_called()

    def test_changed_verified_tag_does_not_converge(self):
        self.tag_drift = True
        with self.assertRaises(Exception):
            self.sync()
        self.assertTrue(all(method in ('GET', 'DOWNLOAD') for method, _ in self.src.calls + self.dst.calls))

    def test_github_empty_target_remains_invalid(self):
        with self.assertRaises(Exception):
            self.sync({'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice'})
        self.assertEqual(self.tag_reads, 0)

    def test_recovery_marker_does_not_converge(self):
        self.dst.release['body'] = '[repo-mirror:pending:v3]'
        with self.assertRaises(Exception):
            self.sync()
        self.assertTrue(all(method in ('GET', 'DOWNLOAD') for method, _ in self.src.calls + self.dst.calls))

    def test_destination_identity_change_does_not_converge(self):
        original = self.dst.request
        def request(method, url, body=None):
            result = original(method, url, body)
            if url.endswith('/releases/20'):
                result = copy.deepcopy(result)
                result['id'] = 21
            return result
        self.dst.request = request
        with self.assertRaises(Exception):
            self.sync()
        self.assertTrue(all(method in ('GET', 'DOWNLOAD') for method, _ in self.src.calls + self.dst.calls))
