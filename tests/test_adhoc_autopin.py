import copy
from urllib.error import HTTPError

import unittest
from unittest.mock import patch
import tempfile
import json
from pathlib import Path
import os

from repo_mirror import adhoc

URL = 'https://github.com/alice/project'

class Source:
    def request(self, method, url, *args):
        return {'id': 11, 'clone_url': 'https://github.com/alice/project.git'}

class Destination:
    def __init__(self):
        self.repo = None
    def request(self, method, url, payload=None):
        if url.endswith('/actions/permissions'):
            return {'enabled': False} if method == 'GET' else None
        if url.endswith('/user'):
            return {'login': 'bot'}
        if method == 'GET':
            if self.repo is None:
                raise HTTPError(url, 404, 'missing', {}, None)
            return self.repo.copy()
        assert method == 'POST'
        self.repo = {'id': 22, 'full_name': 'bot/' + payload['name'], 'description': payload['description']}
        return self.repo.copy()

def config():
    return {'destinations': {'d': {'type': 'github', 'url': 'https://github.com', 'owner': 'bot', 'auth': 'mirrorbot'}}, 'repositories': []}

def test_add_creates_and_pins_destination():
    cfg = config()
    assert adhoc.add_to_config_pinned(cfg, URL, Source(), Source(), Destination(), owner='bot', account_type='personal', destination='custom')
    entry = cfg['repositories'][0]
    assert entry['source']['id'] == 11
    assert entry['mirrors']['d'] == {'name': 'custom', 'id': 22}

def test_cli_autopin_uses_destination_token_and_writes_pins():
    from repo_mirror import github_api
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'config.json'
        path.write_text(json.dumps(config()))
        dst = Destination()
        app = object()
        with patch.dict(os.environ, {'GH_TOKEN': 'wrong-token', 'DESTINATION_OWNER': 'bot',
                                     'MIRROR_APP_ID': '123', 'MIRROR_APP_PRIVATE_KEY': 'key'}), \
             patch.object(github_api, 'AppTokenProvider', return_value=app) as provider, \
             patch.object(github_api, 'SourceAPI', side_effect=lambda token: Source()) as source, \
             patch.object(github_api, 'API', side_effect=lambda token=None: dst if token is app else Source()) as api:
            adhoc.main(['--config', str(path), '--urls', URL, '--auto-pin'])
        provider.assert_called_once_with('123', 'key', 'bot', 'personal')
        source.assert_called_once_with(app)
        assert api.call_args_list[-1].args == (app,)
        assert json.loads(path.read_text())['repositories'][0]['source']['id'] == 11


def test_failure_leaves_config_unchanged():
    cfg = config()
    before = copy.deepcopy(cfg)
    class Bad(Source):
        def request(self, *args):
            return {'id': True}
    with unittest.TestCase().assertRaises(ValueError):
        adhoc.add_to_config_pinned(cfg, URL, Bad(), Source(), Destination(), owner='bot', account_type='personal')
    assert cfg == before


class AutopinTests(unittest.TestCase):
    test_add_creates_and_pins_destination = staticmethod(test_add_creates_and_pins_destination)
    test_cli_autopin_uses_destination_token_and_writes_pins = staticmethod(test_cli_autopin_uses_destination_token_and_writes_pins)
    test_failure_leaves_config_unchanged = staticmethod(test_failure_leaves_config_unchanged)

    def test_replaced_destination_is_not_mutated(self):
        class Replaced(Destination):
            def __init__(self):
                super().__init__()
                self.puts = []

            def request(self, method, url, payload=None):
                if method == 'PUT':
                    self.puts.append(url)
                if method == 'POST':
                    created = super().request(method, url, payload)
                    assert self.repo is not None
                    self.repo['id'] = 99
                    return created
                return super().request(method, url, payload)

        cfg = config()
        before = copy.deepcopy(cfg)
        dst = Replaced()
        with self.assertRaises(ValueError):
            adhoc.add_to_config_pinned(cfg, URL, Source(), Source(), dst, owner='bot', account_type='personal')
        self.assertEqual(dst.puts, [])
        self.assertEqual(cfg, before)

    def test_identity_checks_precede_actions_mutation(self):
        for fault in ('invalid_created_id', 'destination_name', 'destination_marker',
                      'source_id', 'source_clone'):
            with self.subTest(fault=fault):
                class FaultyDestination(Destination):
                    puts = 0

                    def request(self, method, url, payload=None):
                        if method == 'PUT':
                            self.puts += 1
                        result = super().request(method, url, payload)
                        if method == 'POST':
                            assert self.repo is not None
                            if fault == 'invalid_created_id':
                                result['id'] = True
                            elif fault == 'destination_name':
                                self.repo['full_name'] = 'bot/other'
                            elif fault == 'destination_marker':
                                self.repo['description'] = 'not ours'
                        return result

                dst = FaultyDestination()

                class FaultySource(Source):
                    def request(self, method, url, *args):
                        result = super().request(method, url, *args)
                        if dst.repo is not None:
                            if fault == 'source_id':
                                result['id'] = 99
                            elif fault == 'source_clone':
                                result['clone_url'] = 'https://github.com/alice/other.git'
                        return result

                cfg = config()
                before = copy.deepcopy(cfg)
                src = FaultySource()
                with self.assertRaises((ValueError, RuntimeError)):
                    adhoc.add_to_config_pinned(cfg, URL, src, src, dst, owner='bot', account_type='personal')
                self.assertEqual(dst.puts, 0)
                self.assertEqual(cfg, before)
