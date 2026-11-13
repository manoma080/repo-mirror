"""Release orchestration against an in-memory Forgejo API with realistic body writes."""
import copy
import io
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from repo_mirror.forgejo_destination_adapter import ForgejoDestinationAdapter
from repo_mirror.repository import mirror_releases

SOURCE = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice'}
UP = 'https://source/repo'


def missing(url):
    return HTTPError(url, 404, 'not found', {}, io.BytesIO())


class Source:
    def __init__(self, body='', draft=False):
        self.release = dict(id=10, tag_name='v1', name='title', body=body,
                            draft=draft, prerelease=False, target_commitish='main')
        self.data = b'new complete asset bytes\x00'

    def pages(self, url):
        if url.endswith('/assets'):
            return [dict(id=100, name='package', size=len(self.data), state='uploaded',
                         url='https://source/asset', browser_download_url='https://source/asset')]
        return [copy.deepcopy(self.release)]

    def request(self, method, url, body=None):
        if url.endswith('/latest'):
            raise missing(url)
        return copy.deepcopy(self.release)

    def download(self, url, out):
        out.write(self.data)


class ForgejoAPI:
    origin = 'http://127.0.0.1:3300'
    base = '/api/v1/repos/team/project'

    def __init__(self, existing=True):
        self.release = dict(id=20, tag_name='v1', name='title', body='old notes',
                            draft=False, prerelease=False, target_commitish='main') if existing else None
        self.assets = {201: ('package', b'old complete asset bytes\x00')} if existing else {}
        self.calls = []
        self.fail_upload = False
        self.next_id = 202

    def list_releases(self):
        return [copy.deepcopy(self.release)] if self.release else []

    def list_assets(self, release):
        return [dict(id=i, name=n, size=len(data)) for i, (n, data) in self.assets.items()]

    def _json(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path.endswith('/latest'):
            if self.release and not self.release['draft']:
                return copy.deepcopy(self.release)
            raise missing(path)
        if '/tags/' in path and not self.release:
            raise missing(path)
        if method == 'POST':
            self.release = dict(body, id=20)
        elif method == 'PATCH':
            # Forgejo ignores the empty string but persists exactly one space.
            self.release.update({k: v for k, v in body.items() if k != 'body' or v not in ('', None)})
        if self.release is None:
            raise missing(path)
        return copy.deepcopy(self.release)

    def _request(self, method, path):
        self.calls.append((method, path, None))
        if '/assets/' in path:
            del self.assets[int(path.rsplit('/', 1)[1])]
        else:
            self.release = None

    def download(self, asset, output):
        output.write(self.assets[asset['id']][1])

    def upload(self, release, name, data, size, content_type='application/octet-stream'):
        if self.fail_upload:
            self.fail_upload = False
            raise RuntimeError('simulated interruption')
        value = data.read()
        assert len(value) == size
        aid = self.next_id
        self.next_id += 1
        self.assets[aid] = (name, value)
        return dict(id=aid, name=name, size=size)


class ForgejoSpaceIntegrationTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'})
        env.start()
        self.addCleanup(env.stop)
        self.src = Source()
        self.api = ForgejoAPI()
        self.dst = ForgejoDestinationAdapter('team/project', 'token', client=self.api)

    def sync(self):
        return mirror_releases(SOURCE, 'repo', 'team', self.src, self.dst, UP,
                               self.dst.target, 'marker', initializing=False)

    def assert_finished(self, expected=' '):
        self.assertEqual(self.api.release['body'], expected)
        self.assertEqual(list(self.api.assets.values()), [('package', self.src.data)])
        self.assertFalse(self.api.release['draft'])

    def test_existing_changed_asset_and_empty_bodies(self):
        for body in ('', ' '):
            with self.subTest(body=body):
                self.setUp()
                self.src.release['body'] = body
                self.sync()
                self.assert_finished()
                patches = [value for method, _, value in self.api.calls if method == 'PATCH']
                self.assertTrue(any('pending' in str(value) for value in patches))
                self.assertTrue(any(value.get('body') == ' ' for value in patches))

    def test_new_empty_release(self):
        self.api.release = None
        self.api.assets.clear()
        self.sync()
        self.assert_finished()

    def test_interrupted_create_resumes(self):
        self.api.release = None
        self.api.assets.clear()
        self.api.fail_upload = True
        with self.assertRaisesRegex(RuntimeError, 'simulated interruption'):
            self.sync()
        self.assertIn('[repo-mirror:pending:v3]', self.api.release['body'])
        self.sync()
        self.assert_finished()

    def test_resume_after_pending_marker(self):
        self.api.fail_upload = True
        with self.assertRaisesRegex(RuntimeError, 'simulated interruption'):
            self.sync()
        self.assertIn('pending', str(self.api.release))
        self.sync()
        self.assert_finished()

    def test_other_whitespace_remains_distinct(self):
        for body in ('  ', '\t', '\n'):
            with self.subTest(body=body):
                self.setUp()
                self.src.release['body'] = body
                self.sync()
                self.assert_finished(body)

    def test_latest_already_correct(self):
        with patch('repo_mirror.repository.fetch_source_latest', return_value=self.src.release):
            self.sync()
        self.assert_finished()

    def test_draft_release_remains_draft(self):
        self.src.release['draft'] = True
        self.api.release['draft'] = True
        self.sync()
        self.assertIn('[repo-mirror:draft-identity:v1]', self.api.release['body'])
        self.assertEqual(list(self.api.assets.values()), [('package', self.src.data)])
        self.assertTrue(self.api.release['draft'])
        self.src.release['draft'] = False
        self.sync()
        self.assert_finished()

    def test_source_reserved_markers_are_rejected_without_writes(self):
        for body in ('[repo-mirror:pending:v3]{}', '[repo-mirror:draft-identity:v1]forged'):
            with self.subTest(body=body):
                self.api.calls.clear()
                self.src.release['body'] = body
                with self.assertRaises(Exception):
                    self.sync()
                self.assertFalse(any(method in ('PATCH', 'POST', 'DELETE') for method, _, _ in self.api.calls))

    def test_forged_pending_marker_cannot_resume(self):
        self.api.fail_upload = True
        with self.assertRaisesRegex(RuntimeError, 'simulated interruption'):
            self.sync()
        self.api.release['body'] = self.api.release['body'].replace('signature', 'forged_signature')
        prior = copy.deepcopy(self.api.release)
        assets = copy.deepcopy(self.api.assets)
        with self.assertRaises(Exception):
            self.sync()
        self.assertEqual(self.api.release, prior)
        self.assertEqual(self.api.assets, assets)

    def test_source_body_drift_during_upload_is_not_finalized(self):
        upload = self.api.upload
        def drift(*args, **kwargs):
            result = upload(*args, **kwargs)
            self.src.release['body'] = 'changed during upload'
            return result
        self.api.upload = drift
        with self.assertRaises(Exception):
            self.sync()
        self.assertIn('[repo-mirror:pending:v3]', self.api.release['body'])

    def test_source_fixture_reads_fresh_release_snapshot(self):
        original = self.src.pages(UP + '/releases')[0]
        self.src.release['body'] = 'changed during upload'
        self.assertEqual(original['body'], '')
        self.assertEqual(self.src.request('GET', UP + '/releases/10')['body'],
                         'changed during upload')
        self.assertEqual(self.src.pages(UP + '/releases')[0]['body'],
                         'changed during upload')


class GitHubAPI(ForgejoAPI):
    def _json(self, method, path, body=None):
        if method == 'PATCH' and '/assets/' not in path:
            self.calls.append((method, path, copy.deepcopy(body)))
            self.release.update(body)
            return copy.deepcopy(self.release)
        return super()._json(method, path, body)


class GitHubShapedDestination(ForgejoDestinationAdapter):
    """In-memory GitHub-shaped transport, preserving empty bodies on writes."""
    supports_target_commitish = False
    verify_empty_body_read_only = False

    def _release(self, item):
        release = super()._release(item)
        release['body'] = item.get('body')
        return release

    def _request(self, method, url, body=None, authorized_old_tag=None):
        path = self._path(url)
        if method in ('POST', 'PATCH') and '/assets/' not in path:
            return self._release(self.client._json(method, self.client.base + path, body))
        return super()._request(method, url, body, authorized_old_tag)


class GitHubSpaceIntegrationTests(unittest.TestCase):
    sync = ForgejoSpaceIntegrationTests.sync

    def setUp(self):
        env = patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'})
        env.start()
        self.addCleanup(env.stop)
        self.src = Source()
        self.api = GitHubAPI()
        self.dst = GitHubShapedDestination('team/project', 'token', client=self.api)

    def test_existing_changed_asset_empty_and_one_space(self):
        for body in ('', ' ', '  '):
            with self.subTest(body=body):
                self.setUp()
                self.src.release['body'] = body
                self.sync()
                # GitHub writes preserve source whitespace; Forgejo's adapter alone
                # translates semantic emptiness to its single-space storage value.
                expected = body
                self.assertEqual(self.api.release['body'], expected)
                self.assertEqual(list(self.api.assets.values()), [('package', self.src.data)])
                patches = [value for method, _, value in self.api.calls if method == 'PATCH']
                self.assertTrue(any('pending' in str(value) for value in patches))
                self.assertEqual(patches[-1]['body'], expected)

    def test_interrupted_create_resumes(self):
        self.api.release = None
        self.api.assets.clear()
        self.api.fail_upload = True
        with self.assertRaisesRegex(RuntimeError, 'simulated interruption'):
            self.sync()
        self.assertIn('[repo-mirror:pending:v3]', self.api.release['body'])
        self.sync()
        self.assertEqual(self.api.release['body'], '')
        self.assertEqual(list(self.api.assets.values()), [('package', self.src.data)])


class DraftBodyNormalizationTests(unittest.TestCase):
    def test_signed_draft_body_normalizes_only_semantic_empty(self):
        from repo_mirror.repository import IDENTITY_SENTINEL, draft_identity_signature, find_tag_releases
        secret = 'synthetic-test-only-secret'
        with patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': secret}):
            for api_cls, adapter_cls in ((ForgejoAPI, ForgejoDestinationAdapter), (GitHubAPI, GitHubShapedDestination)):
                bodies = {}
                for body in (None, '', ' ', '  ', '\t', '\n'):
                    with self.subTest(destination=api_cls.__name__, body=body):
                        src, api = Source(body=body, draft=True), api_cls()
                        api.release['draft'] = True
                        dst = adapter_cls('team/project', 'token', client=api)
                        mirror_releases(SOURCE, 'repo', 'team', src, dst, UP, dst.target, 'marker', initializing=False)
                        result = api.release['body']
                        sig = draft_identity_signature(secret, 'marker', 'v1', dst.target, api.release['id'])
                        prefix = '' if body in (None, '', ' ') else body
                        self.assertEqual(result.encode(), (prefix + '\n' + IDENTITY_SENTINEL + sig).encode())
                        self.assertTrue(find_tag_releases(None, dst.target, 'v1', releases=[api.release], marker='marker'))
                        forged = dict(api.release, body=result[:-1] + ('0' if result[-1] != '0' else '1'))
                        forged['tag_name'] = 'untagged-20'
                        with self.assertRaises(ValueError):
                            find_tag_releases(None, dst.target, 'v1', releases=[forged], marker='marker')
                        bodies[body] = result.encode()
                self.assertEqual(bodies[None], bodies[''])
                self.assertEqual(bodies[''], bodies[' '])
