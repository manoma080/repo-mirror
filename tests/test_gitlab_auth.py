"""Optional authenticated reads for the public gitlab.com source."""
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from repo_mirror.gitlab_source_adapter import GitLabSourceAdapter, fetch_group_projects


class _Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(json.dumps(body).encode())
        self.headers = headers or {}
        self.status = 200

    def geturl(self):
        return 'https://gitlab.com/api/v4/projects/1'


@contextmanager
def _capture(seen):
    @contextmanager
    def fake_open(request, timeout=None):
        seen.append(request)
        yield _Response({'id': 1, 'visibility': 'public', 'path_with_namespace': 'g/p',
                         'http_url_to_repo': 'https://gitlab.com/g/g/p.git',
                         'tag_name': 'v1', 'released_at': '2025-01-01T00:00:00Z',
                         'assets': {'links': []}})
    yield fake_open


class GitLabTokenTests(unittest.TestCase):
    def _adapter(self, origin, token):
        adapter = GitLabSourceAdapter(origin, 'g/p', 1, token=token)
        seen = []
        adapter.opener = _FakeOpener(seen, origin)  # type: ignore[assignment]
        return adapter, seen

    def test_gitlab_com_sends_private_token_on_api_reads(self):
        adapter, seen = self._adapter('https://gitlab.com', 'glpat-example')
        adapter.project()
        self.assertEqual(seen[0].get_header('Private-token'), 'glpat-example')

    def test_self_hosted_ignores_supplied_token(self):
        adapter, seen = self._adapter('https://gitlab.example', 'glpat-example')
        adapter.project()
        self.assertIsNone(seen[0].get_header('Private-token'))

    def test_invalid_token_rejected(self):
        for token in ('', '  ', 'a b', 5):
            with self.subTest(token=token), self.assertRaises(ValueError):
                GitLabSourceAdapter('https://gitlab.com', 'g/p', 1, token=token)

    def test_group_discovery_uses_token_only_for_gitlab_com(self):
        seen = []
        fakes = iter([_FakeOpener(seen, 'https://gitlab.com'), _FakeOpener(seen, 'https://gitlab.example')])
        with patch('repo_mirror.gitlab_source_adapter.build_opener', side_effect=lambda *a, **k: next(fakes)):
            fetch_group_projects('https://gitlab.com', 'g', 'glpat-example')
            fetch_group_projects('https://gitlab.example', 'g', 'glpat-example')
        self.assertEqual(seen[0].get_header('Private-token'), 'glpat-example')
        self.assertIsNone(seen[1].get_header('Private-token'))

    def test_empty_env_gitlab_token_mirrors_gitlab_com_without_raising(self):
        from repo_mirror import repository
        from repo_mirror.sources import source_marker

        source = {
            'type': 'gitlab',
            'base_url': 'https://gitlab.com',
            'owner': 'g',
            'mirror': {'source_id': 1, 'destination_id': 9},
        }
        destination = {
            'id': 9,
            'description': source_marker(source, 'p'),
            'default_branch': 'main',
        }
        seen = []

        class _Dst:
            def request(self, method, url, data=None, **kwargs):
                if '/releases' in url:
                    return []
                return destination

        with patch.dict(os.environ, {'MIRROR_GITLAB_TOKEN': ''}, clear=False), \
             patch('repo_mirror.gitlab_source_adapter.build_opener',
                   return_value=_FakeOpener(seen, 'https://gitlab.com')), \
             patch('repo_mirror.repository.mirror_git') as git:
            repository.mirror_repository(source, 'p', 'dest', None, _Dst(), 'dest-token')

        self.assertEqual(git.call_args.args[0], 'https://gitlab.com/g/p.git')
        self.assertTrue(seen)
        self.assertTrue(all(req.get_header('Private-token') is None for req in seen))


class _FakeOpener:
    def __init__(self, seen, origin='https://gitlab.com'):
        self.seen = seen
        self.origin = origin

    @contextmanager
    def open(self, request, timeout=None):
        self.seen.append(request)
        body = {'id': 1, 'visibility': 'public', 'path_with_namespace': 'g/p',
                'http_url_to_repo': self.origin + '/g/p.git', 'default_branch': 'main'}
        url = request.full_url
        if '/groups/' in url or '/releases' in url:
            body = []
        response = _Response(body)
        response.geturl = lambda: url.split('/api/')[0] + '/api/v4/x'
        yield response


class ForgejoTokenFileTests(unittest.TestCase):
    def test_owner_only_single_line_required(self):
        from repo_mirror.forgejo_sync import read_owner_secret
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'token')
            with open(path, 'w') as stream:
                stream.write('glpat-example\n')
            os.chmod(path, 0o600)
            self.assertEqual(read_owner_secret(path, 'GitLab token'), 'glpat-example')
            os.chmod(path, 0o644)
            with self.assertRaises(ValueError):
                read_owner_secret(path, 'GitLab token')


if __name__ == '__main__':
    unittest.main()
