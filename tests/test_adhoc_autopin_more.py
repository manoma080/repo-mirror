import copy
import unittest
from repo_mirror import adhoc
from test_adhoc_autopin import URL, Source, Destination, config


def test_existing_unpinned_entry_gets_pins_and_keeps_custom_target():
    cfg = config()
    adhoc.add_to_config(cfg, URL, 'custom')
    dest = Destination()
    assert adhoc.add_to_config_pinned(cfg, URL, Source(), Source(), dest, owner='bot', account_type='personal')
    assert cfg['repositories'][0]['mirrors']['d'] == {'name': 'custom', 'id': 22}
    assert not adhoc.add_to_config_pinned(cfg, URL, Source(), Source(), dest, owner='bot', account_type='personal')


def test_existing_pin_mismatch_never_replaced():
    cfg = config()
    dest = Destination()
    adhoc.add_to_config_pinned(cfg, URL, Source(), Source(), dest, owner='bot', account_type='personal')
    original = copy.deepcopy(cfg)
    dest.repo['id'] = 99
    with unittest.TestCase().assertRaisesRegex(ValueError, 'destination repository id'):
        adhoc.add_to_config_pinned(cfg, URL, Source(), Source(), dest, owner='bot', account_type='personal')
    assert cfg == original


def test_replaced_source_cannot_adopt_interrupted_destination():
    cfg = config()
    dest = Destination()
    class ChangedSource(Source):
        calls = 0
        def request(self, method, url, *args):
            self.calls += 1
            return {'id': 11 if self.calls == 1 else 12, 'clone_url': 'https://github.com/alice/project.git'}
    with unittest.TestCase().assertRaisesRegex(ValueError, 'source repository id changed'):
        adhoc.add_to_config_pinned(cfg, URL, ChangedSource(), Source(), dest, owner='bot', account_type='personal')
    assert cfg == config()
    class Replacement(Source):
        def request(self, *args):
            return {'id': 12, 'clone_url': 'https://github.com/alice/project.git'}
    with unittest.TestCase().assertRaisesRegex(ValueError, 'unpinned existing destination'):
        adhoc.add_to_config_pinned(cfg, URL, Replacement(), Source(), dest, owner='bot', account_type='personal')
    assert cfg == config()


class AutopinMoreTests(unittest.TestCase):
    test_existing_unpinned_entry_gets_pins_and_keeps_custom_target = staticmethod(test_existing_unpinned_entry_gets_pins_and_keeps_custom_target)
    test_existing_pin_mismatch_never_replaced = staticmethod(test_existing_pin_mismatch_never_replaced)
    test_replaced_source_cannot_adopt_interrupted_destination = staticmethod(test_replaced_source_cannot_adopt_interrupted_destination)

class AdditionalIntegrationTests(unittest.TestCase):
    def test_created_destination_actions_disabled_and_verified(self):
        cfg = config()
        class ActionsDestination(Destination):
            def __init__(self):
                super().__init__()
                self.events = []
            def request(self, method, url, payload=None):
                if url.endswith('/actions/permissions'):
                    self.events.append((method, payload))
                    return {'enabled': False} if method == 'GET' else None
                return super().request(method, url, payload)
        dst = ActionsDestination()
        adhoc.add_to_config_pinned(cfg, URL, Source(), Source(), dst, owner='bot', account_type='personal')
        self.assertEqual(dst.events, [('PUT', {'enabled': False}), ('GET', None)])

    def test_existing_unpinned_markers_are_never_adopted(self):
        for suffix in ('', ' [repo-mirror:initializing:v1]'):
            with self.subTest(suffix=suffix):
                cfg = config()
                dst = Destination()
                dst.repo = {'id': 22, 'full_name': 'bot/mirror-alice-project',
                            'description': '[repo-mirror: github:https://github.com/alice/project]' + suffix}
                with self.assertRaisesRegex(ValueError, 'unpinned existing destination'):
                    adhoc.add_to_config_pinned(cfg, URL, Source(), Source(), dst, owner='bot', account_type='personal')
                self.assertEqual(cfg, config())

    def test_failed_actions_readback_leaves_config_unchanged(self):
        for response in ({'enabled': True}, {}, None):
            with self.subTest(response=response):
                cfg = config()
                class BadActions(Destination):
                    def request(self, method, url, payload=None):
                        if url.endswith('/actions/permissions'):
                            return response
                        return super().request(method, url, payload)
                dst = BadActions()
                with self.assertRaisesRegex(ValueError, 'Actions not verified disabled'):
                    adhoc.add_to_config_pinned(cfg, URL, Source(), Source(), dst, owner='bot', account_type='personal')
                self.assertEqual(cfg, config())
                self.assertIsNotNone(dst.repo)

    def test_forgejo_pins_via_forgejo_api(self):
        cfg = config()
        class Forge(Source):
            def request(self, method, url, *args):
                assert url == 'https://codeberg.org/api/v1/repos/alice/project'
                return {'id': 44, 'clone_url': 'https://codeberg.org/alice/project.git'}
        class Never(Source):
            def request(self, *args):
                raise AssertionError('GitHub source used for Forgejo')
        adhoc.add_to_config_pinned(cfg, 'https://codeberg.org/alice/project', Never(), Forge(), Destination(), owner='bot', account_type='personal')
        self.assertEqual(cfg['repositories'][0]['source']['id'], 44)

    def test_existing_source_pin_mismatch_is_not_replaced(self):
        cfg = config()
        dest = Destination()
        adhoc.add_to_config_pinned(cfg, URL, Source(), Source(), dest, owner='bot', account_type='personal')
        original = copy.deepcopy(cfg)
        class Changed(Source):
            def request(self, *args):
                return {'id': 55, 'clone_url': 'https://github.com/alice/project.git'}
        with self.assertRaisesRegex(ValueError, 'source repository id mismatch'):
            adhoc.add_to_config_pinned(cfg, URL, Changed(), Source(), dest, owner='bot', account_type='personal')
        self.assertEqual(cfg, original)

    def test_wrong_source_clone_identity_never_creates_destination(self):
        cfg = config()
        dst = Destination()
        class Redirected(Source):
            def request(self, *args):
                return {'id': 11, 'clone_url': 'https://github.com/attacker/project.git'}
        with self.assertRaisesRegex(ValueError, 'source clone URL'):
            adhoc.add_to_config_pinned(cfg, URL, Redirected(), Source(), dst, owner='bot', account_type='personal')
        self.assertIsNone(dst.repo)
        self.assertEqual(cfg, config())
