"""plan_migration must accept entries that carry only the immutable destination ID."""
import unittest

from repo_mirror.migration_attestation import sign
from repo_mirror.migrations import claim, plan_migration, verify_recovery

SECRET = 'k' * 40


def document(with_name):
    mirror = {'id': 22} if not with_name else {'id': 22, 'name': with_name}
    return {
        'destinations': {'github-mirror-owner': {'type': 'github', 'url': 'https://github.com',
                                              'owner': 'mirror-owner', 'auth': 'mirrorbot'}},
        'repositories': [
            {'source': {'url': 'https://github.com/old/repo', 'id': 11},
             'mirrors': {'github-mirror-owner': mirror}},
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
        'destination': {'profile': 'github-mirror-owner', 'owner': 'mirror-owner',
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


def _config_with_legacy(source_owner, source_name, dest_name, legacy, source_id=1395104116, dest_id=42):
    return {
        'destinations': {'github-mirror-owner': {'type': 'github', 'url': 'https://github.com',
                                              'owner': 'mirror-owner', 'auth': 'mirrorbot'}},
        'repositories': [
            {'source': {'url': f'https://github.com/{source_owner}/{source_name}', 'id': source_id},
             'mirrors': {'github-mirror-owner': {'id': dest_id, 'name': dest_name, 'legacy': legacy}}},
        ],
    }


def _relocation_step(orig_owner, orig_name, new_owner, new_name, dest_name,
                     source_id=1395104116, dest_id=42):
    return {
        'source_origin': 'https://github.com',
        'source_id': source_id,
        'original_owner': orig_owner,
        'original_name': orig_name,
        'owner': new_owner,
        'name': new_name,
        'destination': {'profile': 'github-mirror-owner', 'owner': 'mirror-owner',
                        'name': dest_name, 'id': dest_id},
    }


def _legacy_from_entry(entry):
    return {
        'source_owner': entry['recovery']['source_owner'],
        'source_name': entry['recovery']['source_name'],
        'destination_name': entry['recovery']['destination_name'],
        'signature': entry['recovery']['signature'],
    }


class RepeatedRelocationPlanTests(unittest.TestCase):
    def test_second_relocation_resigns_while_preserving_original_legacy_binding(self):
        doc1 = {
            'destinations': {'github-mirror-owner': {'type': 'github', 'url': 'https://github.com',
                                                  'owner': 'mirror-owner', 'auth': 'mirrorbot'}},
            'repositories': [
                {'source': {'url': 'https://github.com/upstream-owner/legacy-project',
                            'id': 1395104116},
                 'mirrors': {'github-mirror-owner': {'id': 42,
                                                  'name': 'mirror-upstream-owner-legacy-project'}}},
            ],
        }
        entry1 = plan_migration(
            _relocation_step('upstream-owner', 'legacy-project',
                             'upstream-owner', 'legacy-project-extended',
                             'mirror-upstream-owner-legacy-project'),
            {'document': doc1, 'path': 'config.json', 'base_sha': 'a' * 40},
            secret=SECRET,
        )
        legacy1 = _legacy_from_entry(entry1)

        entry2 = plan_migration(
            _relocation_step('upstream-owner', 'legacy-project-extended',
                             'upstream-owner', 'legacy-project',
                             'mirror-upstream-owner-legacy-project-extended'),
            {'document': _config_with_legacy('upstream-owner', 'legacy-project-extended',
                                             'mirror-upstream-owner-legacy-project-extended',
                                             legacy1),
             'path': 'config.json', 'base_sha': 'b' * 40},
            secret=SECRET,
        )
        self.assertEqual(entry2['recovery']['source_owner'], 'upstream-owner')
        self.assertEqual(entry2['recovery']['source_name'], 'legacy-project')
        self.assertEqual(entry2['recovery']['destination_name'],
                         'mirror-upstream-owner-legacy-project')
        self.assertEqual(entry2['destination']['name'],
                         'mirror-upstream-owner-legacy-project')
        self.assertNotEqual(entry2['recovery']['signature'], legacy1['signature'])
        self.assertEqual(entry2['recovery']['signature'], sign(claim(entry2), SECRET))
        verify_recovery(entry2, SECRET)

    def test_third_relocation_has_no_drift_or_accumulation(self):
        doc1 = {
            'destinations': {'github-mirror-owner': {'type': 'github', 'url': 'https://github.com',
                                                  'owner': 'mirror-owner', 'auth': 'mirrorbot'}},
            'repositories': [
                {'source': {'url': 'https://github.com/owner1/repo1', 'id': 11},
                 'mirrors': {'github-mirror-owner': {'id': 22, 'name': 'mirror-owner1-repo1'}}},
            ],
        }
        entry1 = plan_migration(
            _relocation_step('owner1', 'repo1', 'owner2', 'repo2', 'mirror-owner1-repo1',
                             source_id=11, dest_id=22),
            {'document': doc1, 'path': 'config.json', 'base_sha': 'a' * 40},
            secret=SECRET, destination_origin='https://forgejo.example',
        )
        legacy1 = _legacy_from_entry(entry1)

        entry2 = plan_migration(
            _relocation_step('owner2', 'repo2', 'owner3', 'repo3', 'mirror-owner2-repo2',
                             source_id=11, dest_id=22),
            {'document': _config_with_legacy('owner2', 'repo2', 'mirror-owner2-repo2',
                                             legacy1, source_id=11, dest_id=22),
             'path': 'config.json', 'base_sha': 'b' * 40},
            secret=SECRET, destination_origin='https://forgejo.example',
        )
        legacy2 = _legacy_from_entry(entry2)

        entry3 = plan_migration(
            _relocation_step('owner3', 'repo3', 'owner4', 'repo4', 'mirror-owner3-repo3',
                             source_id=11, dest_id=22),
            {'document': _config_with_legacy('owner3', 'repo3', 'mirror-owner3-repo3',
                                             legacy2, source_id=11, dest_id=22),
             'path': 'config.json', 'base_sha': 'c' * 40},
            secret=SECRET, destination_origin='https://forgejo.example',
        )
        self.assertEqual(set(entry3['recovery']),
                         {'source_origin', 'source_owner', 'source_name',
                          'destination_name', 'signature'})
        self.assertEqual(entry3['recovery']['source_owner'], 'owner1')
        self.assertEqual(entry3['recovery']['source_name'], 'repo1')
        self.assertEqual(entry3['recovery']['destination_name'], 'mirror-owner1-repo1')
        self.assertEqual(entry3['destination']['name'], 'mirror-owner4-repo4')
        self.assertNotIn(entry3['recovery']['signature'],
                         (legacy1['signature'], legacy2['signature']))
        self.assertEqual(entry3['recovery']['signature'],
                         sign(claim(entry3, 'https://forgejo.example'), SECRET))
        verify_recovery(entry3, SECRET, 'https://forgejo.example')

    def test_wrong_legacy_signature_still_raises(self):
        entry1 = plan_migration(
            relocation(),
            {'document': document(None), 'path': 'config.json', 'base_sha': 'a' * 40},
            secret=SECRET,
        )
        legacy = _legacy_from_entry(entry1)
        mismatched_claim = dict(claim(entry1), current_source_name='other-intermediate')
        mismatched_sig = sign(mismatched_claim, SECRET)
        flipped = ('1' if legacy['signature'][0] == '0' else '0') + legacy['signature'][1:]
        for bad_signature in (flipped, mismatched_sig, '0' * 64, 'not-hex'):
            with self.subTest(bad_signature=bad_signature):
                bad_legacy = {**legacy, 'signature': bad_signature}
                with self.assertRaisesRegex(ValueError, '^invalid migration attestation$'):
                    plan_migration(
                        _relocation_step('new', 'repo', 'third', 'repo2',
                                         'mirror-new-repo', source_id=11, dest_id=22),
                        {'document': _config_with_legacy('new', 'repo', 'mirror-new-repo',
                                                         bad_legacy, source_id=11, dest_id=22),
                         'path': 'config.json', 'base_sha': 'b' * 40},
                        secret=SECRET,
                    )

    def test_absent_or_short_secret_preserves_carried_legacy_signature(self):
        carried_sig = 'f' * 64
        legacy = {
            'source_owner': 'old',
            'source_name': 'repo',
            'destination_name': 'mirror-old-repo',
            'signature': carried_sig,
        }
        for secret in (None, '', 'short'):
            with self.subTest(secret=secret):
                entry = plan_migration(
                    _relocation_step('mid', 'repo2', 'final', 'repo3',
                                     'mirror-mid-repo2', source_id=11, dest_id=22),
                    {'document': _config_with_legacy('mid', 'repo2', 'mirror-mid-repo2',
                                                     legacy, source_id=11, dest_id=22),
                     'path': 'config.json', 'base_sha': 'b' * 40},
                    secret=secret,
                )
                self.assertEqual(entry['recovery']['source_owner'], 'old')
                self.assertEqual(entry['recovery']['source_name'], 'repo')
                self.assertEqual(entry['recovery']['destination_name'], 'mirror-old-repo')
                self.assertEqual(entry['recovery']['signature'], carried_sig)


if __name__ == '__main__':
    unittest.main()
