"""Offline tests for ``auto_discoveries`` on the Forgejo runner."""

import base64
import copy
import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from repo_mirror import forgejo_schedule
from repo_mirror.auto_discoveries_onboarding import (
    ForgejoDestinationOnboardingAdapter,
    PartialOnboarding,
    PlannedInduction,
    onboard_auto_discoveries_forgejo,
)
from repo_mirror.forgejo_automatic_transition import prepare_source_transition
from repo_mirror.forgejo_control_config import ForgejoFile
from repo_mirror.forgejo_git import APIError
from repo_mirror.forgejo_sync import normalize_named, parse_manifest
from repo_mirror.migrations import blob_sha


def _http_error(url, code):
    return HTTPError(url, code, f'HTTP {code}', Message(), None)


def _encode(doc):
    return (json.dumps(doc, indent=2, ensure_ascii=False) + '\n').encode('utf-8')


def _base_doc(auto_discoveries=None, repositories=None, *, groups=None, account_type='personal'):
    doc = {
        'destinations': {'forgejo-home': {'account_type': account_type}},
        'repositories': list(repositories or []),
    }
    if auto_discoveries is not None:
        doc['auto_discoveries'] = auto_discoveries
    if groups is not None:
        doc['groups'] = groups
    return doc


class FakeSourceAPI:
    def __init__(self, routes=None, pages_routes=None):
        self.routes = dict(routes or {})
        self.pages_routes = dict(pages_routes or {})
        self.requests = []
        self.page_requests = []

    def request(self, method, url, data=None):
        self.requests.append((method, url))
        if url not in self.routes:
            raise _http_error(url, 404)
        value = self.routes[url]
        if isinstance(value, Exception):
            raise value
        return value

    def pages(self, url):
        self.page_requests.append(url)
        if url not in self.pages_routes:
            raise _http_error(url, 404)
        value = self.pages_routes[url]
        if isinstance(value, Exception):
            raise value
        return list(value)


class FakeForgejoAPI:
    """In-memory Forgejo API supporting repo creation, readback, and control repo CAS files."""

    def __init__(
        self,
        owner='dest',
        *,
        existing=None,
        control_repo=None,
        control_id=99,
        control_files=None,
    ):
        self.owner = owner
        self.repos = dict(existing or {})
        self.calls = []
        self._next_id = 7000
        self.fail_on_create = None
        self.post_has_actions = False
        self.readback_has_actions = None
        self.control_repo = control_repo
        self.control_id = control_id
        self.control_files = dict(control_files or {})
        self.cas_conflict_next = False
        self.replace_puts = 0

    def request(self, method, path, payload=None, *, token=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if self.control_repo:
            ctrl_prefix = f'/api/v1/repos/{self.control_repo}'
            if method == 'GET' and path == ctrl_prefix:
                return {
                    'id': self.control_id,
                    'full_name': self.control_repo,
                    'private': True,
                    'has_actions': False,
                }
            contents_prefix = f'{ctrl_prefix}/contents/'
            if path.startswith(contents_prefix):
                rel = path[len(contents_prefix):]
                if method == 'GET':
                    file_path = rel.split('?', 1)[0]
                    if file_path not in self.control_files:
                        raise APIError(404)
                    raw = self.control_files[file_path]
                    return {
                        'type': 'file',
                        'encoding': 'base64',
                        'content': base64.b64encode(raw).decode('ascii'),
                        'sha': blob_sha(raw),
                    }
                if method == 'PUT':
                    if self.cas_conflict_next:
                        self.cas_conflict_next = False
                        raise APIError(409)
                    if rel not in self.control_files:
                        raise APIError(404)
                    current = self.control_files[rel]
                    if payload.get('sha') != blob_sha(current):
                        raise APIError(409)
                    if rel == 'config.json':
                        self.replace_puts += 1
                    self.control_files[rel] = base64.b64decode(payload['content'].encode('ascii'))
                    return {'content': {'sha': blob_sha(self.control_files[rel])}}

        if method == 'GET' and path == '/api/v1/user':
            return {'login': self.owner}
        prefix = f'/api/v1/repos/{self.owner}/'
        if path.startswith(prefix) and method == 'GET':
            name = path[len(prefix):]
            repo = self.repos.get(name.lower())
            if repo is None:
                raise APIError(404)
            out = dict(repo)
            if self.readback_has_actions is not None:
                out['has_actions'] = self.readback_has_actions
            return out
        if method == 'POST' and path in ('/api/v1/user/repos', f'/api/v1/orgs/{self.owner}/repos'):
            name = payload['name']
            if self.fail_on_create is not None and name == self.fail_on_create:
                raise APIError(500)
            self._next_id += 1
            repo = {
                'id': self._next_id,
                'name': name,
                'full_name': f'{self.owner}/{name}',
                'private': bool(payload['private']),
                'description': payload['description'],
                'has_actions': self.post_has_actions,
            }
            self.repos[name.lower()] = repo
            return dict(repo)
        raise AssertionError(f'unexpected Forgejo API call: {method} {path}')


class ForgejoSchemaAndTransitionTests(unittest.TestCase):
    def test_normalize_named_and_parse_manifest_accept_auto_discoveries_and_discovered_by(self):
        doc = _base_doc(
            [
                {
                    'url': 'https://github.com/Alice',
                    'id': 10,
                    'visibility': 'public',
                    'ignore_releases': True,
                    'destination_name_prefix': 'pref',
                    'exclude': ['skip'],
                    'overrides': {'beta': {'name': 'custom-beta', 'visibility': 'private', 'ignore_releases': False}},
                },
                {'url': 'https://forge.example/Team'},
                {'url': 'https://gitlab.com/org/sub', 'id': 77},
            ],
            [
                {
                    'source': {'url': 'https://github.com/Alice/alpha', 'id': 11},
                    'mirrors': {'forgejo-home': {'id': 21}},
                    'discovered_by': 'https://github.com/Alice',
                },
                {
                    'source': {'url': 'https://gitlab.com/org/sub/legacy-proj', 'id': 12},
                    'mirrors': {'forgejo-home': {'name': 'gitlab-' + ('ab' * 12), 'id': 22}},
                    'discovered_by': 'https://gitlab.com/org/sub',
                },
            ],
        )
        normalized = normalize_named(doc)
        self.assertEqual(normalized[0]['destination_name'], 'pref-alpha')
        self.assertEqual(normalized[0]['discovered_by'], 'https://github.com/Alice')
        self.assertEqual(normalized[1]['destination_name'], 'gitlab-' + ('ab' * 12))
        entries = parse_manifest(doc)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]['destination_name'], 'pref-alpha')
        self.assertEqual(entries[0]['discovered_by'], 'https://github.com/Alice')

    def test_normalize_named_rejects_invalid_discovered_by_and_override_collisions(self):
        bad_docs = [
            _base_doc(
                None,
                [
                    {
                        'source': {'url': 'https://github.com/Alice/alpha', 'id': 11},
                        'mirrors': {'forgejo-home': {'name': 'mirror-Alice-alpha', 'id': 21}},
                        'discovered_by': 'https://github.com/Alice',
                    }
                ],
            ),
            _base_doc(
                [{'url': 'https://github.com/Alice'}],
                [
                    {
                        'source': {'url': 'https://github.com/Bob/alpha', 'id': 11},
                        'mirrors': {'forgejo-home': {'name': 'mirror-Alice-alpha', 'id': 21}},
                        'discovered_by': 'https://github.com/Alice',
                    }
                ],
            ),
            _base_doc(
                [{'url': 'https://github.com/Alice'}],
                [
                    {
                        'source': {'url': 'https://github.com/Alice/alpha', 'id': 11},
                        'mirrors': {'forgejo-home': {'name': 'wrong-name', 'id': 21}},
                        'discovered_by': 'https://github.com/Alice',
                    }
                ],
            ),
            _base_doc(
                [{'url': 'https://github.com/Alice', 'overrides': {'alpha': {'name': 'Shared'}}}],
                [
                    {
                        'source': {'url': 'https://github.com/Other/one', 'id': 11},
                        'mirrors': {'forgejo-home': {'name': 'shared', 'id': 21}},
                    }
                ],
            ),
            _base_doc(
                [
                    {'url': 'https://github.com/Alice', 'overrides': {'a': {'name': 'Same'}}},
                    {'url': 'https://github.com/Bob', 'overrides': {'b': {'name': 'same'}}},
                ]
            ),
        ]
        for bad in bad_docs:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_manifest(bad)

    def test_prepare_source_transition_preserves_and_drops_discovered_by(self):
        secret = 'x' * 32
        doc = _base_doc(
            [{'url': 'https://github.com/Alice', 'id': 10, 'destination_name_prefix': 'pref'}],
            [
                {
                    'source': {'url': 'https://github.com/Alice/alpha', 'id': 11},
                    'mirrors': {'forgejo-home': {'name': 'pref-alpha', 'id': 21}},
                    'discovered_by': 'https://github.com/Alice',
                }
            ],
        )
        same_owner = prepare_source_transition(
            doc,
            0,
            {
                'id': 11,
                'full_name': 'Alice/beta',
                'html_url': 'https://github.com/Alice/beta',
                'clone_url': 'https://github.com/Alice/beta.git',
            },
            3300,
            'dest',
            secret,
        )
        self.assertEqual(same_owner['repositories'][0]['discovered_by'], 'https://github.com/Alice')
        self.assertEqual(same_owner['repositories'][0]['mirrors']['forgejo-home']['name'], 'pref-beta')
        parse_manifest(same_owner)

        cross_owner = prepare_source_transition(
            doc,
            0,
            {
                'id': 11,
                'full_name': 'Bob/alpha',
                'html_url': 'https://github.com/Bob/alpha',
                'clone_url': 'https://github.com/Bob/alpha.git',
            },
            3300,
            'dest',
            secret,
        )
        self.assertNotIn('discovered_by', cross_owner['repositories'][0])
        self.assertEqual(cross_owner['repositories'][0]['mirrors']['forgejo-home']['name'], 'pref-alpha')
        parse_manifest(cross_owner)


class ForgejoDestinationAdapterTests(unittest.TestCase):
    def test_adapter_methods_personal_and_org_without_actions_endpoint(self):
        api = FakeForgejoAPI(owner='dest')
        adapter = ForgejoDestinationOnboardingAdapter(api, 'dest', organization=False)
        self.assertEqual(adapter.owner_identity(), 'dest')
        self.assertIsNone(adapter.get_repository('missing'))

        item = PlannedInduction(
            name='alpha',
            source_id=11,
            clone_url='https://github.com/Alice/alpha.git',
            full_name='Alice/alpha',
            dest_name='mirror-Alice-alpha',
            visibility='private',
            ignore_releases=False,
            source_type='github',
            base_url='https://github.com',
            owner='Alice',
            rule_url='https://github.com/Alice',
        )
        marker = adapter.marker_for(item)
        self.assertEqual(marker, 'repo-mirror-source: github.com/Alice/alpha')
        dest_id = adapter.create_repository(item.dest_name, True, marker)
        adapter.disable_actions(item.dest_name)
        readback = adapter.verify_repository(item.dest_name, dest_id)
        self.assertEqual(readback['id'], dest_id)
        self.assertFalse(any('/actions/' in path for _, path, _ in api.calls))

        org_api = FakeForgejoAPI(owner='myorg')
        org_adapter = ForgejoDestinationOnboardingAdapter(org_api, 'myorg', organization=True)
        self.assertEqual(org_adapter.owner_identity(), 'myorg')
        org_id = org_adapter.create_repository('repo-one', True, 'repo-mirror-source: forge.example/team/repo-one')
        org_adapter.disable_actions('repo-one')
        self.assertEqual(org_adapter.verify_repository('repo-one', org_id)['full_name'], 'myorg/repo-one')
        self.assertTrue(any(path == '/api/v1/orgs/myorg/repos' for _, path, _ in org_api.calls))


class ForgejoOnboardingEndToEndTests(unittest.TestCase):
    def test_induction_autopin_exclude_overrides_visibility_and_steady_state(self):
        doc = _base_doc(
            [
                {
                    'url': 'https://github.com/Alice',
                    'visibility': 'public',
                    'exclude': ['SkipMe'],
                    'overrides': {
                        'beta': {'name': 'custom-beta', 'visibility': 'private', 'ignore_releases': True},
                        'gamma': {'visibility': 'public'},
                    },
                }
            ]
        )
        raw = _encode(doc)
        api = FakeForgejoAPI(
            owner='dest',
            control_repo='dest/control',
            control_files={
                'config.json': raw,
                'migrations.json': b'{"version": 1, "migrations": {}, "signatures": {}}',
                'failures.json': b'{"version": 1, "journals": {}}\n',
            },
        )
        cas = ForgejoFile(api, 'dest', 'control', 'config.json', expected_id=99)
        src = FakeSourceAPI(
            routes={'https://api.github.com/users/Alice': {'id': 1234, 'login': 'Alice', 'type': 'User'}},
            pages_routes={
                'https://api.github.com/users/Alice/repos?type=owner': [
                    {
                        'id': 11,
                        'name': 'alpha',
                        'full_name': 'Alice/alpha',
                        'clone_url': 'https://github.com/Alice/alpha.git',
                        'private': False,
                    },
                    {
                        'id': 12,
                        'name': 'beta',
                        'full_name': 'Alice/beta',
                        'clone_url': 'https://github.com/Alice/beta.git',
                        'private': False,
                    },
                    {
                        # Private source must remain private even when override requests public.
                        'id': 13,
                        'name': 'gamma',
                        'full_name': 'Alice/gamma',
                        'clone_url': 'https://github.com/Alice/gamma.git',
                        'private': True,
                    },
                    {
                        'id': 14,
                        'name': 'skipme',
                        'full_name': 'Alice/skipme',
                        'clone_url': 'https://github.com/Alice/skipme.git',
                        'private': False,
                    },
                ]
            },
        )
        stream = io.StringIO()
        res = onboard_auto_discoveries_forgejo(raw, cas, lambda _: src, api, owner='dest', stream=stream)
        self.assertEqual(api.replace_puts, 1)
        self.assertEqual(res.inducted_total, 3)

        published = json.loads(res.config_bytes)
        self.assertEqual(published['auto_discoveries'][0]['id'], 1234)
        by_name = {entry['mirrors']['forgejo-home']['name']: entry for entry in published['repositories']}
        self.assertEqual(by_name['mirror-Alice-alpha']['mirrors']['forgejo-home']['visibility'], 'public')
        self.assertFalse(api.repos['mirror-alice-alpha']['private'])
        self.assertEqual(by_name['custom-beta']['mirrors']['forgejo-home']['visibility'], 'private')
        self.assertTrue(by_name['custom-beta']['mirrors']['forgejo-home']['ignore_releases'])
        self.assertTrue(api.repos['custom-beta']['private'])
        self.assertEqual(by_name['mirror-Alice-gamma']['mirrors']['forgejo-home']['visibility'], 'private')
        self.assertTrue(api.repos['mirror-alice-gamma']['private'])
        self.assertNotIn('mirror-Alice-skipme', by_name)

        self.assertIn('auto_discovery 1/1 (Alice): OK discovered=4 inducted=3 excluded=1 tracked=0', stream.getvalue())
        self.assertNotIn('https://', stream.getvalue())
        self.assertFalse(any('/actions/' in path for _, path, _ in api.calls))

        # Steady-state re-run: zero repo creations, zero CAS PUTs, authoritative readback still occurs.
        api.calls.clear()
        steady_stream = io.StringIO()
        steady = onboard_auto_discoveries_forgejo(
            res.config_bytes, cas, lambda _: src, api, owner='dest', stream=steady_stream
        )
        self.assertEqual(steady.config_bytes, res.config_bytes)
        self.assertEqual(api.replace_puts, 1)
        self.assertFalse(any(method == 'POST' for method, _, _ in api.calls))
        self.assertTrue(any(path.endswith('/contents/config.json?ref=refs/heads/main') for _, path, _ in api.calls))
        self.assertIn('auto_discovery 1/1 (Alice): OK discovered=4 inducted=0 excluded=1 tracked=3', steady_stream.getvalue())

    def test_gitlab_public_source_respects_public_rule_visibility(self):
        doc = _base_doc(
            [{'url': 'https://gitlab.com/team/sub', 'id': 7, 'visibility': 'public'}]
        )
        raw = _encode(doc)
        api = FakeForgejoAPI(
            owner='dest',
            control_repo='dest/control',
            control_files={
                'config.json': raw,
                'migrations.json': b'{"version": 1, "migrations": {}, "signatures": {}}',
                'failures.json': b'{"version": 1, "journals": {}}\n',
            },
        )
        cas = ForgejoFile(api, 'dest', 'control', 'config.json', expected_id=99)
        src = FakeSourceAPI(
            routes={
                'https://gitlab.com/api/v4/groups/team%2Fsub': {
                    'id': 7,
                    'visibility': 'public',
                    'full_path': 'team/sub',
                }
            },
            pages_routes={
                'https://gitlab.com/api/v4/groups/team%2Fsub/projects?include_subgroups=false&with_shared=false': [
                    {
                        'id': 701,
                        'path': 'proj',
                        'visibility': 'public',
                        'path_with_namespace': 'team/sub/proj',
                        'http_url_to_repo': 'https://gitlab.com/team/sub/proj.git',
                    }
                ]
            },
        )
        res = onboard_auto_discoveries_forgejo(raw, cas, lambda _: src, api, owner='dest', stream=io.StringIO())
        published = json.loads(res.config_bytes)
        self.assertEqual(published['repositories'][0]['mirrors']['forgejo-home']['visibility'], 'public')
        self.assertFalse(api.repos['mirror-team-sub-proj']['private'])

    def test_cas_conflict_raises_partial_onboarding_and_refuses_adoption(self):
        doc = _base_doc([{'url': 'https://github.com/Alice', 'id': 100}])
        raw = _encode(doc)
        api = FakeForgejoAPI(
            owner='dest',
            control_repo='dest/control',
            control_files={
                'config.json': raw,
                'migrations.json': b'{"version": 1, "migrations": {}, "signatures": {}}',
                'failures.json': b'{"version": 1, "journals": {}}\n',
            },
        )
        api.cas_conflict_next = True
        cas = ForgejoFile(api, 'dest', 'control', 'config.json', expected_id=99)
        src = FakeSourceAPI(
            routes={'https://api.github.com/users/Alice': {'id': 100, 'login': 'Alice', 'type': 'User'}},
            pages_routes={
                'https://api.github.com/users/Alice/repos?type=owner': [
                    {
                        'id': 11,
                        'name': 'alpha',
                        'full_name': 'Alice/alpha',
                        'clone_url': 'https://github.com/Alice/alpha.git',
                        'private': False,
                    }
                ]
            },
        )
        with self.assertRaises(PartialOnboarding) as ctx:
            onboard_auto_discoveries_forgejo(raw, cas, lambda _: src, api, owner='dest', stream=io.StringIO())
        self.assertEqual(ctx.exception.names, ('mirror-Alice-alpha',))
        self.assertEqual(cas.read(), raw)
        self.assertIn('mirror-alice-alpha', api.repos)

        # Retry refuses unpinned remote destination.
        with self.assertRaisesRegex(ValueError, 'unpinned existing destination requires manual reconciliation'):
            onboard_auto_discoveries_forgejo(raw, cas, lambda _: src, api, owner='dest', stream=io.StringIO())

    def test_zero_discovery_guard(self):
        existing_child = {
            'source': {'url': 'https://github.com/Alice/alpha', 'id': 11},
            'mirrors': {'forgejo-home': {'name': 'mirror-Alice-alpha', 'id': 7001, 'visibility': 'private'}},
            'discovered_by': 'https://github.com/Alice',
        }
        doc = _base_doc([{'url': 'https://github.com/Alice', 'id': 100}], [existing_child])
        raw = _encode(doc)
        api = FakeForgejoAPI(
            owner='dest',
            control_repo='dest/control',
            control_files={
                'config.json': raw,
                'migrations.json': b'{"version": 1, "migrations": {}, "signatures": {}}',
                'failures.json': b'{"version": 1, "journals": {}}\n',
            },
        )
        cas = ForgejoFile(api, 'dest', 'control', 'config.json', expected_id=99)
        empty_src = FakeSourceAPI(
            routes={'https://api.github.com/users/Alice': {'id': 100, 'login': 'Alice', 'type': 'User'}},
            pages_routes={'https://api.github.com/users/Alice/repos?type=owner': []},
        )
        with self.assertRaisesRegex(
            ValueError, 'auto-discovery returned zero repositories for a rule with existing entries'
        ):
            onboard_auto_discoveries_forgejo(raw, cas, lambda _: empty_src, api, owner='dest', stream=io.StringIO())
        self.assertEqual(api.replace_puts, 0)

    def test_no_actions_verification_fails_closed_on_post_and_readback(self):
        doc = _base_doc([{'url': 'https://github.com/Alice', 'id': 100}])
        raw = _encode(doc)
        src = FakeSourceAPI(
            routes={'https://api.github.com/users/Alice': {'id': 100, 'login': 'Alice', 'type': 'User'}},
            pages_routes={
                'https://api.github.com/users/Alice/repos?type=owner': [
                    {
                        'id': 11,
                        'name': 'alpha',
                        'full_name': 'Alice/alpha',
                        'clone_url': 'https://github.com/Alice/alpha.git',
                        'private': False,
                    }
                ]
            },
        )

        api_post = FakeForgejoAPI(owner='dest', control_repo='dest/control', control_files={'config.json': raw})
        api_post.post_has_actions = True
        cas_post = ForgejoFile(api_post, 'dest', 'control', 'config.json', expected_id=99)
        with self.assertRaises(PartialOnboarding) as exc_post:
            onboard_auto_discoveries_forgejo(raw, cas_post, lambda _: src, api_post, owner='dest', stream=io.StringIO())
        self.assertIn('Actions not verified', str(exc_post.exception.__cause__))
        self.assertEqual(api_post.replace_puts, 0)

        api_read = FakeForgejoAPI(owner='dest', control_repo='dest/control', control_files={'config.json': raw})
        api_read.readback_has_actions = True
        cas_read = ForgejoFile(api_read, 'dest', 'control', 'config.json', expected_id=99)
        with self.assertRaises(PartialOnboarding) as exc_read:
            onboard_auto_discoveries_forgejo(raw, cas_read, lambda _: src, api_read, owner='dest', stream=io.StringIO())
        self.assertIn('Actions not verified', str(exc_read.exception.__cause__))
        self.assertEqual(api_read.replace_puts, 0)

    def test_groups_and_auto_discoveries_collision_refused_before_writes(self):
        # A GitLab group entry and an auto_discoveries rule producing the same destination name must fail closed.
        group_name = 'gitlab-' + hashlib.sha256(b'https://gitlab.com\0' + b'7\0' + b'701').hexdigest()[:24]
        doc = _base_doc(
            [
                {
                    'url': 'https://github.com/Alice',
                    'id': 100,
                    'overrides': {'alpha': {'name': group_name}},
                }
            ],
            groups=[{'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'team/sub', 'id': 7, 'repos': 'all'}],
        )
        with self.assertRaises(ValueError):
            parse_manifest({k: v for k, v in doc.items() if k != 'groups'})

        # Even if auto_discoveries uses a prefix matching an explicit/groups entry's destination name:
        existing_group_entry = {
            'source': {'type': 'gitlab', 'url': 'https://gitlab.com/team/sub/proj', 'id': 701},
            'mirrors': {'forgejo-home': {'name': 'shared-proj', 'id': 9001}},
        }
        collision_doc = _base_doc(
            [{'url': 'https://github.com/Alice', 'id': 100, 'destination_name_prefix': 'shared'}],
            [existing_group_entry],
            groups=[{'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'team/sub', 'id': 7, 'repos': 'all'}],
        )
        raw = _encode(collision_doc)
        api = FakeForgejoAPI(
            owner='dest',
            control_repo='dest/control',
            control_files={
                'config.json': raw,
                'migrations.json': b'{"version": 1, "migrations": {}, "signatures": {}}',
                'failures.json': b'{"version": 1, "journals": {}}\n',
            },
        )
        cas = ForgejoFile(api, 'dest', 'control', 'config.json', expected_id=99)
        src = FakeSourceAPI(
            routes={'https://api.github.com/users/Alice': {'id': 100, 'login': 'Alice', 'type': 'User'}},
            pages_routes={
                'https://api.github.com/users/Alice/repos?type=owner': [
                    {
                        'id': 11,
                        'name': 'proj',
                        'full_name': 'Alice/proj',
                        'clone_url': 'https://github.com/Alice/proj.git',
                        'private': False,
                    }
                ]
            },
        )
        with self.assertRaisesRegex(ValueError, 'duplicate destination repository'):
            onboard_auto_discoveries_forgejo(raw, cas, lambda _: src, api, owner='dest', stream=io.StringIO())
        self.assertFalse(any(method == 'POST' for method, _, _ in api.calls))
        self.assertEqual(api.replace_puts, 0)


class ForgejoScheduleIntegrationTests(unittest.TestCase):
    def test_missing_control_store_fails_closed_before_lock(self):
        doc = _base_doc([{'url': 'https://github.com/Alice'}])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / 'manifest'
            manifest.write_bytes(_encode(doc))
            status = forgejo_schedule.main(
                [
                    'run',
                    '--manifest',
                    str(manifest),
                    '--token-file',
                    'unused',
                    '--timestamp-secret-file',
                    'unused',
                    '--journal-dir',
                    directory,
                    '--owner',
                    'dest',
                    '--lock',
                    str(root / 'lock'),
                ]
            )
        self.assertEqual(status, 2)
        self.assertFalse((root / 'lock').exists())

    def test_scheduler_runs_onboarding_against_forgejo_control_repo_before_workers(self):
        orphan_child = {
            'source': {'url': 'https://github.com/Alice/gone', 'id': 99},
            'mirrors': {'forgejo-home': {'name': 'mirror-Alice-gone', 'id': 7001, 'visibility': 'private'}},
            'discovered_by': 'https://github.com/Alice',
        }
        doc = _base_doc([{'url': 'https://github.com/Alice'}], [orphan_child])
        raw = _encode(doc)
        api = FakeForgejoAPI(
            owner='dest',
            control_repo='dest/control',
            control_files={
                'config.json': raw,
                'migrations.json': b'{"version": 1, "migrations": {}, "signatures": {}}',
                'failures.json': b'{"version": 1, "journals": {}}\n',
            },
        )
        src = FakeSourceAPI(
            routes={'https://api.github.com/users/Alice': {'id': 100, 'login': 'Alice', 'type': 'User'}},
            pages_routes={
                'https://api.github.com/users/Alice/repos?type=owner': [
                    {
                        'id': 11,
                        'name': 'alpha',
                        'full_name': 'Alice/alpha',
                        'clone_url': 'https://github.com/Alice/alpha.git',
                        'private': False,
                    }
                ]
            },
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'manifest').write_bytes(raw)
            (root / 'secret').write_text('x' * 32)
            (root / 'secret').chmod(0o600)
            (root / 'token').write_text('forgejo-token')
            (root / 'token').chmod(0o600)
            (root / 'journal').mkdir(mode=0o700)

            events = []

            def spawn(command, **kwargs):
                snapshot = json.loads(Path(command[command.index('--config') + 1]).read_text())
                events.append(('worker', command[-1], len(snapshot['repositories']), snapshot['auto_discoveries'][0].get('id')))
                return Mock(poll=lambda: 0, pid=999999999, wait=lambda: 0)

            stdout = io.StringIO()
            with (
                patch('repo_mirror.forgejo_control_config.ForgejoControlAPI', return_value=api),
                patch('repo_mirror.forgejo_schedule._build_auto_discovery_source_client', return_value=lambda _: src),
                patch('repo_mirror.forgejo_schedule.subprocess.Popen', side_effect=spawn),
                redirect_stdout(stdout),
            ):
                status = forgejo_schedule.main(
                    [
                        'run',
                        '--manifest',
                        str(root / 'manifest'),
                        '--token-file',
                        str(root / 'token'),
                        '--timestamp-secret-file',
                        str(root / 'secret'),
                        '--journal-dir',
                        str(root / 'journal'),
                        '--owner',
                        'dest',
                        '--lock',
                        str(root / 'lock'),
                        '--forgejo-config-repo',
                        'dest/control',
                        '--forgejo-config-id',
                        '99',
                    ]
                )

            self.assertEqual(status, 0, stdout.getvalue())
            self.assertEqual(api.replace_puts, 1)
            self.assertEqual(
                events,
                [
                    ('worker', 'https://github.com/Alice/gone', 2, 100),
                    ('worker', 'https://github.com/Alice/alpha', 2, 100),
                ],
            )
            out = stdout.getvalue()
            self.assertIn('auto_discovery 1/1 (Alice): OK discovered=1 inducted=1 excluded=0 tracked=0 missing_upstream=1', out)
            self.assertIn('orphaned child repository Alice/gone: destination mirror-Alice-gone untouched', out)

    def test_scheduler_refuses_groups_and_auto_discoveries_collision(self):
        group_name = 'gitlab-' + hashlib.sha256(b'https://gitlab.com\0' + b'7\0' + b'42').hexdigest()[:24]
        doc = _base_doc(
            [{'url': 'https://github.com/Alice', 'id': 100, 'destination_name_prefix': 'shared'}],
            groups=[{'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'team/sub', 'id': 7, 'repos': 'all'}],
        )
        raw = _encode(doc)
        api = FakeForgejoAPI(
            owner='dest',
            control_repo='dest/control',
            control_files={
                'config.json': raw,
                'migrations.json': b'{"version": 1, "migrations": {}, "signatures": {}}',
                'failures.json': b'{"version": 1, "journals": {}}\n',
            },
        )
        gitlab_src = FakeSourceAPI(
            routes={'https://gitlab.com/api/v4/groups/team%2Fsub': {'id': 7, 'visibility': 'public', 'full_path': 'team/sub'}},
            pages_routes={
                'https://gitlab.com/api/v4/groups/team%2Fsub/projects?include_subgroups=false&with_shared=false': [
                    {
                        'id': 42,
                        'path': 'p',
                        'visibility': 'public',
                        'path_with_namespace': 'team/sub/p',
                        'http_url_to_repo': 'https://gitlab.com/team/sub/p.git',
                    }
                ]
            },
        )
        gh_src = FakeSourceAPI(
            routes={'https://api.github.com/users/Alice': {'id': 100, 'login': 'Alice', 'type': 'User'}},
            pages_routes={
                'https://api.github.com/users/Alice/repos?type=owner': [
                    {
                        'id': 11,
                        'name': 'p',
                        'full_name': 'Alice/p',
                        'clone_url': 'https://github.com/Alice/p.git',
                        'private': False,
                    }
                ]
            },
        )
        # First verify gitlab group onboarding + auto_discoveries distinct destinations succeed,
        # and colliding override against existing group destination name fails closed before any worker runs.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            colliding_doc = _base_doc(
                [{'url': 'https://github.com/Alice', 'id': 100, 'destination_name_prefix': 'shared'}],
                [
                    {
                        'source': {'type': 'gitlab', 'url': 'https://gitlab.com/team/sub/p', 'id': 42},
                        'mirrors': {'forgejo-home': {'name': group_name, 'id': 9001}},
                    },
                    {
                        'source': {'url': 'https://github.com/Other/p', 'id': 55},
                        'mirrors': {'forgejo-home': {'name': 'shared-p', 'id': 9002}},
                    },
                ],
                groups=[{'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'team/sub', 'id': 7, 'repos': 'all'}],
            )
            colliding_raw = _encode(colliding_doc)
            api.control_files['config.json'] = colliding_raw
            (root / 'manifest').write_bytes(colliding_raw)
            (root / 'secret').write_text('x' * 32)
            (root / 'secret').chmod(0o600)
            (root / 'token').write_text('forgejo-token')
            (root / 'token').chmod(0o600)
            (root / 'journal').mkdir(mode=0o700)

            with (
                patch('repo_mirror.forgejo_control_config.ForgejoControlAPI', return_value=api),
                patch('repo_mirror.gitlab_onboarding_forgejo.AnonymousGroupAPI', return_value=gitlab_src),
                patch('repo_mirror.gitlab_source_adapter.GitLabSourceAdapter', return_value=Mock(project=lambda: {})),
                patch('repo_mirror.forgejo_schedule._build_auto_discovery_source_client', return_value=lambda _: gh_src),
                patch('repo_mirror.forgejo_schedule.subprocess.Popen') as worker,
                redirect_stdout(io.StringIO()),
            ):
                status = forgejo_schedule.main(
                    [
                        'run',
                        '--manifest',
                        str(root / 'manifest'),
                        '--token-file',
                        str(root / 'token'),
                        '--timestamp-secret-file',
                        str(root / 'secret'),
                        '--journal-dir',
                        str(root / 'journal'),
                        '--owner',
                        'dest',
                        '--lock',
                        str(root / 'lock'),
                        '--forgejo-config-repo',
                        'dest/control',
                        '--forgejo-config-id',
                        '99',
                        '--asset-max-bytes',
                        '100',
                    ]
                )
            self.assertEqual(status, 1)
            worker.assert_not_called()
            self.assertEqual(api.replace_puts, 0)


if __name__ == '__main__':
    unittest.main()
