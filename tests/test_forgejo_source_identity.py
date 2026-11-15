import unittest

from repo_mirror.forgejo_sync import parse_source
from repo_mirror.forgejo_local import forgejo_source_identity


class ForgejoSourceIdentityTests(unittest.TestCase):
    def test_public_forgejo_identity_includes_host(self):
        self.assertNotEqual(forgejo_source_identity('https://codeberg.org/alice/project'),
                            forgejo_source_identity('https://forge.example/alice/project'))

    def test_github_identity_remains_unchanged(self):
        self.assertEqual(parse_source('https://github.com/manoma080/mirror-demo'),
                         'manoma080/mirror-demo')

    def test_rejects_credentials_query_and_github_policy_bypass(self):
        for url in ('https://user:password@codeberg.org/alice/project',
                    'https://codeberg.org/alice/project?token=secret',
                    'https://github.com/alice/project'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                forgejo_source_identity(url)
