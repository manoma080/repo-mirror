import io
import unittest
from urllib.error import HTTPError

from repo_mirror import repository


class SharedReleaseEntryTests(unittest.TestCase):
    def test_release_policy_can_run_without_git_or_repository_creation(self):
        class Source:
            def pages(self, url):
                assert url == 'https://source/repos/alice/demo/releases'
                return []

            def request(self, method, url):
                assert url.endswith('/releases/latest')
                raise HTTPError(url, 404, '', {}, io.BytesIO())

        class Destination:
            def request(self, *args):
                raise AssertionError('no destination writes for an empty release set')

        repository.mirror_releases(
            {'type': 'github', 'owner': 'alice'}, 'demo', 'dest', Source(), Destination(),
            'https://source/repos/alice/demo', 'https://destination/repos/dest/demo',
            'source-marker', initializing=False,
        )

    def test_duplicate_source_tags_abort_before_destination_access(self):
        class Source:
            def pages(self, url):
                return [{'id': 1, 'tag_name': 'v1'}, {'id': 2, 'tag_name': 'v1'}]

            def request(self, method, url):
                raise HTTPError(url, 404, '', {}, io.BytesIO())

        class Destination:
            def __getattr__(self, name):
                raise AssertionError('destination must not be accessed')

        with self.assertRaisesRegex(ValueError, 'duplicate source release tag'):
            repository.mirror_releases(
                {'type': 'github', 'owner': 'alice'}, 'demo', 'dest', Source(), Destination(),
                'https://source/repos/alice/demo', 'https://destination/repos/dest/demo',
                'source-marker', initializing=False,
            )

    def test_destination_discovery_failure_does_not_create_release(self):
        class Source:
            def pages(self, url):
                return [{'id': 1, 'tag_name': 'v1'}]

            def request(self, method, url):
                if url.endswith('/releases/1'):
                    return {'id': 1, 'tag_name': 'v1'}
                raise HTTPError(url, 404, '', {}, io.BytesIO())

        class Destination:
            writes = []

            def request(self, method, url, data=None):
                if method == 'GET':
                    raise HTTPError(url, 404, '', {}, io.BytesIO())
                self.writes.append((method, url))
                raise AssertionError('unexpected destination write')

            def pages(self, url):
                raise RuntimeError('destination listing unavailable')

        dst = Destination()
        with self.assertRaisesRegex(RuntimeError, 'destination listing unavailable'):
            repository.mirror_releases(
                {'type': 'github', 'owner': 'alice'}, 'demo', 'dest', Source(), dst,
                'https://source/repos/alice/demo', 'https://destination/repos/dest/demo',
                'source-marker', initializing=False,
            )
        self.assertEqual(dst.writes, [])

    def test_source_discovery_failure_aborts_before_destination_access(self):
        class Source:
            def pages(self, url):
                raise RuntimeError('source listing unavailable')

        class Destination:
            def __getattr__(self, name):
                raise AssertionError('destination must not be accessed')

        with self.assertRaisesRegex(RuntimeError, 'source listing unavailable'):
            repository.mirror_releases(
                {'type': 'github', 'owner': 'alice'}, 'demo', 'dest', Source(), Destination(),
                'https://source/repos/alice/demo', 'https://destination/repos/dest/demo',
                'source-marker', initializing=False,
            )
