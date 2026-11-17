"""Tests for the durable migration journal (`migrations.json`)."""
import base64
import hashlib
import json
import os
import tempfile
import unittest
from urllib.error import HTTPError

from repo_mirror.migrations_journal import MigrationsJournal, journal_key, parse
from repo_mirror.migrations_store import (
    FileMigrations, GitHubMigrations, MigrationsStore, build_entry,
)

SECRET = 'k' * 40


def entry(phase='prepared', source_id=1234, source_name='repo',
          destination_name='mirror-owner-repo'):
    return {
        'source': {'origin': 'https://github.com', 'id': source_id,
                   'owner': 'newowner', 'name': source_name},
        'destination': {'profile': 'github-manoma080', 'owner': 'manoma080',
                        'name': destination_name, 'id': 5678},
        'recovery': {'source_origin': 'https://github.com', 'source_owner': 'oldowner',
                     'source_name': 'repo', 'destination_name': 'mirror-owner-repo',
                     'signature': 'c0ffee'},
        'config': {'path': 'config.json', 'base_sha': 'a' * 40},
        'phase': phase,
    }


class JournalValidationTests(unittest.TestCase):
    def test_recorded_entry_survives_reload(self):
        journal = MigrationsJournal(None, SECRET).put(entry())
        reloaded = parse(journal.encode(), SECRET)
        self.assertEqual(
            reloaded.get('https://github.com', 1234)['destination']['name'],
            'mirror-owner-repo')

    def test_tampered_entry_is_rejected(self):
        journal = MigrationsJournal(None, SECRET).put(entry())
        document = json.loads(journal.encode())
        document['migrations'][journal_key('https://github.com', 1234)]['destination']['name'] = 'attacker'
        with self.assertRaises(ValueError):
            parse(json.dumps(document).encode(), SECRET)

    def test_wrong_secret_is_rejected(self):
        raw = MigrationsJournal(None, SECRET).put(entry()).encode()
        with self.assertRaises(ValueError):
            parse(raw, 'z' * 40)

    def test_duplicate_keys_are_rejected(self):
        raw = b'{"version":1,"version":1,"migrations":{},"signatures":{}}'
        with self.assertRaises(ValueError):
            parse(raw, SECRET)

    def test_boolean_source_id_is_rejected(self):
        plan = entry()
        plan['source']['id'] = True
        with self.assertRaises(ValueError):
            build_entry(plan)

    def test_unsupported_config_path_is_rejected(self):
        plan = entry()
        plan['config']['path'] = 'other.json'
        with self.assertRaises(ValueError):
            build_entry(plan)

    def test_conflicting_record_for_same_source_is_rejected(self):
        journal = MigrationsJournal(None, SECRET).put(entry())
        with self.assertRaises(ValueError):
            journal.put(entry(source_name='different'))

    def test_recovery_binding_must_not_change_on_advance(self):
        journal = MigrationsJournal(None, SECRET).put(entry())
        changed = entry('synced')
        changed['recovery']['destination_name'] = 'renamed-destination'
        with self.assertRaises(ValueError):
            journal.advance('https://github.com', 1234, changed)

    def test_phases_advance_forward_only(self):
        journal = MigrationsJournal(None, SECRET).put(entry())
        journal = journal.advance('https://github.com', 1234, entry('synced'))
        self.assertEqual(journal.get('https://github.com', 1234)['phase'], 'synced')
        with self.assertRaises(ValueError):
            journal.advance('https://github.com', 1234, entry('prepared'))

    def test_incomplete_migration_cannot_be_removed(self):
        journal = MigrationsJournal(None, SECRET).put(entry())
        with self.assertRaises(ValueError):
            journal.remove('https://github.com', 1234)

    def test_completed_migration_can_be_removed(self):
        journal = MigrationsJournal(None, SECRET).put(entry())
        for phase in ('synced', 'renamed', 'configured'):
            journal = journal.advance('https://github.com', 1234, entry(phase))
        self.assertEqual(journal.remove('https://github.com', 1234).entries(), {})


class FileStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, 'migrations.json')
        self.store = MigrationsStore(FileMigrations(self.path), SECRET)

    def test_absent_file_loads_empty(self):
        self.assertEqual(self.store.pending(), {})

    def test_record_advance_and_complete_publish(self):
        self.store.record(build_entry(entry()))
        self.assertEqual(list(self.store.pending().values())[0]['source']['name'], 'repo')
        self.store.advance('https://github.com', 1234, entry('synced'))
        self.assertEqual(self.store.get('https://github.com', 1234)['phase'], 'synced')
        for phase in ('renamed', 'configured'):
            self.store.advance('https://github.com', 1234, entry(phase))
        self.store.complete('https://github.com', 1234)
        self.assertEqual(self.store.pending(), {})

    def test_stale_writer_is_detected(self):
        stale = self.store.backend.load_bytes()
        self.store.record(build_entry(entry()))
        with self.assertRaises(ValueError):
            FileMigrations(self.path).publish(stale, b'{}\n')


class FakeContentsAPI:
    """Minimal Contents API stand-in with blob shas and optimistic concurrency."""

    def __init__(self):
        self.content = None
        self.requests = []
        self.tamper = False

    def _sha(self, raw):
        return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()

    def _response(self, raw):
        return {'type': 'file', 'encoding': 'base64', 'sha': self._sha(raw),
                'content': base64.b64encode(raw).decode('ascii')}

    def request(self, method, url, payload=None):
        payload = payload or {}
        self.requests.append((method, url, payload))
        if method == 'GET':
            if self.content is None:
                raise HTTPError(url, 404, 'Not Found', None, None)
            return self._response(self.content)
        if self.content is not None:
            if payload.get('sha') != self._sha(self.content):
                raise HTTPError(url, 409, 'Conflict', None, None)
        elif 'sha' in payload:
            raise HTTPError(url, 409, 'Conflict', None, None)
        self.content = base64.b64decode(payload['content'])
        if self.tamper:
            self.content += b' '
        return {'content': self._response(self.content)}


class GitHubBackendTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeContentsAPI()
        self.store = MigrationsStore(GitHubMigrations(self.api, 'manoma080', 'repo-mirror'), SECRET)

    def test_first_record_creates_file_without_sha(self):
        self.store.record(build_entry(entry()))
        puts = [r for r in self.api.requests if r[0] == 'PUT']
        self.assertEqual(len(puts), 1)
        self.assertNotIn('sha', puts[0][2])

    def test_second_record_uses_conditional_sha(self):
        self.store.record(build_entry(entry()))
        self.store.record(build_entry(entry(source_id=9999, source_name='other')))
        puts = [r for r in self.api.requests if r[0] == 'PUT']
        self.assertEqual(len(puts), 2)
        self.assertIn('sha', puts[1][2])
        self.assertEqual(len(self.store.pending()), 2)

    def test_readback_mismatch_is_fatal(self):
        self.api.tamper = True
        with self.assertRaises(ValueError):
            self.store.record(build_entry(entry()))

    def test_stale_publish_does_not_overwrite_newer_record(self):
        self.store.record(build_entry(entry()))
        stale = self.api.content
        self.store.record(build_entry(entry(source_id=9999, source_name='other')))
        with self.assertRaises(ValueError):
            GitHubMigrations(self.api, 'manoma080', 'repo-mirror').publish(
                stale, stale + b'x')
        self.assertEqual(parse(self.api.content, SECRET).pending()[
            journal_key('https://github.com', 9999)]['source']['name'], 'other')

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(self.store.pending(), {})


if __name__ == '__main__':
    unittest.main()
