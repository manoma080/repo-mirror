import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import mirror


class CloneIdentityTests(unittest.TestCase):
    def test_github_clone_identity_is_case_insensitive(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice'}
        _, _, accepted = self.run_mirror(source, 'https://github.com/Alice/Demo.git', True)
        self.assertTrue(accepted)

    def test_forgejo_clone_identity_remains_case_sensitive(self):
        source = {'type': 'forgejo', 'base_url': 'https://code.example', 'owner': 'alice'}
        _, _, accepted = self.run_mirror(source, 'https://code.example/Alice/demo.git', True)
        self.assertFalse(accepted)

    def run_mirror(self, source, clone_url, exists):
        calls = []

        class Source:
            def request(self, method, url):
                if url.endswith('/releases/latest'):
                    raise HTTPError(url, 404, '', {}, io.BytesIO())
                return {'clone_url': clone_url}

            def pages(self, url):
                return []

        class Destination:
            def request(self, method, url, data=None):
                calls.append((method, url))
                if method == 'GET':
                    if exists:
                        return {'description': mirror.source_marker(source, 'demo')}
                    raise HTTPError(url, 404, '', {}, io.BytesIO())
                if method == 'POST':
                    return {'full_name': 'dest/' + mirror.destination_name(source, 'demo')}
                return {}

            def pages(self, url):
                return []

        with patch.object(mirror, 'mirror_git') as git:
            try:
                mirror.mirror_repository(source, 'demo', 'dest', Source(), Destination(), 'token')
            except ValueError:
                return calls, git.call_count, False
            return calls, git.call_count, True

    def test_mismatched_clone_path_rejected_without_destination_writes(self):
        source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'alice'}
        for exists in (False, True):
            with self.subTest(exists=exists):
                calls, git_calls, accepted = self.run_mirror(source, 'https://github.com/mallory/other.git', exists)
                self.assertFalse(accepted)
                self.assertFalse([call for call in calls if call[0] != 'GET'])
                self.assertEqual(git_calls, 0)

    def test_unsafe_urls_rejected_without_destination_writes(self):
        source = {'type': 'forgejo', 'base_url': 'https://code.example/forge', 'owner': 'alice'}
        for clone in (
            'https://code.example/forge/alice/other.git',
            'https://code.example/alice/demo.git',
            'https://code.example/forge/alice/../alice/demo.git',
            'https://code.example/forge/alice/%64emo.git',
            'https://code.example/forge/alice/demo.git;alias',
            'https://code.example/forge/alice/demo.git?',
            'https://code.example/forge/alice/demo.git#fragment',
            'https://user@code.example/forge/alice/demo.git',
            'https://code.example:444/forge/alice/demo.git',
            'http://code.example/forge/alice/demo.git',
        ):
            with self.subTest(clone=clone):
                calls, git_calls, accepted = self.run_mirror(source, clone, True)
                self.assertFalse(accepted)
                self.assertFalse([call for call in calls if call[0] != 'GET'])
                self.assertEqual(git_calls, 0)

    def test_valid_github_and_forgejo_clone_paths(self):
        for source, clone in [
            ({'type': 'github', 'base_url': 'https://api.github.com', 'owner': 'alice'}, 'https://github.com/alice/demo.git'),
            ({'type': 'forgejo', 'base_url': 'https://code.example/forge', 'owner': 'alice'}, 'https://code.example/forge/alice/demo.git'),
        ]:
            with self.subTest(clone=clone):
                calls, git_calls, accepted = self.run_mirror(source, clone, False)
                self.assertTrue(accepted)
                self.assertEqual(git_calls, 1)
                self.assertTrue(any(method == 'POST' for method, _ in calls))
