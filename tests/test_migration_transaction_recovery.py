"""Recovery-path tests for the provider-neutral migration transaction."""
import json
import os
import tempfile
import unittest

from repo_mirror.migrations import (
    MigrationTransaction,
    blob_sha,
    config_document,
    is_published,
    plan_migration,
    rewrite_config,
    verify_recovery,
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

    def name_by_id(self, repository_id):
        matches = [name for name, repo in self.items.items() if repo.get('id') == repository_id]
        if not matches:
            raise ValueError('migration destination missing')
        if len(matches) > 1:
            raise ValueError('migration destination pin is ambiguous')
        return matches[0]

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

    def test_second_relocation_rewrites_signature_and_renames_from_intermediate_name(self):
        entry1 = self.transaction.migrate(relocation())
        self.assertIsNotNone(entry1)
        sig1 = json.loads(self.configuration.raw)['repositories'][0]['mirrors']['github-dest']['legacy']['signature']

        move2 = {
            'source_origin': 'https://api.github.com', 'source_id': 1,
            'original_owner': 'new-owner', 'original_name': 'renamed',
            'owner': 'third-owner', 'name': 'final-repo',
            'destination': {'profile': 'github-dest', 'owner': 'dest',
                            'name': 'mirror-new-owner-renamed', 'id': 2},
        }
        entry2 = self.transaction.migrate(move2)
        self.assertIsNotNone(entry2)
        self.assertEqual(set(entry2['recovery']),
                         {'source_origin', 'source_owner', 'source_name',
                          'destination_name', 'signature'})
        self.assertEqual(entry2['recovery']['source_owner'], 'old-owner')
        self.assertEqual(entry2['recovery']['source_name'], 'repo')
        self.assertEqual(entry2['recovery']['destination_name'], 'mirror-old-owner-repo')
        self.assertNotEqual(entry2['recovery']['signature'], sig1)
        verify_recovery(entry2, SECRET)

        published_doc = config_document(self.configuration.raw)
        self.assertTrue(is_published(published_doc, entry2))
        mirror = published_doc['repositories'][0]['mirrors']['github-dest']
        self.assertEqual(mirror['name'], 'mirror-third-owner-final-repo')
        self.assertEqual(mirror['legacy']['source_owner'], 'old-owner')
        self.assertEqual(mirror['legacy']['source_name'], 'repo')
        self.assertEqual(mirror['legacy']['destination_name'], 'mirror-old-owner-repo')
        self.assertEqual(mirror['legacy']['signature'], entry2['recovery']['signature'])
        self.assertEqual(self.repositories.renames, [
            ('mirror-old-owner-repo', 'mirror-new-owner-renamed'),
            ('mirror-new-owner-renamed', 'mirror-third-owner-final-repo'),
        ])
        self.assertEqual(self.store.pending(), {})

    def test_second_relocation_back_to_original_name_renames_from_intermediate(self):
        doc = {
            'destinations': {'github-dest': {'account_type': 'personal'}},
            'repositories': [
                {'source': {'url': 'https://github.com/Anonymous941/QuestEnhancer', 'id': 1},
                 'mirrors': {'github-dest': {'name': 'mirror-Anonymous941-QuestEnhancer', 'id': 2}}},
            ],
        }
        self.configuration.raw = (json.dumps(doc, indent=2) + '\n').encode()
        self.repositories = Repositories(('mirror-Anonymous941-QuestEnhancer', 2))
        self.transaction = MigrationTransaction(
            self.store, self.repositories, self.configuration, secret=SECRET)

        move1 = {
            'source_origin': 'https://api.github.com', 'source_id': 1,
            'original_owner': 'Anonymous941', 'original_name': 'QuestEnhancer',
            'owner': 'Anonymous941', 'name': 'QuestEnhancer-Bubbagum88',
            'destination': {'profile': 'github-dest', 'owner': 'dest',
                            'name': 'mirror-Anonymous941-QuestEnhancer', 'id': 2},
        }
        entry1 = self.transaction.migrate(move1)
        move2 = {
            'source_origin': 'https://api.github.com', 'source_id': 1,
            'original_owner': 'Anonymous941', 'original_name': 'QuestEnhancer-Bubbagum88',
            'owner': 'Anonymous941', 'name': 'QuestEnhancer',
            'destination': {'profile': 'github-dest', 'owner': 'dest',
                            'name': 'mirror-Anonymous941-QuestEnhancer-Bubbagum88', 'id': 2},
        }
        entry2 = self.transaction.migrate(move2)
        self.assertEqual(entry2['destination']['name'], 'mirror-Anonymous941-QuestEnhancer')
        self.assertEqual(entry2['recovery']['destination_name'], 'mirror-Anonymous941-QuestEnhancer')
        self.assertNotEqual(entry2['recovery']['signature'], entry1['recovery']['signature'])
        verify_recovery(entry2, SECRET)
        self.assertTrue(is_published(config_document(self.configuration.raw), entry2))
        self.assertEqual(self.repositories.renames, [
            ('mirror-Anonymous941-QuestEnhancer', 'mirror-Anonymous941-QuestEnhancer-Bubbagum88'),
            ('mirror-Anonymous941-QuestEnhancer-Bubbagum88', 'mirror-Anonymous941-QuestEnhancer'),
        ])
        self.assertEqual(self.store.pending(), {})

    def test_third_relocation_has_no_drift_or_name_accumulation(self):
        entry1 = self.transaction.migrate(relocation())
        entry2 = self.transaction.migrate({
            'source_origin': 'https://api.github.com', 'source_id': 1,
            'original_owner': 'new-owner', 'original_name': 'renamed',
            'owner': 'third-owner', 'name': 'moved-twice',
            'destination': {'profile': 'github-dest', 'owner': 'dest',
                            'name': 'mirror-new-owner-renamed', 'id': 2},
        })
        entry3 = self.transaction.migrate({
            'source_origin': 'https://api.github.com', 'source_id': 1,
            'original_owner': 'third-owner', 'original_name': 'moved-twice',
            'owner': 'fourth-owner', 'name': 'moved-thrice',
            'destination': {'profile': 'github-dest', 'owner': 'dest',
                            'name': 'mirror-third-owner-moved-twice', 'id': 2},
        })
        self.assertEqual(set(entry3['recovery']),
                         {'source_origin', 'source_owner', 'source_name',
                          'destination_name', 'signature'})
        self.assertEqual(entry3['recovery']['source_owner'], 'old-owner')
        self.assertEqual(entry3['recovery']['source_name'], 'repo')
        self.assertEqual(entry3['recovery']['destination_name'], 'mirror-old-owner-repo')
        self.assertNotIn(entry3['recovery']['signature'],
                         (entry1['recovery']['signature'], entry2['recovery']['signature']))
        verify_recovery(entry3, SECRET)
        published_doc = config_document(self.configuration.raw)
        self.assertTrue(is_published(published_doc, entry3))
        self.assertEqual(published_doc['repositories'][0]['mirrors']['github-dest']['name'],
                         'mirror-fourth-owner-moved-thrice')
        self.assertEqual(self.store.pending(), {})

    def test_legacy_name_mismatch_still_fails_closed_and_signature_checked_for_published(self):
        self.transaction.migrate(relocation())
        move2 = {
            'source_origin': 'https://api.github.com', 'source_id': 1,
            'original_owner': 'new-owner', 'original_name': 'renamed',
            'owner': 'third-owner', 'name': 'final-repo',
            'destination': {'profile': 'github-dest', 'owner': 'dest',
                            'name': 'mirror-new-owner-renamed', 'id': 2},
        }
        raw, sha = self.configuration.read()
        entry2 = plan_migration(
            move2, {'document': json.loads(raw), 'path': 'config.json', 'base_sha': sha},
            secret=SECRET,
        )
        # Signature differs before rewrite_config, so is_published must be False.
        self.assertFalse(is_published(config_document(raw), entry2))
        for field in ('source_owner', 'source_name', 'destination_name'):
            with self.subTest(field=field):
                tampered = json.loads(raw)
                tampered['repositories'][0]['mirrors']['github-dest']['legacy'][field] = 'tampered'
                with self.assertRaisesRegex(ValueError, '^migration original binding changed$'):
                    rewrite_config((json.dumps(tampered, indent=2) + '\n').encode(), entry2)

    def test_missing_or_ambiguous_destination_id_fails_closed(self):
        entry = self.entry('prepared')
        with self.subTest('missing destination id'):
            self.repositories.items = {}
            with self.assertRaisesRegex(ValueError, '^migration destination missing$'):
                self.transaction.recover()
            self.assertEqual(self.repositories.renames, [])
            self.assertEqual(self.configuration.publishes, 0)

        with self.subTest('ambiguous destination id'):
            self.repositories = Repositories(('mirror-old-owner-repo', 2), ('mirror-duplicate', 2))
            self.transaction = MigrationTransaction(
                self.store, self.repositories, self.configuration, secret=SECRET)
            with self.assertRaisesRegex(ValueError, '^migration destination pin is ambiguous$'):
                self.transaction.recover()
            self.assertEqual(self.repositories.renames, [])
            self.assertEqual(self.configuration.publishes, 0)

    def test_crash_between_rename_and_publish_on_second_relocation_does_not_rename_twice(self):
        self.transaction.migrate(relocation())
        raw, sha = self.configuration.read()
        move2 = {
            'source_origin': 'https://api.github.com', 'source_id': 1,
            'original_owner': 'new-owner', 'original_name': 'renamed',
            'owner': 'third-owner', 'name': 'final-repo',
            'destination': {'profile': 'github-dest', 'owner': 'dest',
                            'name': 'mirror-new-owner-renamed', 'id': 2},
        }
        entry2 = plan_migration(
            move2, {'document': json.loads(raw), 'path': 'config.json', 'base_sha': sha},
            secret=SECRET,
        )
        # Simulate crash right after destination rename committed, before journal advanced or config published.
        self.repositories.rename('mirror-new-owner-renamed', 'mirror-third-owner-final-repo')
        self.store.record({**entry2, 'phase': 'synced'})
        renames_before = list(self.repositories.renames)

        self.assertEqual(self.transaction.recover(), 1)
        self.assertEqual(self.repositories.renames, renames_before)
        self.assertTrue(is_published(config_document(self.configuration.raw), entry2))
        self.assertEqual(self.store.pending(), {})


if __name__ == '__main__':
    unittest.main()
