import unittest

from repo_mirror.forgejo_sync import parse_manifest


class ForgejoAutomaticTransitionTests(unittest.TestCase):
    def test_move_follows_derived_name_and_preserves_original_binding(self):
        from repo_mirror.forgejo_automatic_transition import prepare_source_transition
        secret = 'fixture-signing-secret-0123456789abcdef'
        config = {'destinations': {'home': {}}, 'repositories': [
            {'source': {'url': 'https://git.example/old/repo', 'id': 11},
             'mirrors': {'home': {'name': 'repo', 'id': 22}}}]}
        metadata = {'id': 11, 'full_name': 'new/renamed',
                    'html_url': 'https://git.example/new/renamed',
                    'clone_url': 'https://git.example/new/renamed.git'}
        changed = prepare_source_transition(config, 0, metadata, 3300, 'dest', secret)
        entry = parse_manifest(changed)[0]
        self.assertEqual(entry['url'], 'https://git.example/new/renamed')
        # A name equal to the one derived for the old source tracks the move;
        # the original binding keeps the pre-move name for recovery.
        self.assertEqual(entry['destination_name'], 'renamed')
        self.assertEqual(entry['legacy_url'], 'https://git.example/old/repo')
        self.assertEqual(entry['legacy_destination_name'], 'repo')
        self.assertNotIn('legacy', config['repositories'][0]['mirrors']['home'])
        from repo_mirror.migration_attestation import forgejo_claim, verify
        verify(forgejo_claim(entry, 3300, 'dest'), entry['signature'], secret)

    def test_repeated_move_rejects_changed_repository_pin(self):
        from repo_mirror.forgejo_automatic_transition import prepare_source_transition
        secret = 'fixture-signing-secret-0123456789abcdef'
        config = {'destinations': {'home': {}}, 'repositories': [
            {'source': {'url': 'https://git.example/old/repo', 'id': 11},
             'mirrors': {'home': {'name': 'repo', 'id': 22}}}]}
        first = {'id': 11, 'full_name': 'new/repo', 'html_url': 'https://git.example/new/repo',
                 'clone_url': 'https://git.example/new/repo.git'}
        moved = prepare_source_transition(config, 0, first, 3300, 'dest', secret)
        moved['repositories'][0]['source']['id'] = 12
        second = {'id': 12, 'full_name': 'next/repo', 'html_url': 'https://git.example/next/repo',
                  'clone_url': 'https://git.example/next/repo.git'}
        with self.assertRaises(ValueError):
            prepare_source_transition(moved, 0, second, 3300, 'dest', secret)

    def test_defaulted_name_follows_the_source_but_a_custom_name_is_preserved(self):
        from repo_mirror.forgejo_automatic_transition import prepare_source_transition
        secret = 'fixture-signing-secret-0123456789abcdef'
        metadata = {'id': 11, 'full_name': 'new/renamed', 'html_url': 'https://git.example/new/renamed',
                    'clone_url': 'https://git.example/new/renamed.git'}
        for mirror, expected in (({'id': 22}, 'renamed'), ({'name': 'mirror-copy', 'id': 22}, 'mirror-copy')):
            with self.subTest(mirror=mirror):
                config = {'destinations': {'home': {}}, 'repositories': [
                    {'source': {'url': 'https://git.example/old/repo', 'id': 11}, 'mirrors': {'home': mirror}}]}
                changed = prepare_source_transition(config, 0, metadata, 3300, 'dest', secret)
                entry = parse_manifest(changed)[0]
                self.assertEqual(entry['destination_name'], expected)
                self.assertEqual(entry['legacy_destination_name'], 'repo' if expected == 'renamed' else 'mirror-copy')
                self.assertEqual(entry['legacy_url'], 'https://git.example/old/repo')
                # The candidate is a copy; the configured document is untouched.
                self.assertEqual(parse_manifest(config)[0]['url'], 'https://git.example/old/repo')
                self.assertNotIn('legacy', config['repositories'][0]['mirrors']['home'])
