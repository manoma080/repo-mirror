import unittest
from unittest.mock import Mock, patch

from repo_mirror.repository import mirror_repository, _IdentityGuardedDestination
from repo_mirror.sources import source_marker


class ReleaseIdentityGuardTests(unittest.TestCase):
    def test_each_destination_operation_checks_before_delegating(self):
        for method, args in [('request', ('DELETE', '/asset')), ('pages', ('/releases',)),
                             ('download', ('/asset', object())), ('upload', ('/upload', object(), 'x'))]:
            with self.subTest(method=method):
                dst = Mock()
                check = Mock(side_effect=ValueError('changed'))
                guarded = _IdentityGuardedDestination(dst, check)
                with self.assertRaisesRegex(ValueError, 'changed'):
                    getattr(guarded, method)(*args)
                getattr(dst, method).assert_not_called()

    def test_initialization_marker_is_not_patched_after_source_replacement(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice',
                  'repos': ['repo'], 'destinations': {'repo': {'name': 'repo', 'source_id': 1, 'destination_id': 2}}}
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

    def test_destination_change_between_release_operations_blocks_next_read(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice',
                  'repos': ['repo'], 'destinations': {'repo': {'name': 'repo', 'source_id': 1, 'destination_id': 2}}}
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
            guarded.request('GET', target + '/releases/1')
            state['id'] = 3
            guarded.request('GET', target + '/releases/2')
        with patch('repo_mirror.repository.mirror_git'), patch('repo_mirror.repository.mirror_releases', releases):
            with self.assertRaisesRegex(ValueError, 'destination repository id mismatch'):
                mirror_repository(source, 'repo', 'dest', src, dst, 'token')
        self.assertFalse(any(c.args[1].endswith('/releases/2') for c in dst.request.call_args_list))
