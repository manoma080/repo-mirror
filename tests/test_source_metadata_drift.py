import unittest
from unittest.mock import patch

from test_forgejo_space_body_integration import SOURCE, UP, Source, ForgejoAPI, GitHubAPI, GitHubShapedDestination
from repo_mirror.forgejo_destination_adapter import ForgejoDestinationAdapter
from repo_mirror.repository import mirror_releases


class SourceMetadataDriftTests(unittest.TestCase):
    def test_forgejo_empty_target_with_existing_identical_tag(self):
        source = {'type': 'forgejo', 'base_url': 'https://forge.example', 'owner': 'alice'}
        with patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'}):
            src, api = Source(body='notes'), GitHubAPI()
            src.release['target_commitish'] = ''
            dst = GitHubShapedDestination('team/project', 'token', client=api)
            src_request, dst_request = src.request, dst.request
            def source_request(method, url, body=None):
                if '/git/refs/tags/' in url:
                    return [{'ref': 'refs/tags/v1', 'object': {'type': 'tag', 'sha': 'a' * 40}}]
                if url.endswith('/assets'):
                    return src.pages(url)
                return src_request(method, url, body)
            def destination_request(method, url, body=None):
                if '/git/ref/tags/' in url:
                    return {'ref': 'refs/tags/v1', 'object': {'type': 'tag', 'sha': 'a' * 40}}
                return dst_request(method, url, body)
            src.request, dst.request = source_request, destination_request
            mirror_releases(source, 'repo', 'team', src, dst, UP, dst.target, 'marker', initializing=False)
            self.assertEqual(api.release['body'], 'notes')

    def test_empty_forgejo_target_missing_or_mismatched_tags_never_write(self):
        from test_forgejo_space_body_integration import missing
        source = {'type': 'forgejo', 'base_url': 'https://forge.example', 'owner': 'alice'}
        for draft in (False, True):
            for failure in ('missing-source', 'missing-destination', 'mismatch'):
                with self.subTest(draft=draft, failure=failure), patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'}):
                    src, api = Source(body='notes', draft=draft), GitHubAPI(existing=False)
                    src.release['target_commitish'] = ''
                    dst = GitHubShapedDestination('team/project', 'token', client=api)
                    source_request, destination_request = src.request, dst.request
                    def source_ref(method, url, body=None):
                        if '/git/refs/tags/' in url:
                            if failure == 'missing-source': raise missing(url)
                            return [{'ref': 'refs/tags/v1', 'object': {'type': 'commit', 'sha': 'a' * 40}}]
                        return source_request(method, url, body)
                    def destination_ref(method, url, body=None):
                        if '/git/ref/tags/' in url:
                            if failure == 'missing-destination': raise missing(url)
                            return {'ref': 'refs/tags/v1', 'object': {'type': 'commit', 'sha': ('b' if failure == 'mismatch' else 'a') * 40}}
                        return destination_request(method, url, body)
                    src.request, dst.request = source_ref, destination_ref
                    with self.assertRaises(Exception):
                        mirror_releases(source, 'repo', 'team', src, dst, UP, dst.target, 'marker', initializing=False)
                    self.assertFalse(any(method != 'GET' for method, _, _ in api.calls))

    def test_empty_forgejo_target_create_and_draft_preserve_payload_and_detect_drift(self):
        source = {'type': 'forgejo', 'base_url': 'https://forge.example', 'owner': 'alice'}
        for draft in (False, True):
            for drift in (False, True):
                with self.subTest(draft=draft, drift=drift), patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'}):
                    src, api = Source(body='notes', draft=draft), GitHubAPI(existing=False)
                    src.release['target_commitish'] = ''
                    dst = GitHubShapedDestination('team/project', 'token', client=api)
                    source_request, destination_request = src.request, dst.request
                    def source_ref(method, url, body=None):
                        if '/git/refs/tags/' in url:
                            return [{'ref': 'refs/tags/v1', 'object': {'type': 'tag', 'sha': 'a' * 40}}]
                        if url.endswith('/assets'): return src.pages(url)
                        return source_request(method, url, body)
                    def destination_ref(method, url, body=None):
                        if '/git/ref/tags/' in url:
                            return {'ref': 'refs/tags/v1', 'object': {'type': 'tag', 'sha': 'a' * 40}}
                        return destination_request(method, url, body)
                    src.request, dst.request = source_ref, destination_ref
                    if drift:
                        upload = api.upload
                        def changed(*args, **kwargs):
                            result = upload(*args, **kwargs)
                            src.release['target_commitish'] = None
                            return result
                        api.upload = changed
                        with self.assertRaisesRegex(ValueError, 'source release changed during sync'):
                            mirror_releases(source, 'repo', 'team', src, dst, UP, dst.target, 'marker', initializing=False)
                        self.assertIn('[repo-mirror:pending:v3]', api.release['body'])
                    else:
                        mirror_releases(source, 'repo', 'team', src, dst, UP, dst.target, 'marker', initializing=False)
                        self.assertEqual(api.release['draft'], draft)
                    writes = [body for method, _, body in api.calls if method in ('POST', 'PATCH')]
                    self.assertTrue(any(method == 'POST' for method, _, _ in api.calls))
                    self.assertTrue(all('target_commitish' not in body for body in writes))

    def test_tag_change_during_copy_keeps_pending_marker(self):
        source = {'type': 'forgejo', 'base_url': 'https://forge.example', 'owner': 'alice'}
        with patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'synthetic-test-only-secret'}):
            src, api = Source(body='notes'), GitHubAPI()
            src.release['target_commitish'] = ''
            dst = GitHubShapedDestination('team/project', 'token', client=api)
            source_request, destination_request = src.request, dst.request
            changed = [False]
            def source_ref(method, url, body=None):
                if '/git/refs/tags/' in url:
                    return [{'ref': 'refs/tags/v1', 'object': {'type': 'tag', 'sha': ('b' if changed[0] else 'a') * 40}}]
                if url.endswith('/assets'): return src.pages(url)
                return source_request(method, url, body)
            def destination_ref(method, url, body=None):
                if '/git/ref/tags/' in url:
                    return {'ref': 'refs/tags/v1', 'object': {'type': 'tag', 'sha': ('b' if changed[0] else 'a') * 40}}
                return destination_request(method, url, body)
            src.request, dst.request = source_ref, destination_ref
            upload = api.upload
            def drift(*args, **kwargs):
                result = upload(*args, **kwargs)
                changed[0] = True
                return result
            api.upload = drift
            with self.assertRaisesRegex(ValueError, 'tag verification failed'):
                mirror_releases(source, 'repo', 'team', src, dst, UP, dst.target, 'marker', initializing=False)
            self.assertIn('[repo-mirror:pending:v3]', api.release['body'])

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
            self.assertEqual(api.release['body'], '')

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
