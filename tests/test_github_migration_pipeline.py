"""End-to-end tests for the post-batch migration transaction.

These drive the real ``cli`` entry point against a fake GitHub Contents API so
the journal, the destination rename and the configuration publication are all
exercised through their production wiring.
"""
import base64
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from repo_mirror import cli
from repo_mirror.config_named import normalize_config
from repo_mirror.migrations import blob_sha
from repo_mirror.migrations_journal import parse

MANAGEMENT = 'manoma080/repo-mirror'
SECRET = 'fixture-signing-secret-0123456789abcdef'
API = 'https://api.github.com'
REPO_URL = API + '/repos/dest/'


def config_document(name='mirror-old-owner-repo', source_owner='old-owner',
                    source_name='repo', source_id=1, destination_id=2):
    return {
        'destinations': {'github-dest': {'account_type': 'personal'}},
        'repositories': [
            {'source': {'url': f'https://github.com/{source_owner}/{source_name}', 'id': source_id},
             'mirrors': {'github-dest': {'name': name, 'id': destination_id}}},
            {'source': {'url': 'https://github.com/other/keep', 'id': 11},
             'mirrors': {'github-dest': {'name': 'mirror-other-keep', 'id': 12}}},
        ],
    }


def relocation(name='mirror-old-owner-repo', owner='new-owner', repo='renamed'):
    return {
        'source_origin': API, 'source_id': 1,
        'original_owner': 'old-owner', 'original_name': 'repo',
        'owner': owner, 'name': repo,
        'destination': {'profile': 'github-dest', 'owner': 'dest', 'name': name, 'id': 2},
    }


class FakeGitHub:
    """Contents API plus destination repository endpoints, with blob CAS."""

    def __init__(self):
        self.files = {}
        self.repos = {}
        self.aliases = {}
        self.calls = []
        self.fail = None
        self.crash_after = None

    # -- setup ------------------------------------------------------------
    def seed_config(self, raw):
        self.files['config.json'] = raw

    def seed_repo(self, name, identifier):
        self.repos[name] = {'id': identifier, 'name': name, 'full_name': 'dest/' + name}

    # -- inspection -------------------------------------------------------
    def config(self):
        return self.files['config.json']

    def journal(self):
        return self.files.get('migrations.json')

    def pending(self):
        raw = self.journal()
        return {} if raw is None else parse(raw, SECRET).pending()

    def methods(self, method):
        return [call for call in self.calls if call[0] == method]

    # -- API --------------------------------------------------------------
    @staticmethod
    def _sha(raw):
        return blob_sha(raw)

    def _contents(self, raw):
        return {'type': 'file', 'encoding': 'base64', 'sha': self._sha(raw),
                'content': base64.b64encode(raw).decode('ascii')}

    def request(self, method, url, payload=None):
        payload = payload or {}
        self.calls.append((method, url, payload))
        if self.fail is not None and (method, url) == self.fail:
            self.fail = None
            raise RuntimeError('injected crash')
        if '/contents/' in url:
            return self._contents_request(method, url.split('/contents/', 1)[1].split('?', 1)[0],
                                          payload, url)
        if url.startswith(REPO_URL):
            return self._repo_request(method, url.rsplit('/', 1)[1], payload, url)
        raise AssertionError(f'unexpected request {method} {url}')

    def _contents_request(self, method, name, payload, url):
        if method == 'GET':
            raw = self.files.get(name)
            if raw is None:
                raise HTTPError(url, 404, 'Not Found', None, None)
            return self._contents(raw)
        if method != 'PUT':
            raise AssertionError(method)
        current = self.files.get(name)
        if current is None:
            if 'sha' in payload:
                raise HTTPError(url, 409, 'Conflict', None, None)
        elif payload.get('sha') != self._sha(current):
            raise HTTPError(url, 409, 'Conflict', None, None)
        raw = base64.b64decode(payload['content'])
        self.files[name] = raw
        return {'content': self._contents(raw)}

    def _repo_request(self, method, name, payload, url):
        if method == 'GET':
            if name in self.repos:
                return dict(self.repos[name])
            if name in self.aliases:
                return dict(self.repos[self.aliases[name]])
            raise HTTPError(url, 404, 'Not Found', None, None)
        if method != 'PATCH':
            raise AssertionError(method)
        if name not in self.repos:
            raise HTTPError(url, 404, 'Not Found', None, None)
        repo = {**self.repos.pop(name), 'name': payload['name'],
                'full_name': 'dest/' + payload['name']}
        self.repos[payload['name']] = repo
        self.aliases[name] = payload['name']
        if self.crash_after == (method, url):
            # Model a crash after the rename committed but before the caller
            # observed the response.
            self.crash_after = None
            raise RuntimeError('injected crash')
        return dict(repo)


ENVIRONMENT = {
    'MIRROR_TIMESTAMP_SECRET': SECRET,
    'GH_TOKEN': 'destination-token',
    'DESTINATION_OWNER': 'dest',
    'MIRROR_MANAGEMENT_REPOSITORY': MANAGEMENT,
    'MANAGEMENT_TOKEN': 'management-token',
}


def only_moved(move):
    """Report a relocation for the moved source only, like the real worker."""
    return move_map({'old-owner': move})


def move_map(moves):
    def mirror(source, name, *args, **kwargs):
        return moves.get(source['owner'])
    return mirror


def run_cli(directory, api, move=None, environment=None, mirror=None):
    config_path = Path(directory) / 'config.json'
    config_path.write_bytes(api.config())
    environment = dict(ENVIRONMENT if environment is None else environment)
    stderr = io.StringIO()
    # The second API instance is the destination credential: the one that can
    # see and rename the mirror repositories.
    with patch.dict('os.environ', environment, clear=True), \
            patch.object(cli, 'SourceAPI'), \
            patch.object(cli, 'API', side_effect=[Mock(), api]), \
            patch.object(cli, 'get_existing', return_value=None), \
            patch.object(cli, 'mirror_repository', side_effect=mirror or only_moved(move)), \
            patch('repo_mirror.github_migration.API', return_value=api), \
            redirect_stderr(stderr):
        try:
            result = cli.main(['--config', str(config_path)])
            error = None
        except Exception as exc:  # the batch reports failures by raising
            result, error = None, exc
    return error, result, stderr.getvalue()


class MigrationPipelineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.api = FakeGitHub()
        self.api.seed_config((json.dumps(config_document(), indent=2) + '\n').encode())
        self.api.seed_repo('mirror-old-owner-repo', 2)

    def published(self):
        return normalize_config(json.loads(self.api.config()))['repositories'][0]

    # -- success ----------------------------------------------------------
    def test_success_renames_destination_and_publishes_configuration(self):
        error, result, stderr = run_cli(self.directory, self.api, relocation())
        self.assertIsNone(error, stderr)
        self.assertEqual(self.api.repos['mirror-new-owner-renamed']['id'], 2)
        self.assertNotIn('mirror-old-owner-repo', self.api.repos)
        self.assertEqual(self.pending_count(), 0)
        source = self.published()
        self.assertEqual(source['owner'], 'new-owner')
        self.assertEqual(source['name'], 'renamed')
        settings = source['mirror']
        self.assertEqual(settings['name'], 'mirror-new-owner-renamed')
        self.assertEqual(settings['destination_id'], 2)
        self.assertEqual(settings['source_id'], 1)
        self.assertEqual(settings['legacy']['source_owner'], 'old-owner')
        self.assertEqual(settings['legacy']['source_name'], 'repo')
        self.assertEqual(settings['legacy']['destination_name'], 'mirror-old-owner-repo')
        self.assertRegex(settings['legacy']['signature'], r'^[0-9a-f]{64}$')
        # Every other entry is preserved byte for byte.
        self.assertIn(b'{\n      "source": {\n        "url": "https://github.com/other/keep",',
                      self.api.config())
        self.assertEqual(result[0][0], 1)
        self.assertIn('migration completed', stderr)

    def pending_count(self):
        return len(self.api.pending())

    # -- crash and resume -------------------------------------------------
    def test_crash_after_rename_then_resume_completes(self):
        self.api.crash_after = ('PATCH', REPO_URL + 'mirror-old-owner-repo')
        error, _, _ = run_cli(self.directory, self.api, relocation())
        self.assertIsNotNone(error)
        # The rename committed but the configuration was not published.
        self.assertIn('mirror-new-owner-renamed', self.api.repos)
        self.assertEqual(self.published()['owner'], 'old-owner')
        pending = self.api.pending()
        self.assertEqual([entry['phase'] for entry in pending.values()], ['synced'])

        error, result, stderr = run_cli(self.directory, self.api, relocation())
        self.assertIsNone(error, stderr)
        self.assertIn('migration recovery resumed=1', stderr)
        self.assertEqual(self.published()['owner'], 'new-owner')
        self.assertEqual(self.pending_count(), 0)
        self.assertEqual(result[0][1]['name'], 'renamed')

    # -- fail closed ------------------------------------------------------
    def test_configuration_changed_by_another_writer_stops(self):
        self.api.crash_after = ('PATCH', REPO_URL + 'mirror-old-owner-repo')
        run_cli(self.directory, self.api, relocation())
        # A second writer publishes an unrelated change.
        document = json.loads(self.api.config())
        document['repositories'].append(
            {'source': {'url': 'https://github.com/third/new', 'id': 31},
             'mirrors': {'github-dest': {'name': 'mirror-third-new', 'id': 32}}})
        other = (json.dumps(document, indent=2) + '\n').encode()
        self.api.seed_config(other)

        error, _, stderr = run_cli(self.directory, self.api, relocation())
        self.assertIsNotNone(error)
        self.assertIn('configuration changed by another writer', stderr + str(error))
        self.assertEqual(self.api.config(), other)
        self.assertEqual([entry['phase'] for entry in self.api.pending().values()], ['synced'])

    def test_destination_name_collision_stops_before_rename(self):
        self.api.seed_repo('mirror-new-owner-renamed', 999)
        original = self.api.config()
        error, _, stderr = run_cli(self.directory, self.api, relocation())
        self.assertIsNotNone(error)
        self.assertIn('destination name collision', stderr)
        self.assertEqual(self.api.repos['mirror-new-owner-renamed']['id'], 999)
        self.assertEqual(self.api.repos['mirror-old-owner-repo']['id'], 2)
        self.assertEqual(self.api.config(), original)
        self.assertEqual([entry['phase'] for entry in self.api.pending().values()], ['prepared'])

    def test_replaced_destination_pin_stops(self):
        self.api.repos['mirror-old-owner-repo'] = {
            'id': 77, 'name': 'mirror-old-owner-repo', 'full_name': 'dest/mirror-old-owner-repo'}
        error, _, stderr = run_cli(self.directory, self.api, relocation())
        self.assertIsNotNone(error)
        self.assertIn('destination pin replaced', stderr)
        self.assertIn('mirror-old-owner-repo', self.api.repos)

    # -- custom names -----------------------------------------------------
    def test_custom_destination_name_is_preserved(self):
        self.api.seed_config((json.dumps(config_document(name='copy'), indent=2) + '\n').encode())
        self.api.repos = {}
        self.api.seed_repo('copy', 2)
        error, _, stderr = run_cli(self.directory, self.api, relocation(name='copy'))
        self.assertIsNone(error, stderr)
        self.assertEqual(self.api.methods('PATCH'), [])
        self.assertIn('copy', self.api.repos)
        source = self.published()
        self.assertEqual(source['mirror']['name'], 'copy')
        self.assertEqual(source['mirror']['legacy']['destination_name'], 'copy')
        self.assertEqual(self.pending_count(), 0)

    def test_two_relocations_publish_sequentially(self):
        document = config_document()
        document['repositories'].append(
            {'source': {'url': 'https://github.com/second/moved2', 'id': 21},
             'mirrors': {'github-dest': {'name': 'mirror-second-moved2', 'id': 22}}})
        self.api.seed_config((json.dumps(document, indent=2) + '\n').encode())
        self.api.seed_repo('mirror-second-moved2', 22)
        second = {'source_origin': API, 'source_id': 21,
                  'original_owner': 'second', 'original_name': 'moved2',
                  'owner': 'third', 'name': 'moved3',
                  'destination': {'profile': 'github-dest', 'owner': 'dest',
                                  'name': 'mirror-second-moved2', 'id': 22}}
        error, _, stderr = run_cli(
            self.directory, self.api, mirror=move_map({'old-owner': relocation(), 'second': second}))
        self.assertIsNone(error, stderr)
        self.assertIn('mirror-new-owner-renamed', self.api.repos)
        self.assertIn('mirror-third-moved3', self.api.repos)
        published = json.loads(self.api.config())['repositories']
        self.assertEqual(published[0]['source']['url'], 'https://github.com/new-owner/renamed')
        self.assertEqual(published[2]['source']['url'], 'https://github.com/third/moved3')
        self.assertEqual(published[2]['mirrors']['github-dest']['name'], 'mirror-third-moved3')
        self.assertEqual(len([call for call in self.api.methods('PUT')
                              if 'config.json' in call[1]]), 2)
        self.assertEqual(self.pending_count(), 0)

    # -- store absent -----------------------------------------------------
    def test_no_rename_when_management_store_is_absent(self):
        environment = {key: value for key, value in ENVIRONMENT.items()
                       if key != 'MIRROR_MANAGEMENT_REPOSITORY'}
        original = self.api.config()
        for key in ('GITHUB_REPOSITORY',):
            environment.pop(key, None)
        error, result, stderr = run_cli(self.directory, self.api, relocation(), environment)
        self.assertIsNone(error, stderr)
        self.assertIn('migration deferred', stderr)
        self.assertEqual(self.api.methods('PATCH'), [])
        self.assertEqual(self.api.methods('PUT'), [])
        self.assertEqual(self.api.config(), original)
        self.assertEqual(result[0][1]['name'], 'renamed')


if __name__ == '__main__':
    unittest.main()
