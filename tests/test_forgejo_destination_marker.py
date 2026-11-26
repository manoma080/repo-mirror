"""The Forgejo destination marker uses its own format; a move must still verify."""
import unittest

from repo_mirror.forgejo_git import _source_marker
from repo_mirror.forgejo_migration import MigrationRefused, _ForgejoPolicy

SECRET = 'k' * 40


class Repository:
    def __init__(self, repos):
        self.repos = repos
        self.renames = []

    def get(self, name):
        return self.repos.get(name)

    def rename(self, old, new):
        self.renames.append((old, new))
        self.repos[new] = self.repos.pop(old)


def entry(marker):
    return {
        'source': {'origin': 'https://github.com', 'id': 1461542642,
                   'owner': 'manoma080', 'name': 'mirror-manoma080-test-repo'},
        'destination': {'profile': 'forgejo-home', 'owner': 'labadmin',
                        'name': 'mirror-manoma080-test-repo', 'id': 26},
        'recovery': {'source_origin': 'https://github.com', 'source_owner': 'manoma080',
                     'source_name': 'mirror-fixture-owner-test-repo',
                     'destination_name': 'mirror-fixture-owner-test-repo',
                     'signature': 'c0ffee'},
        'config': {'path': 'config-forgejo.json', 'base_sha': 'a' * 40},
        'phase': 'prepared',
    }


def repository(marker):
    return Repository({'mirror-fixture-owner-test-repo': {
        'id': 26, 'full_name': 'labadmin/mirror-fixture-owner-test-repo',
        'private': True, 'description': marker}})


class ForgejoDestinationMarkerTests(unittest.TestCase):
    def setUp(self):
        self.policy = _ForgejoPolicy({}, 3300, 'labadmin', SECRET)

    def test_forgejo_marker_from_the_original_binding_is_accepted(self):
        marker = _source_marker('manoma080/mirror-fixture-owner-test-repo')
        repo = repository(marker + ' [repo-mirror:initialized:v1]')
        self.policy._verify_repo(repo, entry(marker), 'mirror-fixture-owner-test-repo')

    def test_github_style_marker_is_still_rejected(self):
        marker = '[repo-mirror: github:https://github.com/manoma080/mirror-fixture-owner-test-repo]'
        with self.assertRaises(MigrationRefused):
            self.policy._verify_repo(repository(marker), entry(marker),
                                     'mirror-fixture-owner-test-repo')

    def test_wrong_id_or_public_destination_is_rejected(self):
        marker = _source_marker('manoma080/mirror-fixture-owner-test-repo')
        repo = repository(marker)
        repo.repos['mirror-fixture-owner-test-repo']['private'] = False
        with self.assertRaises(MigrationRefused):
            self.policy._verify_repo(repo, entry(marker), 'mirror-fixture-owner-test-repo')


if __name__ == '__main__':
    unittest.main()
