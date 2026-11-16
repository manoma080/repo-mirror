import unittest
from repo_mirror.forgejo_git import APIError, ForgejoDestination


class DestinationAPI:
    def __init__(self, repo):
        self.repo = repo
        self.methods = []

    def request(self, method, path, payload=None):
        self.methods.append(method)
        if method != 'GET':
            raise AssertionError('unexpected mutation')
        if self.repo is None:
            raise APIError(404)
        return dict(self.repo)


class PinnedForgejoDestinationTests(unittest.TestCase):
    def test_missing_or_replaced_pinned_target_is_not_created_or_modified(self):
        for repo in (None, {'id': 44, 'full_name': 'mirror/project', 'private': True,
                             'has_actions': True, 'description': 'repo-mirror-source: github.com/manoma080/project'}):
            with self.subTest(repo=repo):
                api = DestinationAPI(repo)
                with self.assertRaises(RuntimeError):
                    ForgejoDestination(api, 'mirror').begin_initialization('project', 'manoma080/project', expected_id=42)
                self.assertEqual(api.methods, ['GET'])
