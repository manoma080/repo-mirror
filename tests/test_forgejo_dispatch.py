import copy
import unittest
from unittest.mock import Mock

from repo_mirror import forgejo_dispatch


A = 'https://github.com/Owner/Alpha'
B = 'https://code.example:8443/Owner/Beta'


def config():
    return {
        'destinations': {'home': {'type': 'forgejo', 'url': 'http://127.0.0.1:3300',
                                  'owner': 'local', 'auth': 'forgejo-home'}},
        'repositories': [
            {'source': {'url': A, 'id': 10},
             'mirrors': {'home': {'name': 'custom', 'id': 20, 'ignore_releases': True}}},
        ],
        'host_concurrency_limits': {'github.com': 1},
    }


class ForgejoDispatchTests(unittest.TestCase):
    def plan(self, document=None, **inputs):
        if document is None:
            document = config()
        return forgejo_dispatch.plan_dispatch(document, inputs, owner='local', port=3300)

    def test_selected_alias_preserves_spelling_pins_and_unrelated_settings(self):
        document = config()
        published, runtime, selected = self.plan(
            document, source_urls='https://API.GITHUB.COM/owner/alpha', ignore_releases='false')
        expected = copy.deepcopy(document)
        expected['repositories'][0]['mirrors']['home'].pop('ignore_releases')
        self.assertIsNone(published)
        self.assertEqual(runtime, expected)
        self.assertEqual(selected, [A])

    def test_blank_keeps_group_discovery(self):
        document = config()
        document['groups'] = [{'type': 'gitlab', 'base_url': 'https://gitlab.com',
                              'owner': 'group', 'id': 7, 'repos': 'all'}]
        self.assertEqual(self.plan(document, source_urls=' '), (None, document, None))

    def test_group_selected_dispatch_keeps_config_but_omits_discovery_from_runtime(self):
        document = config()
        document['groups'] = [{'type': 'gitlab', 'base_url': 'https://gitlab.com',
                              'owner': 'group', 'id': 7, 'repos': 'all'}]
        published, runtime, selected = self.plan(document, source_urls=A)
        self.assertIsNone(published)
        self.assertNotIn('groups', runtime)
        self.assertEqual(selected, [A])
        self.assertIn('groups', document)

    def test_remove_last_explicit_entry_returns_empty_named_config(self):
        document = config()
        published, runtime, selected = self.plan(document, source_urls=A, remove_from_config=True)
        self.assertEqual(published, {**document, 'repositories': []})
        self.assertEqual(published, runtime)
        self.assertEqual(selected, [])
        self.assertEqual(len(document['repositories']), 1)
        self.assertEqual(self.plan(published), (None, published, None))

    def test_group_routes_are_reserved_even_without_discovery(self):
        document = config()
        document['groups'] = [{'type': 'gitlab', 'base_url': 'https://gitlab.example:443',
                              'owner': 'group/sub', 'id': 7, 'repos': 'all'}]
        generated = 'gitlab-' + 'ab' * 12
        stale = 'https://gitlab.example:443/another/old'
        document['repositories'].append({'source': {'url': stale, 'type': 'gitlab', 'id': 31},
                                        'mirrors': {'home': {'name': generated, 'id': 41}}})
        for options in (dict(source_urls='https://gitlab.example/group/sub/new'),
                        dict(source_urls=stale, remove_from_config=True),
                        dict(source_urls=B, destination_name=generated.upper())):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, 'group-managed'):
                self.plan(document, **options)

    def test_alias_duplicates_are_rejected_before_running_any_callbacks(self):
        source, dest = Mock(), Mock()
        with self.assertRaisesRegex(ValueError, 'duplicate requested source'):
            forgejo_dispatch.plan_dispatch(config(), {'source_urls': A + ',https://api.github.com/owner/ALPHA',
                                                       'add_to_config': True}, owner='local', port=3300,
                                            source_lookup=source, prepare_destination=dest)
        source.assert_not_called()
        dest.assert_not_called()

    def test_invalid_or_conflicting_options(self):
        for options in (dict(add_to_config=True), dict(source_urls=A, add_to_config=True, remove_from_config=True),
                        dict(source_urls=A, remove_from_config=True, ignore_releases='false'),
                        dict(source_urls=A, visibility='internal'), dict(source_urls=A, ignore_releases=0),
                        dict(source_urls=f'{A},{B}', destination_name='name'),
                        dict(source_urls=A, add_to_config=1), dict(source_urls=A + ','),
                        dict(source_urls='https://secret@code.example/owner/project')):
            with self.subTest(options=options), self.assertRaises(ValueError) as error:
                self.plan(**options)
            self.assertNotIn('secret@', str(error.exception))

    def test_one_time_custom_name_is_not_published(self):
        published, runtime, selected = self.plan(source_urls=B, destination_name='once', ignore_releases='true')
        self.assertIsNone(published)
        self.assertEqual(selected, [B])
        self.assertEqual(runtime['repositories'][1], {'source': {'url': B},
                                                    'mirrors': {'home': {'name': 'once', 'ignore_releases': True}}})

    def test_pinned_name_override_requires_exact_name(self):
        for add in (False, True):
            with self.subTest(add=add), self.assertRaisesRegex(ValueError, 'rename pinned'):
                self.plan(source_urls=A, destination_name='CUSTOM', add_to_config=add)
        self.assertEqual(self.plan(source_urls=A, destination_name='custom'), (None, config(), [A]))

    def test_new_target_cannot_collide_with_unselected_mirror(self):
        with self.assertRaisesRegex(ValueError, 'conflicting destination'):
            self.plan(source_urls=B, destination_name='CUSTOM')

    def test_relocation_keeps_recovery_binding(self):
        document = config()
        historical = 'https://github.com/Old/Alpha'
        legacy = {'url': historical, 'name': 'old-target', 'signature': 'a' * 64}
        document['repositories'][0]['mirrors']['home']['legacy'] = legacy
        _, runtime, _ = self.plan(document, source_urls=A, ignore_releases='false')
        self.assertEqual(runtime['repositories'][0]['mirrors']['home']['legacy'], legacy)
        with self.assertRaisesRegex(ValueError, 'historical source'):
            self.plan(document, source_urls=historical)
        with self.assertRaisesRegex(ValueError, 'conflicting destination'):
            self.plan(document, source_urls=B, destination_name='OLD-TARGET')

    def test_visibility_override_for_new_mapping_is_runtime_only(self):
        published, runtime, selected = self.plan(source_urls=B, visibility='public')
        self.assertIsNone(published)
        self.assertEqual(runtime['repositories'][-1]['mirrors']['home']['visibility'], 'public')
        self.assertEqual(selected, [B])

    def test_visibility_cannot_change_an_existing_pin(self):
        with self.assertRaisesRegex(ValueError, 'change pinned destination visibility'):
            self.plan(source_urls=A, visibility='public')
        self.assertEqual(self.plan(source_urls=A, visibility='private'), (None, config(), [A]))

    def test_adding_persists_both_ids_after_batch_preflight(self):
        document = config()
        calls = []

        def source(url, kind):
            calls.append(('source', url, kind))
            return {A: 10, B: 30}[url]

        def dest(source, mirror, *, create):
            url = source['url']
            calls.append(('dest', url, create))
            self.assertEqual(mirror['name'], {A: 'custom', B: 'Beta'}[url])
            if create:
                self.assertEqual(source['id'], {A: 10, B: 30}[url])
                return {A: 20, B: 40}[url]
            return mirror.get('id')

        published, runtime, selected = forgejo_dispatch.plan_dispatch(
            document, {'source_urls': f'{B},{A}', 'add_to_config': True, 'ignore_releases': 'false'},
            owner='local', port=3300, source_lookup=source, prepare_destination=dest)
        self.assertEqual(calls[:4], [('source', B, 'forgejo'), ('dest', B, False),
                                     ('source', A, 'github'), ('dest', A, False)])
        self.assertEqual(published, runtime)
        self.assertEqual(selected, [B, A])
        self.assertEqual(published['repositories'][1], {'source': {'url': B, 'id': 30},
                                                      'mirrors': {'home': {'id': 40}}})
        self.assertEqual(published['repositories'][0]['mirrors']['home'], {'name': 'custom', 'id': 20})
        self.assertEqual(document, config())

    def test_late_preflight_conflict_prevents_all_creation(self):
        source = Mock(side_effect=[30, 10])
        dest = Mock(side_effect=[None, 99])
        with self.assertRaisesRegex(ValueError, 'destination repository ID mismatch'):
            forgejo_dispatch.plan_dispatch(config(), {'source_urls': f'{B},{A}', 'add_to_config': True},
                                            owner='local', port=3300, source_lookup=source,
                                            prepare_destination=dest)
        self.assertEqual([call.kwargs['create'] for call in dest.call_args_list], [False, False])

    def test_refuses_adoption_of_unpinned_existing_target(self):
        dest = Mock(return_value=40)
        with self.assertRaisesRegex(ValueError, 'unpinned existing destination'):
            forgejo_dispatch.plan_dispatch(config(), {'source_urls': B, 'add_to_config': True},
                                            owner='local', port=3300, source_lookup=Mock(return_value=30),
                                            prepare_destination=dest)
        self.assertEqual([call.kwargs['create'] for call in dest.call_args_list], [False])

    def test_pinned_add_noop_does_not_propose_publication(self):
        document = config()
        result = forgejo_dispatch.plan_dispatch(document, {'source_urls': A, 'add_to_config': 'true'},
                                                owner='local', port=3300, source_lookup=Mock(return_value=10),
                                                prepare_destination=Mock(return_value=20))
        self.assertEqual(result, (None, document, [A]))

    def test_destination_replacement_during_final_readback_refuses_publication(self):
        document = config()
        with self.assertRaisesRegex(ValueError, 'destination repository ID changed'):
            forgejo_dispatch.plan_dispatch(document, {'source_urls': B, 'add_to_config': True},
                                            owner='local', port=3300, source_lookup=Mock(return_value=30),
                                            prepare_destination=Mock(side_effect=[None, 40, 41]))
        self.assertEqual(document, config())

    def test_refuses_pinning_boolean_ids_and_rechecks_source_after_creation(self):
        for ids, dest_ids in (([True], []), ([30], [False]), ([30, 30, 31], [None, 40])):
            with self.subTest(ids=ids, dest_ids=dest_ids), self.assertRaisesRegex(ValueError, 'ID'):
                forgejo_dispatch.plan_dispatch(config(), {'source_urls': B, 'add_to_config': True},
                                                owner='local', port=3300, source_lookup=Mock(side_effect=ids),
                                                prepare_destination=Mock(side_effect=dest_ids))

    def test_gitlab_one_time_lookup_provides_runtime_only_source_id(self):
        url = 'https://gitlab.com/group/sub/project'
        source = Mock(return_value=50)
        dest = Mock()
        published, runtime, selected = forgejo_dispatch.plan_dispatch(
            config(), {'source_urls': url}, owner='local', port=3300,
            source_lookup=source, prepare_destination=dest)
        self.assertIsNone(published)
        self.assertEqual(selected, [url])
        self.assertEqual(runtime['repositories'][-1]['source'], {'url': url, 'id': 50})
        source.assert_called_once_with(url, 'gitlab')
        dest.assert_not_called()
