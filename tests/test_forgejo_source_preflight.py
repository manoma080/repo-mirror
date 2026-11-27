from types import SimpleNamespace
from unittest.mock import patch, Mock
from urllib.error import HTTPError
from email.message import Message
import tempfile
import unittest


from repo_mirror import forgejo_sync
from repo_mirror.forgejo_git import APIError, ForgejoDestination


def no_latest(*args):
    raise HTTPError('https://api.github.com', 404, 'not found', Message(), None)


class SourcePreflightTests(unittest.TestCase):
    def test_invalid_release_metadata_blocks_destination_initialization(self):
        valid = {'id': 1, 'tag_name': 'release/v1', 'name': None, 'body': None,
                 'draft': False, 'prerelease': False}
        for field, value in [('id', 0), ('id', True), ('name', 5), ('draft', None), ('prerelease', 'false')]:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as tmp:
                release = dict(valid, **{field: value})
                dest = SimpleNamespace(begin_initialization=Mock())
                src = SimpleNamespace(pages=lambda url: [release], request=lambda *a: valid)
                with patch.object(forgejo_sync, 'ForgejoSourceAdapter', return_value=src):
                    self.assertEqual(forgejo_sync._sync_requested(SimpleNamespace(port=3300, owner='local'),
                        ['mirror-owner/mirror-test'], dest, 'token', 'local', 'secret', tmp), 1)
                dest.begin_initialization.assert_not_called()

    def test_latest_failures_never_initialize_destination(self):
        valid = {'id': 1, 'tag_name': 'release/v1', 'name': None, 'body': None,
                 'draft': False, 'prerelease': False}
        cases = [None, {}, dict(valid, id=2), dict(valid, tag_name='other'),
                 dict(valid, draft=True), dict(valid, prerelease=True), dict(valid, name=3),
                 OSError('network failure'), HTTPError('https://api.github.com', 500, 'error', Message(), None)]
        for latest in cases:
            with self.subTest(latest=latest), tempfile.TemporaryDirectory() as tmp:
                dest = SimpleNamespace(begin_initialization=Mock())
                def request(*args):
                    if isinstance(latest, Exception):
                        raise latest
                    return latest
                src = SimpleNamespace(pages=lambda url: [valid], request=request)
                with patch.object(forgejo_sync, 'ForgejoSourceAdapter', return_value=src):
                    self.assertEqual(forgejo_sync._sync_requested(SimpleNamespace(port=3300, owner='local'),
                        ['mirror-owner/mirror-test'], dest, 'token', 'local', 'secret', tmp), 1)
                dest.begin_initialization.assert_not_called()

    def test_latest_must_be_eligible_in_listing(self):
        latest = {'id': 1, 'tag_name': 'v1', 'name': None, 'body': None,
                  'draft': False, 'prerelease': False}
        for field in ('draft', 'prerelease'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                dest = SimpleNamespace(begin_initialization=Mock())
                src = SimpleNamespace(pages=lambda url: [dict(latest, **{field: True})],
                                      request=lambda *args: latest)
                with patch.object(forgejo_sync, 'ForgejoSourceAdapter', return_value=src):
                    self.assertEqual(forgejo_sync._sync_requested(SimpleNamespace(port=3300, owner='local'),
                        ['mirror-owner/mirror-test'], dest, 'token', 'local', 'secret', tmp), 1)
                dest.begin_initialization.assert_not_called()

    def test_missing_listing_fields_block_initialization(self):
        valid = {'id': 1, 'tag_name': 'v1', 'name': None, 'body': None, 'draft': False, 'prerelease': False}
        for field in valid:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                release = dict(valid)
                del release[field]
                dest = SimpleNamespace(begin_initialization=Mock())
                src = SimpleNamespace(pages=lambda url: [release], request=lambda *a: valid)
                with patch.object(forgejo_sync, 'ForgejoSourceAdapter', return_value=src):
                    self.assertEqual(forgejo_sync._sync_requested(SimpleNamespace(port=3300, owner='local'),
                        ['mirror-owner/mirror-test'], dest, 'token', 'local', 'secret', tmp), 1)
                dest.begin_initialization.assert_not_called()

    def test_unsafe_source_never_mutates_real_destination(self):
        class Recorder:
            def __init__(self, exists):
                self.calls = []
                self.repo = ({'id': 1, 'full_name': 'local/mirror-test', 'private': True,
                              'has_actions': False,
                              'description': 'repo-mirror-source: github.com/mirror-owner/mirror-test'}
                             if exists else None)

            def request(self, method, path, payload=None):
                self.calls.append((method, path))
                if method == 'POST':
                    self.repo = dict(payload, id=1, full_name='local/mirror-test')
                elif method == 'PATCH':
                    self.repo.update(payload)
                elif self.repo is None:
                    raise APIError(404)
                return dict(self.repo)

        valid = {'id': 1, 'tag_name': 'release/v1', 'name': 'release name', 'body': 'notes',
                 'draft': False, 'prerelease': False}
        cases = ([dict(valid, body='[repo-mirror:pending:v3]')],
                 [dict(valid, body=123)], OSError('listing failed'),
                 [valid, {}], [valid, {'tag_name': ''}], [valid, {'tag_name': 12}],
                 [valid, None], [valid, dict(valid)],
                 [valid, {'tag_name': 'untagged-synthetic', 'draft': True}])
        for exists in (False, True):
            for result in cases:
                with self.subTest(exists=exists, result=result), tempfile.TemporaryDirectory() as tmp:
                    api = Recorder(exists)
                    dest = ForgejoDestination(api, 'local')
                    def pages(url):
                        if isinstance(result, Exception):
                            raise result
                        return result
                    with patch.object(forgejo_sync, 'ForgejoSourceAdapter', return_value=SimpleNamespace(pages=pages, request=no_latest)), \
                         patch.object(forgejo_sync, 'sync_refs') as git:
                        status = forgejo_sync._sync_requested(SimpleNamespace(port=3300, owner='local'),
                            ['mirror-owner/mirror-test'], dest, 'source', 'local', 'secret', tmp)
                    self.assertEqual(status, 1)
                    self.assertEqual([call for call in api.calls if call[0] in ('POST', 'PATCH', 'PUT', 'DELETE')], [])
                    git.assert_not_called()

    def test_unsafe_source_stops_before_git_or_release_writes(self):
        releases = [
            {'body': '[repo-mirror:pending:v3]', 'name': 'x'},
            {'body': 'x [repo-mirror:draft-identity:v1]', 'name': 'x', 'draft': True},
            {'body': None, 'name': '[repo-mirror:pending:v1]abc', 'draft': True},
            {'body': 123, 'name': 'x'},
        ]
        for overrides in releases:
            release = dict({'id': 1, 'tag_name': 'v1', 'body': None, 'name': None,
                            'draft': False, 'prerelease': False}, **overrides)
            with self.subTest(release=release), tempfile.TemporaryDirectory() as tmp:
                args = SimpleNamespace(port=3300, owner='local')
                dest = SimpleNamespace(begin_initialization=lambda *a: False, run_pin=(1, 'marker'), revalidate=lambda *a: None)
                src = SimpleNamespace(pages=lambda url: [release])
                with patch.object(forgejo_sync, 'ForgejoSourceAdapter', return_value=src), \
                     patch.object(forgejo_sync, 'sync_refs') as git, \
                     patch.object(forgejo_sync, 'mirror_releases') as releases:
                    self.assertEqual(forgejo_sync._sync_requested(args, ['mirror-owner/mirror-test'], dest, 'github', 'local', 'secret', tmp), 1)
                    git.assert_not_called()
                    releases.assert_not_called()

    def test_preflight_empty_snapshot_and_pin_are_used(self):
        args = SimpleNamespace(port=3300, owner='local')
        checks = []
        dest = SimpleNamespace(begin_initialization=lambda *a: False, run_pin=(1, 'marker'),
                               revalidate=lambda *a: checks.append(a))
        urls = []
        src = SimpleNamespace(pages=lambda url: urls.append(url) or [], request=no_latest)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(forgejo_sync, 'ForgejoSourceAdapter', return_value=src) as factory, \
             patch.object(forgejo_sync, 'sync_refs', return_value=0) as git, \
             patch.object(forgejo_sync, 'snapshot_refs', return_value=(None, {}, {})) as snapshot, \
             patch.object(forgejo_sync, 'ForgejoDestinationAdapter') as adapter, \
             patch.object(forgejo_sync, 'mirror_releases') as mirror:
            adapter.return_value.target = 'target'
            self.assertEqual(forgejo_sync._sync_requested(args, ['mirror-owner/mirror-test'], dest, 'github-token', 'local', 'secret', tmp), 0)
            self.assertEqual(urls, ['https://api.github.com/repos/mirror-owner/mirror-test/releases'])
            self.assertEqual(factory.call_args.args[:2], ('mirror-owner/mirror-test', 'github-token'))
            self.assertEqual(git.call_args.args[2], 'mirror-owner/mirror-test')
            self.assertEqual(len(snapshot.call_args_list), 2)
            self.assertTrue(all(c.args[2] == 'mirror-owner/mirror-test' for c in snapshot.call_args_list))
            self.assertEqual(git.call_args.kwargs, {'source_token': 'github-token', 'expected': (1, 'marker')})
            self.assertTrue(checks)
            self.assertIs(mirror.call_args.args[3], src)

    def test_valid_slash_tags_and_release_names_pass_preflight(self):
        args = SimpleNamespace(port=3300, owner='local')
        dest = SimpleNamespace(begin_initialization=lambda *a: False, run_pin=(1, 'marker'),
                               revalidate=lambda *a: None)
        listing = [{'id': 1, 'tag_name': 'release/v1', 'name': 'Release 1 / stable', 'body': 'notes', 'draft': False, 'prerelease': False},
                   {'id': 2, 'tag_name': 'untagged-published', 'draft': False, 'prerelease': False, 'name': None, 'body': None}]
        src = SimpleNamespace(pages=lambda url: listing, request=lambda *a: listing[0])
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(forgejo_sync, 'ForgejoSourceAdapter', return_value=src), \
             patch.object(forgejo_sync, 'sync_refs', return_value=0) as git, \
             patch.object(forgejo_sync, 'snapshot_refs', return_value=(None, {}, {})), \
             patch.object(forgejo_sync, 'ForgejoDestinationAdapter'), \
             patch.object(forgejo_sync, 'mirror_releases') as releases:
            self.assertEqual(forgejo_sync._sync_requested(args, ['mirror-owner/mirror-test'], dest,
                                                          'token', 'local', 'secret', tmp), 0)
            git.assert_called_once()
            releases.assert_called_once()

    def test_changed_release_snapshot_blocks_release_writes_and_preserves_journal(self):
        from repo_mirror.journal import FailureJournal
        import hashlib
        import os

        release = {'id': 1, 'tag_name': 'v1', 'name': None, 'body': None,
                   'draft': False, 'prerelease': False}
        changed = dict(release, id=2, tag_name='v2')
        args = SimpleNamespace(port=3300, owner='local')
        dest = SimpleNamespace(begin_initialization=lambda *a: False, run_pin=None)
        src = SimpleNamespace(pages=Mock(side_effect=[[release], [changed]]), request=Mock(return_value=release))
        adapter = Mock(target='target')
        with tempfile.TemporaryDirectory() as tmp:
            journal_id = hashlib.sha256(b'3300:local:mirror-owner/mirror-test').hexdigest()
            path = os.path.join(tmp, journal_id + '.json')
            journal = FailureJournal(path)
            key = 'a' * 64 + ':' + 'b' * 64
            journal.set(key, True)
            with patch.object(forgejo_sync, 'ForgejoSourceAdapter', return_value=src), \
                 patch.object(forgejo_sync, 'sync_refs', return_value=1) as refs, \
                 patch.object(forgejo_sync, 'snapshot_refs', return_value=(None, {}, {})), \
                 patch.object(forgejo_sync, 'ForgejoDestinationAdapter', return_value=adapter):
                status = forgejo_sync._sync_requested(args, ['mirror-owner/mirror-test'], dest,
                                                     'token', 'local', 'secret', tmp)
            self.assertEqual(status, 1)
            refs.assert_called_once()  # Refs may already have changed after preflight.
            self.assertEqual(src.pages.call_count, 2)
            self.assertEqual(src.request.call_count, 2)
            adapter.request.assert_not_called()
            adapter.pages.assert_not_called()
            self.assertEqual(FailureJournal(path).failures, {key})

    def test_read_failure_or_changed_pin_blocks_git(self):
        args = SimpleNamespace(port=3300, owner='local')
        for failure in ('read', 'pin'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                def check(*args):
                    if failure == 'pin': raise RuntimeError('replaced')
                def pages(url):
                    if failure == 'read':
                        raise OSError('unavailable')
                    return []
                dest = SimpleNamespace(begin_initialization=lambda *a: False, run_pin=(1, 'marker'), revalidate=check)
                with patch.object(forgejo_sync, 'ForgejoSourceAdapter', return_value=SimpleNamespace(pages=pages, request=no_latest)), \
                     patch.object(forgejo_sync, 'sync_refs') as git, \
                     patch.object(forgejo_sync, 'mirror_releases') as releases:
                    self.assertEqual(forgejo_sync._sync_requested(args, ['mirror-owner/mirror-test'], dest, 'token', 'local', 'secret', tmp), 1)
                    git.assert_not_called()
                    releases.assert_not_called()
