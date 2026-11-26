import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('forgejo_local_config_launcher', Path(__file__).resolve().parents[1]/'scheduler/auto_update.py')
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class LocalConfigLauncherTests(unittest.TestCase):
    def test_local_snapshot_pin_and_blob(self):
        raw = b'{"test":true}\n'
        repo = dict(id=31, private=True, has_actions=False, default_branch='main', full_name='forgejo-owner/control')
        file = dict(type='file', encoding='base64', content=base64.b64encode(raw).decode(), sha=hashlib.sha1(f'blob {len(raw)}\0'.encode()+raw).hexdigest())
        requests = []
        def get(url, token):
            requests.append((url,token))
            return file if '/contents/' in url else repo
        with patch.object(updater, 'local_json', side_effect=get):
            self.assertEqual(updater.local_manifest('forgejo-owner/control', 31, 3300, 'local-token'), raw)
            self.assertEqual(len(requests), 3)
            self.assertTrue(all(u.startswith('/api/v1/repos/forgejo-owner/control') for u, _ in requests))
            with self.assertRaises(ValueError):
                updater.local_manifest('forgejo-owner/control', 32, 3300, 'local-token')
            file['sha'] = '0' * 40
            with self.assertRaises(ValueError):
                updater.local_manifest('forgejo-owner/control', 31, 3300, 'local-token')

    def test_valid_manifest_accepts_auto_discoveries_and_discovered_by(self):
        base = {'destinations': {'forgejo-home': {}}, 'repositories': []}
        self.assertTrue(updater.valid_manifest({**base, 'auto_discoveries': []}))
        self.assertTrue(updater.valid_manifest({**base, 'auto_discoveries': [{'url': 'https://github.com/Alice'}]}))
        full = {
            'destinations': {'forgejo-home': {'account_type': 'personal'}},
            'host_concurrency_limits': {'default': 2, 'github.com': 1},
            'auto_discoveries': [
                {
                    'url': 'https://github.com/Alice',
                    'type': 'github',
                    'id': 123,
                    'visibility': 'public',
                    'ignore_releases': True,
                    'destination_name_prefix': 'pref',
                    'exclude': ['skip'],
                    'overrides': {
                        'beta': {'name': 'custom-beta', 'visibility': 'private', 'ignore_releases': False}
                    },
                },
                {'url': 'https://forge.example/Team'},
                {'url': 'https://gitlab.com/org/sub', 'type': 'gitlab', 'id': 7},
            ],
            'repositories': [
                {
                    'source': {'url': 'https://github.com/Alice/alpha', 'id': 11},
                    'mirrors': {'forgejo-home': {'name': 'pref-alpha', 'id': 21}},
                    'discovered_by': 'https://github.com/Alice',
                },
                {
                    'source': {'url': 'https://gitlab.com/org/sub/legacy', 'id': 12},
                    'mirrors': {'forgejo-home': {'name': 'gitlab-' + ('ab' * 12), 'id': 22}},
                    'discovered_by': 'https://gitlab.com/org/sub',
                },
            ],
        }
        self.assertTrue(updater.valid_manifest(full))

        # Coexistence with groups.
        with_groups = {
            'destinations': {'forgejo-home': {}},
            'groups': [{'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'team/sub', 'id': 7, 'repos': 'all'}],
            'auto_discoveries': [{'url': 'https://github.com/Alice'}],
            'repositories': [],
        }
        self.assertTrue(updater.valid_manifest(with_groups))

    def test_valid_manifest_rejects_malformed_auto_discoveries_and_preserves_legacy_rejections(self):
        def named(auto_discoveries=None, repositories=None, extra=None):
            doc = {'destinations': {'forgejo-home': {}}, 'repositories': list(repositories or [])}
            if auto_discoveries is not None:
                doc['auto_discoveries'] = auto_discoveries
            if extra:
                doc.update(extra)
            return doc

        bad_manifests = [
            named(auto_discoveries={}),
            named(auto_discoveries=['https://github.com/Alice']),
            named(auto_discoveries=[{}]),
            named(auto_discoveries=[{'url': 'https://github.com/Alice', 'unknown': True}]),
            named(auto_discoveries=[{'url': 'http://github.com/Alice'}]),
            named(auto_discoveries=[{'url': 'https://user@github.com/Alice'}]),
            named(auto_discoveries=[{'url': 'https://github.com/Alice/repo'}]),
            named(auto_discoveries=[{'url': 'https://github.com/Alice', 'type': 'forgejo'}]),
            named(auto_discoveries=[{'url': 'https://forge.example/Alice', 'type': 'github'}]),
            named(auto_discoveries=[{'url': 'https://gitlab.com/team/sub'}]),
            named(auto_discoveries=[{'url': 'https://github.com/Alice', 'id': True}]),
            named(auto_discoveries=[{'url': 'https://github.com/Alice', 'id': 0}]),
            named(auto_discoveries=[{'url': 'https://github.com/Alice', 'visibility': 'internal'}]),
            named(auto_discoveries=[{'url': 'https://github.com/Alice', 'ignore_releases': 'true'}]),
            named(auto_discoveries=[{'url': 'https://github.com/Alice', 'destination_name_prefix': ''}]),
            named(auto_discoveries=[{'url': 'https://github.com/Alice', 'destination_name_prefix': '..'}]),
            named(auto_discoveries=[{'url': 'https://github.com/Alice', 'exclude': ['A', 'a']}]),
            named(auto_discoveries=[{'url': 'https://github.com/Alice', 'exclude': ['bad/name']}]),
            named(auto_discoveries=[{'url': 'https://github.com/Alice', 'overrides': {'repo': {}}}]),
            named(auto_discoveries=[{'url': 'https://github.com/Alice', 'overrides': {'repo': {'name': 'a' * 101}}}]),
            named(auto_discoveries=[{'url': 'https://github.com/Alice', 'overrides': {'repo': {'name': 'gitlab-' + ('ab' * 12)}}}]),
            named(auto_discoveries=[{'url': 'https://github.com/Alice', 'exclude': ['Repo'], 'overrides': {'repo': {'visibility': 'public'}}}]),
            named(auto_discoveries=[{'url': 'https://github.com/Alice', 'id': 1}, {'url': 'https://GitHub.com/alice', 'id': 2}]),
            named(auto_discoveries=[{'url': 'https://github.com/Alice', 'id': 1}, {'url': 'https://github.com/Bob', 'id': 1}]),
            named(
                auto_discoveries=[{'url': 'https://github.com/Bob'}],
                repositories=[
                    {
                        'source': {'url': 'https://github.com/Alice/alpha', 'id': 1},
                        'mirrors': {'forgejo-home': {'name': 'mirror-Alice-alpha', 'id': 2}},
                        'discovered_by': 'https://github.com/Alice',
                    }
                ],
            ),
            named(
                auto_discoveries=[{'url': 'https://github.com/Alice'}],
                repositories=[
                    {
                        'source': {'url': 'https://github.com/Alice/alpha', 'id': 1},
                        'mirrors': {'forgejo-home': {'name': 'wrong-name', 'id': 2}},
                        'discovered_by': 'https://github.com/Alice',
                    }
                ],
            ),
            named(
                auto_discoveries=[{'url': 'https://github.com/Alice', 'overrides': {'a': {'name': 'Same'}}}],
                repositories=[
                    {
                        'source': {'url': 'https://github.com/Other/one', 'id': 1},
                        'mirrors': {'forgejo-home': {'name': 'same', 'id': 2}},
                    }
                ],
            ),
            named(auto_discoveries=[], extra={'unexpected_key': 1}),
            # Previously-rejected shapes stay rejected:
            [],
            ['http://github.com/a/b'],
            ['https://github.com/a/b.git'],
            ['https://github.com.evil.example/a/b'],
            ['https://forge.example/a/repo', 'https://else.example/b/REPO'],
            {'destinations': {}, 'repositories': []},
            {'destinations': {'a': {}, 'b': {}}, 'repositories': []},
        ]
        for bad in bad_manifests:
            with self.subTest(bad=bad):
                self.assertFalse(updater.valid_manifest(bad))

    def test_valid_manifest_groups_compatibility_and_reserved_names(self):
        gen_name = 'gitlab-' + ('ab' * 12)
        group_only = {
            'destinations': {'forgejo-home': {}},
            'repositories': [],
            'groups': [{'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'team/sub', 'id': 7, 'repos': 'all'}],
        }
        self.assertTrue(updater.valid_manifest(group_only))

        migrated_without_prefix = {
            'destinations': {'forgejo-home': {}},
            'auto_discoveries': [{'url': 'https://gitlab.com/team/sub', 'type': 'gitlab', 'id': 7}],
            'repositories': [
                {
                    'source': {'type': 'gitlab', 'url': 'https://gitlab.com/team/sub/proj', 'id': 42},
                    'mirrors': {'forgejo-home': {'name': gen_name, 'id': 9}},
                    'discovered_by': 'https://gitlab.com/team/sub',
                }
            ],
        }
        self.assertTrue(updater.valid_manifest(migrated_without_prefix))

        bad_variants = [
            # Duplicate group in groups + auto_discoveries (same id)
            {
                'destinations': {'forgejo-home': {}},
                'repositories': [],
                'groups': [{'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'team/sub', 'id': 7, 'repos': 'all'}],
                'auto_discoveries': [{'url': 'https://GitLab.com/other/group', 'type': 'gitlab', 'id': 7}],
            },
            # Duplicate group in groups + auto_discoveries (same owner)
            {
                'destinations': {'forgejo-home': {}},
                'repositories': [],
                'groups': [{'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'team/sub', 'id': 7, 'repos': 'all'}],
                'auto_discoveries': [{'url': 'https://gitlab.com/Team/Sub', 'type': 'gitlab', 'id': 99}],
            },
            # Duplicate owner inside groups itself
            {
                'destinations': {'forgejo-home': {}},
                'repositories': [],
                'groups': [
                    {'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'team/sub', 'id': 7, 'repos': 'all'},
                    {'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'Team/Sub', 'id': 8, 'repos': 'all'},
                ],
            },
            # destination_name_prefix rejected when existing gitlab-<24hex> child exists (with discovered_by)
            {
                'destinations': {'forgejo-home': {}},
                'auto_discoveries': [
                    {'url': 'https://gitlab.com/team/sub', 'type': 'gitlab', 'id': 7, 'destination_name_prefix': 'custom'}
                ],
                'repositories': [
                    {
                        'source': {'type': 'gitlab', 'url': 'https://gitlab.com/team/sub/proj', 'id': 42},
                        'mirrors': {'forgejo-home': {'name': gen_name, 'id': 9}},
                        'discovered_by': 'https://gitlab.com/team/sub',
                    }
                ],
            },
            # destination_name_prefix rejected when existing gitlab-<24hex> child exists (without discovered_by)
            {
                'destinations': {'forgejo-home': {}},
                'auto_discoveries': [
                    {'url': 'https://gitlab.com/team/sub', 'type': 'gitlab', 'id': 7, 'destination_name_prefix': 'custom'}
                ],
                'repositories': [
                    {
                        'source': {'type': 'gitlab', 'url': 'https://gitlab.com/team/sub/proj', 'id': 42},
                        'mirrors': {'forgejo-home': {'name': gen_name, 'id': 9}},
                    }
                ],
            },
            # Prefix producing reserved gitlab-<24hex>
            {
                'destinations': {'forgejo-home': {}},
                'auto_discoveries': [
                    {'url': 'https://github.com/Alice', 'destination_name_prefix': gen_name}
                ],
                'repositories': [],
            },
            # Override default name producing reserved gitlab-<24hex>
            {
                'destinations': {'forgejo-home': {}},
                'auto_discoveries': [
                    {
                        'url': 'https://github.com/Alice',
                        'destination_name_prefix': 'gitlab',
                        'overrides': {'ab' * 12: {'visibility': 'private'}},
                    }
                ],
                'repositories': [],
            },
            # Malformed group variants
            {
                'destinations': {'forgejo-home': {}},
                'repositories': [],
                'groups': [],
            },
            {
                'destinations': {'forgejo-home': {}},
                'repositories': [],
                'groups': [{'type': 'github', 'base_url': 'https://github.com', 'owner': 'Alice', 'id': 7, 'repos': 'all'}],
            },
        ]
        for bad in bad_variants:
            with self.subTest(bad=bad):
                self.assertFalse(updater.valid_manifest(bad))
