"""Recovery-path tests for the provider-neutral migration transaction."""
import json
import os
import tempfile
import unittest

from repo_mirror.migrations import (
    MigrationTransaction,
    blob_sha,
    plan_migration,
    rewrite_config,
)
from repo_mirror.migrations_store import FileMigrations, MigrationsStore

SECRET = 'fixture-signing-secret-0123456789abcdef'


def document(name='mirror-old-owner-repo'):
    return {
        'destinations': {'github-dest': {'account_type': 'personal'}},
        'repositories': [
            {'source': {'url': 'https://github.com/old-owner/repo', 'id': 1},
             'mirrors': {'github-dest': {'name': name, 'id': 2}}},
        ],
    }


def relocation(name='mirror-old-owner-repo'):
    return {'source_origin': 'https://api.github.com', 'source_id': 1,
            'original_owner': 'old-owner', 'original_name': 'repo',
            'owner': 'new-owner', 'name': 'renamed',
            'destination': {'profile': 'github-dest', 'owner': 'dest',
                            'name': name, 'id': 2}}


class MemoryConfiguration:
    def __init__(self, raw):
        self.raw = raw
        self.publishes = 0

    def read(self):
        return self.raw, blob_sha(self.raw)

    def publish(self, expected, replacement):
        if blob_sha(self.raw) != expected:
            raise ValueError('migration configuration changed by another writer')
        self.raw = replacement
        self.publishes += 1


class Repositories:
    def __init__(self, *names):
        self.items = {name: {'id': identifier, 'full_name': 'dest/' + name}
                      for name, identifier in names}
        self.renames = []

    def get(self, name):
        repo = self.items.get(name)
        return None if repo is None else dict(repo)

    def rename(self, old, new):
        self.renames.append((old, new))
        self.items[new] = self.items.pop(old)
        self.items[new]['full_name'] = 'dest/' + new


class MigrationRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, 'migrations.json')
        self.store = MigrationsStore(FileMigrations(self.path), SECRET)
        self.configuration = MemoryConfiguration((json.dumps(document(), indent=2) + '\n').encode())
        self.repositories = Repositories(('mirror-old-owner-repo', 2))
        self.transaction = MigrationTransaction(
            self.store, self.repositories, self.configuration, secret=SECRET)

    def entry(self, phase='prepared', name='mirror-old-owner-repo'):
        raw, sha = self.configuration.read()
        entry = plan_migration(relocation(name), {'document': json.loads(raw), 'path': 'config.json',
                                             'base_sha': sha}, secret=SECRET)
        entry = {**entry, 'phase': phase}
        self.store.record(entry)
        return entry

    def test_recovery_from_prepared_renames_and_publishes(self):
        entry = self.entry('prepared')
        self.assertEqual(self.transaction.recover(), 1)
        self.assertEqual(self.repositories.renames, [('mirror-old-owner-repo', 'mirror-new-owner-renamed')])
        self.assertEqual(self.store.pending(), {})
        self.assertEqual(self.configuration.publishes, 1)
        self.assertEqual(entry['destination']['name'], 'mirror-new-owner-renamed')

    def test_recovery_does_not_rename_twice(self):
        self.entry('synced')
        self.repositories.rename('mirror-old-owner-repo', 'mirror-new-owner-renamed')
        self.assertEqual(self.transaction.recover(), 1)
        self.assertEqual(self.repositories.renames, [('mirror-old-owner-repo', 'mirror-new-owner-renamed')])
        self.assertEqual(self.store.pending(), {})

    def test_old_name_redirecting_elsewhere_fails_closed(self):
        self.entry('synced')
        # The old name now resolves to a repository under a different canonical
        # name; mirroring must not treat that redirect as our destination.
        self.repositories.items['mirror-old-owner-repo'] = {
            'id': 2, 'full_name': 'dest/somewhere-else'}
        with self.assertRaisesRegex(ValueError, 'migration destination identity mismatch'):
            self.transaction.recover()
        self.assertEqual(self.repositories.renames, [])
        self.assertEqual([item['phase'] for item in self.store.pending().values()], ['synced'])

    def test_already_published_configuration_is_not_republished(self):
        entry = self.entry('renamed')
        self.repositories.rename('mirror-old-owner-repo', 'mirror-new-owner-renamed')
        self.configuration.raw = rewrite_config(self.configuration.raw, entry)
        publishes = self.configuration.publishes
        self.assertEqual(self.transaction.recover(), 1)
        self.assertEqual(self.configuration.publishes, publishes)
        self.assertEqual(self.store.pending(), {})

    def test_custom_name_migration_never_renames(self):
        self.configuration.raw = (json.dumps(document('copy'), indent=2) + '\n').encode()
        self.repositories = Repositories(('copy', 2))
        self.transaction = MigrationTransaction(
            self.store, self.repositories, self.configuration, secret=SECRET)
        self.entry('prepared', 'copy')
        self.assertEqual(self.transaction.recover(), 1)
        self.assertEqual(self.repositories.renames, [])
        published = json.loads(self.configuration.raw)
        mirror = published['repositories'][0]['mirrors']['github-dest']
        self.assertEqual(mirror['name'], 'copy')
        self.assertEqual(published['repositories'][0]['source']['url'],
                         'https://github.com/new-owner/renamed')


if __name__ == '__main__':
    unittest.main()
