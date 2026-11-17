"""plan_migration must accept entries that carry only the immutable destination ID."""
import unittest

from repo_mirror.migrations import plan_migration

SECRET = 'k' * 40


def document(with_name):
    mirror = {'id': 22} if not with_name else {'id': 22, 'name': with_name}
    return {
        'destinations': {'github-manoma080': {'type': 'github', 'url': 'https://github.com',
                                              'owner': 'manoma080', 'auth': 'mirrorbot'}},
        'repositories': [
            {'source': {'url': 'https://github.com/old/repo', 'id': 11},
             'mirrors': {'github-manoma080': mirror}},
        ],
    }


def relocation():
    return {
        'source_origin': 'https://github.com',
        'source_id': 11,
        'original_owner': 'old',
        'original_name': 'repo',
        'owner': 'new',
        'name': 'repo',
        'destination': {'profile': 'github-manoma080', 'owner': 'manoma080',
                        'name': 'mirror-old-repo', 'id': 22},
    }


class DestinationPlanTests(unittest.TestCase):
    def test_id_only_entry_uses_the_derived_name(self):
        entry = plan_migration(relocation(),
                               {'document': document(None), 'path': 'config.json',
                                'base_sha': 'a' * 40},
                               secret=SECRET)
        self.assertEqual(entry['destination']['name'], 'mirror-new-repo')
        self.assertEqual(entry['recovery']['destination_name'], 'mirror-old-repo')

    def test_configured_name_must_match_what_the_worker_reported(self):
        with self.assertRaises(ValueError):
            plan_migration(relocation(),
                           {'document': document('some-other-name'), 'path': 'config.json',
                            'base_sha': 'a' * 40}, secret=SECRET)

    def test_custom_name_is_preserved(self):
        custom = relocation()
        custom['destination']['name'] = 'kept-name'
        entry = plan_migration(custom,
                               {'document': document('kept-name'), 'path': 'config.json',
                                'base_sha': 'a' * 40}, secret=SECRET)
        self.assertEqual(entry['destination']['name'], 'kept-name')


if __name__ == '__main__':
    unittest.main()
