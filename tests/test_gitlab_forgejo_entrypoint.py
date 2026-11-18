import tempfile
import unittest
from unittest.mock import patch

from repo_mirror.forgejo_git import _source, _source_url, _source_marker
from scheduler.auto_update import valid_manifest


class GitLabForgejoEntrypointTests(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.tmp_path = temp_dir.name

    def patch_object(self, target, name, value):
        patcher = patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_git_helper_preserves_nested_gitlab_project_identity(self):
        url = 'https://gitlab.com/group/subgroup/project'
        assert _source(url) == ('group/subgroup', 'project')
        assert _source_url(url) == url + '.git'
        assert _source_marker(url) == 'repo-mirror-source: gitlab.com/group/subgroup/project'


    def test_launcher_accepts_explicit_pinned_nested_gitlab(self):
        manifest = {'destinations': {'local': {}}, 'repositories': [
            {'source': {'type': 'gitlab', 'url': 'https://gitlab.com/group/subgroup/project', 'id': 42},
             'mirrors': {'local': {'name': 'project'}}}]}
        assert valid_manifest(manifest)


    def test_worker_runs_pinned_gitlab_via_real_release_engine(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        import repo_mirror.gitlab_source_adapter as gl
        import repo_mirror.forgejo_sync as worker
        events = []
        class FakeSource:
            release_identity_kind = 'tag'
            release_scope = ('https://gitlab.com', 42)
            pinned_base = 'https://gitlab.com/api/v4/projects/42'
            def __init__(self, origin, path, project_id):
                assert (origin, path, project_id) == ('https://gitlab.com', 'group/subgroup/project', 42)
            def project(self):
                return {'id': 42, 'visibility': 'public', 'web_url': 'https://gitlab.com/group/subgroup/project', 'path_with_namespace': 'group/subgroup/project', 'http_url_to_repo': 'https://gitlab.com/group/subgroup/project.git'}
            def list_releases(self):
                events.append('releases')
                return []
            def verify_release_tag(self, tag, destination, target):
                raise AssertionError('no releases expected')
        self.patch_object(gl, 'GitLabSourceAdapter', FakeSource)
        self.patch_object(worker, 'sync_refs', lambda *a, **k: events.append(('refs', k['source_token'])))
        self.patch_object(worker, 'snapshot_refs', lambda *a, **k: (None, {}, {}))
        adapter = Mock(target='target')
        self.patch_object(worker, 'ForgejoDestinationAdapter', lambda *a, **k: adapter)
        dest = Mock(run_pin=None)
        dest.begin_initialization.return_value = False
        url = 'https://gitlab.com/group/subgroup/project'
        entry = {'url': url, 'identity': url, 'source_id': 42, 'destination_name': 'project'}
        assert worker._sync_requested(SimpleNamespace(port=3300, owner='local'), [url], dest, 'github-secret', 'local', 'signing-secret', self.tmp_path, mappings={url: entry}) == 0
        assert ('refs', None) in events
        assert events.index(('refs', None)) < len(events) - 1


    def test_worker_rejects_unpinned_gitlab_before_destination_write(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from repo_mirror.forgejo_sync import _sync_requested
        dest = Mock()
        url = 'https://gitlab.com/group/subgroup/project'
        assert _sync_requested(SimpleNamespace(port=3300, owner='local'), [url], dest, 'secret', 'local', 'signing', self.tmp_path) == 1
        dest.begin_initialization.assert_not_called()


    def test_worker_rejects_ambiguous_gitlab_release_times_before_write(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        import repo_mirror.gitlab_source_adapter as gl
        from repo_mirror.forgejo_sync import _sync_requested
        class Source:
            def __init__(self, *args): pass
            def project(self):
                return {'id': 42, 'web_url': 'https://gitlab.com/g/p', 'path_with_namespace': 'g/p'}
            def list_releases(self):
                return [{'tag_name': t, 'released_at': '2025-01-01T00:00:00Z'} for t in ('a', 'b')]
        self.patch_object(gl, 'GitLabSourceAdapter', Source)
        dest = Mock()
        url = 'https://gitlab.com/g/p'
        entry = {'url': url, 'identity': url, 'source_id': 42, 'destination_name': 'p'}
        assert _sync_requested(SimpleNamespace(port=3300, owner='local'), [url], dest, None, 'local', 'signing', self.tmp_path, mappings={url: entry}) == 1
        dest.begin_initialization.assert_not_called()


    def test_git_helper_rejects_malformed_nested_host(self):
        with self.assertRaises(ValueError):
            _source_url('https://-bad.example/group/sub/project')


    def test_git_helper_preserves_forgejo_default_port_normalization(self):
        assert _source_url('https://code.example:443/a/b') == 'https://code.example/a/b.git'


    def test_worker_uses_real_gitlab_adapter_and_engine_with_network_mocked(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from repo_mirror.gitlab_source_adapter import GitLabSourceAdapter
        import repo_mirror.forgejo_sync as worker
        requests = []
        url = 'https://gitlab.com/group/subgroup/project'
        def get(self, endpoint):
            requests.append(endpoint)
            if '/releases?' in endpoint:
                return [], {}
            return {'id': 42, 'visibility': 'public', 'web_url': url, 'path_with_namespace': 'group/subgroup/project', 'http_url_to_repo': url + '.git'}, {}
        self.patch_object(GitLabSourceAdapter, '_get', get)
        self.patch_object(worker, 'sync_refs', lambda *a, **k: requests.append('git-sync'))
        self.patch_object(worker, 'snapshot_refs', lambda *a, **k: (None, {}, {}))
        self.patch_object(worker, 'ForgejoDestinationAdapter', lambda *a, **k: Mock(target='target'))
        dest = Mock(run_pin=None)
        dest.begin_initialization.return_value = False
        entry = {'url': url, 'identity': url, 'source_id': 42, 'destination_name': 'project'}
        assert worker._sync_requested(SimpleNamespace(port=3300, owner='local'), [url], dest, 'github-secret', 'local', 'signing-secret', self.tmp_path, mappings={url: entry}) == 0
        assert any('/projects/42/releases?' in x for x in requests)
        assert requests.index('git-sync') < max(i for i, x in enumerate(requests) if '/releases?' in x)


    def test_gitlab_worker_tag_verification_uses_git_snapshot_not_forgejo_git_api(self):
        from repo_mirror.gitlab_source_adapter import GitLabSourceAdapter
        from repo_mirror.forgejo_sync import gitlab_tag_verifier
        src = GitLabSourceAdapter('https://gitlab.com', 'g/p', 42)
        sha = 'a' * 40
        def get(self, url):
            if '/repository/tags/' in url:
                return {'name': 'v1', 'target': sha, 'message': None, 'commit': {'id': sha}}, {}
            return {'id': 42, 'visibility': 'public', 'path_with_namespace': 'g/p'}, {}
        self.patch_object(GitLabSourceAdapter, '_get', get)
        verify = gitlab_tag_verifier(src, lambda tag: ('git-ref', sha))
        assert verify('v1', object(), 'target') == ('commit', sha)

    def test_gitlab_pipeline_passes_pinned_project_api_upstream(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        import repo_mirror.forgejo_sync as worker
        import repo_mirror.gitlab_source_adapter as gl
        url = 'https://gitlab.com/g/p'
        class Source:
            pinned_base = 'https://gitlab.com/api/v4/projects/42'
            def __init__(self, *args): pass
            def project(self):
                return {'id': 42, 'visibility': 'public', 'web_url': url, 'path_with_namespace': 'g/p'}
            def list_releases(self): return []
            def verify_release_tag(self, *args): pass
        self.patch_object(gl, 'GitLabSourceAdapter', Source)
        self.patch_object(worker, 'sync_refs', lambda *a, **k: None)
        self.patch_object(worker, 'snapshot_refs', lambda *a, **k: (None, {}, {}))
        self.patch_object(worker, 'ForgejoDestinationAdapter', lambda *a, **k: Mock(target='target'))
        mirror = Mock()
        self.patch_object(worker, 'mirror_releases', mirror)
        dest = Mock(run_pin=None)
        entry = {'url': url, 'identity': url, 'source_id': 42, 'destination_name': 'p'}
        self.assertEqual(worker._sync_requested(SimpleNamespace(port=3300, owner='local'), [url], dest, None, 'local', 'secret', self.tmp_path, mappings={url: entry}), 0)
        self.assertEqual(mirror.call_args.args[5], Source.pinned_base)

    def test_asset_limit_is_positive_bounded_decimal(self):
        from repo_mirror.forgejo_sync import parse_asset_max_bytes
        self.assertEqual(parse_asset_max_bytes('123'), 123)
        for value in ('0', '-1', 'abc', '2147483648', ' 1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_asset_max_bytes(value)

    def test_nonempty_linked_release_runs_real_worker_and_engine(self):
        from contextlib import contextmanager
        from io import BytesIO
        from types import SimpleNamespace
        from unittest.mock import Mock
        from repo_mirror.gitlab_assets import Download
        from repo_mirror.gitlab_source_adapter import GitLabSourceAdapter
        from tests.test_forgejo_space_body_integration import GitHubAPI, GitHubShapedDestination
        import repo_mirror.forgejo_sync as worker
        url = 'https://gitlab.com/g/p'
        sha = 'a' * 40
        item = {'tag_name': 'v1', 'name': 'title', 'description': 'notes', 'released_at': '2025-01-01T00:00:00Z', 'assets': {'links': [{'id': 5, 'name': 'file', 'url': 'https://example.com/file', 'link_type': 'other'}]}}
        def get(self, endpoint):
            if '/repository/tags/' in endpoint:
                return {'name': 'v1', 'target': sha, 'message': None, 'commit': {'id': sha}}, {}
            if '/releases?' in endpoint: return [item], {}
            if '/releases/' in endpoint: return item, {}
            return {'id': 42, 'visibility': 'public', 'web_url': url, 'path_with_namespace': 'g/p', 'http_url_to_repo': url + '.git'}, {}
        self.patch_object(GitLabSourceAdapter, '_get', get)
        self.patch_object(worker, 'sync_refs', lambda *a, **k: None)
        refs = {'refs/tags/v1': sha}
        self.patch_object(worker, 'snapshot_refs', lambda *a, **k: (None, refs, refs))
        api = GitHubAPI(existing=False)
        dst = GitHubShapedDestination('team/project', 'token', client=api)
        dst.supports_target_commitish = True
        self.patch_object(worker, 'ForgejoDestinationAdapter', lambda *a, **k: dst)
        limits = []
        @contextmanager
        def download(url, cap):
            limits.append(cap)
            yield Download(False, BytesIO(b'ok'), 2)
        from repo_mirror import gitlab_assets
        self.patch_object(gitlab_assets, 'download_asset', download)
        dest = Mock(run_pin=None)
        dest.begin_initialization.return_value = True
        entry = {'url': url, 'identity': url, 'source_id': 42, 'destination_name': 'p'}
        with patch.dict('os.environ', {'MIRROR_FORGEJO_ASSET_MAX_BYTES': '123'}):
            result = worker._sync_requested(SimpleNamespace(port=3300, owner='local'), [url], dest, None, 'token', 'secret', self.tmp_path, mappings={url: entry})
        self.assertEqual(result, 0)
        self.assertTrue(limits)
        self.assertEqual(set(limits), {123})
        self.assertEqual(list(api.assets.values()), [('file', b'ok')])

    def test_scheduler_refuses_gitlab_without_limit_before_lock(self):
        import json
        from pathlib import Path
        from repo_mirror.forgejo_schedule import main
        root = Path(self.tmp_path)
        manifest = {'destinations': {'local': {}}, 'repositories': [
            {'source': {'type': 'gitlab', 'url': 'https://gitlab.com/g/p', 'id': 42},
             'mirrors': {'local': {'name': 'p'}}}]}
        (root / 'manifest').write_text(json.dumps(manifest))
        (root / 'secret').write_text('x' * 32)
        (root / 'secret').chmod(0o600)
        result = main(['run', '--manifest', str(root / 'manifest'), '--token-file', str(root / 'token'),
                       '--timestamp-secret-file', str(root / 'secret'), '--journal-dir', str(root),
                       '--owner', 'local', '--lock', str(root / 'lock')])
        self.assertEqual(result, 2)
        self.assertFalse((root / 'lock').exists())

    def test_group_all_is_explicitly_refused_before_lock(self):
        import json
        import io
        from contextlib import redirect_stderr
        from pathlib import Path
        from repo_mirror.forgejo_schedule import main
        root = Path(self.tmp_path)
        (root / 'manifest').write_text(json.dumps({'sources': [{'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'g', 'repos': 'all'}]}))
        output = io.StringIO()
        with redirect_stderr(output):
            result = main(['run', '--manifest', str(root / 'manifest'), '--token-file', str(root / 'token'),
                           '--timestamp-secret-file', str(root / 'secret'), '--journal-dir', str(root),
                           '--owner', 'local', '--lock', str(root / 'lock'), '--asset-max-bytes', '123'])
        self.assertEqual(result, 2)
        self.assertIn('GitLab group discovery is not supported', output.getvalue())
        self.assertFalse((root / 'lock').exists())

    def test_scheduler_forwards_asset_policy_to_worker(self):
        import json
        import os
        import subprocess
        import sys
        from pathlib import Path
        root = Path(self.tmp_path)
        secret = root / 'secret'; secret.write_text('x' * 32); secret.chmod(0o600)
        journals = root / 'journals'; journals.mkdir(mode=0o700)
        manifest = {'destinations': {'local': {}}, 'repositories': [
            {'source': {'type': 'gitlab', 'url': 'https://gitlab.com/g/p', 'id': 42},
             'mirrors': {'local': {'name': 'p'}}}]}
        path = root / 'manifest'; path.write_text(json.dumps(manifest))
        worker = root / 'worker'; worker.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGS"\n'); worker.chmod(0o700)
        cmd = [sys.executable, '-m', 'repo_mirror.forgejo_schedule', 'run', '--manifest', str(path),
               '--token-file', 'unused', '--timestamp-secret-file', str(secret), '--journal-dir', str(journals),
               '--owner', 'local', '--lock', str(root / 'lock'), '--sync-python', str(worker), '--asset-max-bytes', '123']
        result = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, 'ARGS': str(root / 'args')})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        argv = (root / 'args').read_text().splitlines()
        self.assertEqual(argv[argv.index('--asset-max-bytes') + 1], '123')
