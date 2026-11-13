import unittest
from unittest.mock import patch

from test_forgejo_space_body_integration import SOURCE, UP, Source, ForgejoAPI, GitHubAPI, GitHubShapedDestination
from repo_mirror.forgejo_destination_adapter import ForgejoDestinationAdapter
from repo_mirror.repository import mirror_releases


class SourceMetadataDriftTests(unittest.TestCase):
    def test_list_can_omit_optional_metadata_present_in_authoritative_snapshot(self):
        with patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'}):
            src, api = Source(), ForgejoAPI()
            pages = src.pages
            def list_without_target(url):
                result = pages(url)
                if url.endswith('/releases'):
                    del result[0]['target_commitish']
                return result
            src.pages = list_without_target
            dst = ForgejoDestinationAdapter('team/project', 'token', client=api)
            mirror_releases(SOURCE, 'repo', 'team', src, dst, UP, dst.target, 'marker', initializing=False)
            self.assertEqual(api.release['target_commitish'], 'main')
            self.assertNotIn('[repo-mirror:pending:v3]', api.release['body'])

    def test_prewrite_lookup_failure_or_identity_mismatch_never_mutates_destination(self):
        from urllib.error import HTTPError
        from repo_mirror.logging_utils import error_detail
        for failure in ('get', 'id', 'tag_name', 'name', 'latest_id'):
            with self.subTest(failure=failure), patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'}):
                src, api = Source(), ForgejoAPI()
                request = src.request
                def changed(method, url, body=None):
                    if url.endswith('/latest') and failure == 'latest_id':
                        result = src.release.copy()
                        result['id'] = 11
                        return result
                    if url.endswith('/releases/10') or url.endswith('/releases/11'):
                        if failure == 'get':
                            raise HTTPError(url, 503, 'unavailable', {}, None)
                        result = src.release.copy()
                        if failure == 'id': result['id'] = 11
                        if failure == 'tag_name': result['tag_name'] = 'other'
                        if failure == 'name': result['name'] = 'other'
                        return result
                    return request(method, url, body)
                src.request = changed
                dst = ForgejoDestinationAdapter('team/project', 'token', client=api)
                original_assets = api.assets.copy()
                with self.assertRaises(Exception) as caught:
                    mirror_releases(SOURCE, 'repo', 'team', src, dst, UP, dst.target, 'marker', initializing=False)
                if failure != 'get':
                    self.assertEqual(error_detail(caught.exception), 'source release changed during sync')
                self.assertFalse(any(call[0] != 'GET' for call in api.calls))
                self.assertEqual(api.assets, original_assets)
                self.assertEqual(api.release['body'], 'old notes')

    def test_raw_metadata_changes_leave_recovery_marker(self):
        changes = [('body', '', 'changed'), ('body', '', ' '), ('body', ' ', ''),
                   ('name', 'title', 'private-new-name'), ('tag_name', 'v1', 'v2'),
                   ('id', 10, 11), ('draft', False, True), ('prerelease', False, True),
                   ('target_commitish', 'main', 'other')]
        for api_cls, adapter_cls in ((ForgejoAPI, ForgejoDestinationAdapter),
                                     (GitHubAPI, GitHubShapedDestination)):
            for field, initial, value in changes:
                with self.subTest(destination=api_cls.__name__, field=field, value=value), \
                     patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'}):
                    src, api = Source(), api_cls()
                    src.release[field] = initial
                    dst = adapter_cls('team/project', 'token', client=api)
                    upload = api.upload
                    def drift(*args, **kwargs):
                        result = upload(*args, **kwargs)
                        src.release[field] = value
                        return result
                    api.upload = drift
                    with self.assertRaises(Exception) as error:
                        mirror_releases(SOURCE, 'repo', 'team', src, dst, UP, dst.target,
                                        'marker', initializing=False)
                    self.assertEqual(str(error.exception), 'source release changed during sync')
                    from repo_mirror.logging_utils import error_detail
                    self.assertEqual(error_detail(error.exception), 'source release changed during sync')
                    self.assertNotIn('private-new-name', str(error.exception))
                    self.assertIn('[repo-mirror:pending:v3]', api.release['body'])

    def test_omitted_and_null_target_are_distinct(self):
        with patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'}):
            src, api = Source(), ForgejoAPI()
            src.release['target_commitish'] = None
            dst = ForgejoDestinationAdapter('team/project', 'token', client=api)
            upload = api.upload
            def drift(*args, **kwargs):
                result = upload(*args, **kwargs)
                del src.release['target_commitish']
                return result
            api.upload = drift
            with self.assertRaises(Exception):
                mirror_releases(SOURCE, 'repo', 'team', src, dst, UP, dst.target, 'marker', initializing=False)
            self.assertIn('[repo-mirror:pending:v3]', api.release['body'])

    def test_malformed_authoritative_target_omitted_from_listing_fails_before_writes(self):
        for bad in (False, 1, [], {}, ''):
            with self.subTest(value=bad), patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'}):
                src, api = Source(), ForgejoAPI()
                src.release['target_commitish'] = bad
                pages = src.pages
                def without_target(url):
                    result = pages(url)
                    if url.endswith('/releases'):
                        del result[0]['target_commitish']
                    return result
                src.pages = without_target
                dst = ForgejoDestinationAdapter('team/project', 'token', client=api)
                with self.assertRaises(Exception):
                    mirror_releases(SOURCE, 'repo', 'team', src, dst, UP, dst.target, 'marker', initializing=False)
                self.assertFalse(any(method != 'GET' for method, _, _ in api.calls))

    def test_omitted_optional_fields_are_compatible_on_shared_path(self):
        with patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'}):
            src, api = Source(), GitHubAPI()
            for field in ('name', 'body', 'draft', 'prerelease', 'target_commitish'):
                src.release.pop(field, None)
            dst = GitHubShapedDestination('team/project', 'token', client=api)
            mirror_releases(SOURCE, 'repo', 'team', src, dst, UP, dst.target, 'marker', initializing=False)
            self.assertIsNone(api.release['body'])

    def test_malformed_authoritative_fields_fail_before_writes(self):
        cases = [('id', True), ('tag_name', []), ('draft', None), ('prerelease', 1),
                 ('body', []), ('name', {})]
        for field, value in cases:
            with self.subTest(field=field, value=value), patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'}):
                src, api = Source(), ForgejoAPI()
                request, pages = src.request, src.pages
                def altered(method, url, body=None):
                    result = request(method, url, body)
                    if url.endswith('/releases/10'):
                        if value is Ellipsis:
                            result.pop(field)
                        else:
                            result[field] = value
                    return result
                def sparse(url):
                    result = pages(url)
                    if url.endswith('/releases') and field not in ('id', 'tag_name'):
                        result[0].pop(field)
                    return result
                src.request, src.pages = altered, sparse
                dst = ForgejoDestinationAdapter('team/project', 'token', client=api)
                with self.assertRaises(Exception):
                    mirror_releases(SOURCE, 'repo', 'team', src, dst, UP, dst.target, 'marker', initializing=False)
                self.assertFalse(any(method != 'GET' for method, _, _ in api.calls))
