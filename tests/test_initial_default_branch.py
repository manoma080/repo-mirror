import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from repo_mirror import git_sync, repository


class InitialDefaultTests(unittest.TestCase):
    def test_empty_repository_github_selects_another_pushed_branch(self):
        source = {"type": "github", "base_url": "https://github.com", "owner": "alice"}
        src = Mock()
        def source_request(method, url):
            if url.endswith('/releases/latest'):
                raise HTTPError(url, 404, '', {}, None)
            return {"clone_url": "https://github.com/alice/demo.git", "default_branch": "main"}
        src.request.side_effect = source_request
        src.pages.return_value = []
        state = {"default_branch": "configured", "description": repository.source_marker(source, "demo") + " [repo-mirror:initializing:v1]"}
        dst = Mock()
        def request(method, url, data=None):
            if method == 'PATCH':
                state.update(data)
            return dict(state)
        dst.request.side_effect = request
        desired = {"refs/heads/main": "a" * 40, "refs/heads/other": "b" * 40}
        def push(*args):
            state['default_branch'] = 'other'
        def mirror(*args, **kwargs):
            safety = kwargs['safety']
            with patch.object(git_sync, 'capture_snapshot', side_effect=[desired, {}]), patch.object(git_sync, 'push_snapshot', side_effect=push):
                git_sync.synchronize_snapshots(Mock(), Mock(), 'store', 'src', 'dst', safety['source_default'], safety['dest_default'], safety['archive_factory'], safety['switch_default'])
        with patch.object(repository, 'mirror_git', side_effect=mirror):
            repository.mirror_repository(source, 'demo', 'dest', src, dst, 'token')
        self.assertEqual(state['default_branch'], 'main')
        self.assertIn(('PATCH', {'default_branch': 'main'}), [(c.args[0], c.args[2]) for c in dst.request.call_args_list if c.args[0] == 'PATCH'])
