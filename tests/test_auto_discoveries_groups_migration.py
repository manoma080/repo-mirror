"""Offline tests for migrating ``groups`` into ``auto_discoveries``."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from repo_mirror import forgejo_dispatch, forgejo_schedule
from repo_mirror.auto_discoveries_onboarding import onboard_auto_discoveries_forgejo
from repo_mirror.config_named import normalize_config
from repo_mirror.forgejo_control_config import ForgejoFile
from repo_mirror.forgejo_sync import parse_manifest
from repo_mirror.gitlab_onboarding_forgejo import (
    DUPLICATE_GROUP_MESSAGE,
    PREFIX_WITH_GENERATED_MESSAGE,
    gitlab_generated_name,
    translate_groups,
)
from tests.test_auto_discoveries_forgejo import FakeForgejoAPI, FakeSourceAPI


def _encode(doc):
    return (json.dumps(doc, indent=2, ensure_ascii=False) + '\n').encode('utf-8')


def _group(owner='team/sub', group_id=7, base_url='https://gitlab.com'):
    return {
        'type': 'gitlab',
        'base_url': base_url,
        'owner': owner,
        'id': group_id,
        'repos': 'all',
    }


class GroupTranslationAndWarningsTests(unittest.TestCase):
    def test_translate_groups_is_in_memory_only_and_emits_one_warning_per_group(self):
        doc = {
            'destinations': {'forgejo-home': {'account_type': 'personal'}},
            'repositories': [],
            'groups': [_group('team/sub', 7), _group('other/org', 8)],
        }
        snapshot = copy.deepcopy(doc)
        stream = io.StringIO()
        rules = translate_groups(doc, stream=stream)
        self.assertEqual(doc, snapshot)
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0]['url'], 'https://gitlab.com/team/sub')
        self.assertEqual(rules[0]['type'], 'gitlab')
        self.assertEqual(rules[0]['id'], 7)
        self.assertEqual(rules[0]['visibility'], 'private')
        self.assertTrue(rules[0]['_from_groups'])

        lines = [line for line in stream.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(line.startswith('DEPRECATION: groups entry') for line in lines))
        self.assertNotIn('https://', stream.getvalue())

        empty_stream = io.StringIO()
        self.assertEqual(translate_groups({'destinations': {'forgejo-home': {}}, 'repositories': []}, stream=empty_stream), [])
        self.assertEqual(empty_stream.getvalue(), '')

    def test_steady_state_config_bytes_unchanged_and_warnings_accurate(self):
        gen_name = gitlab_generated_name('https://gitlab.com', 7, 42)
        doc = {
            'destinations': {'forgejo-home': {'account_type': 'personal'}},
            'repositories': [
                {
                    'source': {'type': 'gitlab', 'url': 'https://gitlab.com/team/sub/proj', 'id': 42},
                    'mirrors': {'forgejo-home': {'name': gen_name, 'id': 7001}},
                }
            ],
            'groups': [_group('team/sub', 7)],
        }
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
            routes={'https://gitlab.com/api/v4/groups/team%2Fsub': {'id': 7, 'visibility': 'public', 'full_path': 'team/sub'}},
            pages_routes={
                'https://gitlab.com/api/v4/groups/team%2Fsub/projects?include_subgroups=false&with_shared=false': [
                    {
                        'id': 42,
                        'path': 'proj',
                        'visibility': 'public',
                        'path_with_namespace': 'team/sub/proj',
                        'http_url_to_repo': 'https://gitlab.com/team/sub/proj.git',
                    }
                ]
            },
        )
        stream = io.StringIO()
        res = onboard_auto_discoveries_forgejo(raw, cas, lambda _: src, api, owner='dest', stream=stream)
        self.assertEqual(res.config_bytes, raw)
        self.assertEqual(api.replace_puts, 0)
        warnings = [l for l in stream.getvalue().splitlines() if l.startswith('DEPRECATION:')]
        self.assertEqual(len(warnings), 1)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'manifest').write_bytes(raw)
            (root / 'secret').write_text('x' * 32)
            (root / 'secret').chmod(0o600)
            (root / 'token').write_text('forgejo-token')
            (root / 'token').chmod(0o600)
            (root / 'journal').mkdir(mode=0o700)
            launched = []

            def spawn(command, **kwargs):
                launched.append(command[-1])
                return Mock(poll=lambda: 0, pid=999999999, wait=lambda: 0)

            sched_out = io.StringIO()
            with (
                patch('repo_mirror.forgejo_control_config.ForgejoControlAPI', return_value=api),
                patch('repo_mirror.forgejo_schedule._build_auto_discovery_source_client', return_value=lambda _: src),
                patch('repo_mirror.forgejo_schedule.subprocess.Popen', side_effect=spawn),
                redirect_stdout(sched_out),
            ):
                status = forgejo_schedule.main(
                    [
                        'run',
                        '--manifest', str(root / 'manifest'),
                        '--token-file', str(root / 'token'),
                        '--timestamp-secret-file', str(root / 'secret'),
                        '--journal-dir', str(root / 'journal'),
                        '--owner', 'dest',
                        '--lock', str(root / 'lock'),
                        '--forgejo-config-repo', 'dest/control',
                        '--forgejo-config-id', '99',
                        '--asset-max-bytes', '100',
                    ]
                )
            self.assertEqual(status, 0, sched_out.getvalue())
            self.assertEqual(launched, ['https://gitlab.com/team/sub/proj'])
            self.assertEqual((root / 'manifest').read_bytes(), raw)
            self.assertEqual(api.replace_puts, 0)
            sched_warnings = [l for l in sched_out.getvalue().splitlines() if l.startswith('DEPRECATION:')]
            self.assertEqual(len(sched_warnings), 1)

    def test_no_deprecation_warning_when_groups_is_absent(self):
        doc = {
            'destinations': {'forgejo-home': {'account_type': 'personal'}},
            'auto_discoveries': [{'url': 'https://gitlab.com/team/sub', 'type': 'gitlab', 'id': 7}],
            'repositories': [
                {
                    'source': {'url': 'https://gitlab.com/team/sub/proj', 'id': 42},
                    'mirrors': {'forgejo-home': {'name': 'mirror-team-sub-proj', 'id': 7001}},
                    'discovered_by': 'https://gitlab.com/team/sub',
                }
            ],
        }
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
            routes={'https://gitlab.com/api/v4/groups/team%2Fsub': {'id': 7, 'visibility': 'public', 'full_path': 'team/sub'}},
            pages_routes={
                'https://gitlab.com/api/v4/groups/team%2Fsub/projects?include_subgroups=false&with_shared=false': [
                    {
                        'id': 42,
                        'path': 'proj',
                        'visibility': 'public',
                        'path_with_namespace': 'team/sub/proj',
                        'http_url_to_repo': 'https://gitlab.com/team/sub/proj.git',
                    }
                ]
            },
        )
        stream = io.StringIO()
        onboard_auto_discoveries_forgejo(raw, cas, lambda _: src, api, owner='dest', stream=stream)
        self.assertNotIn('DEPRECATION:', stream.getvalue())


class DuplicateGroupDeclarationTests(unittest.TestCase):
    def test_duplicate_declaration_in_groups_and_auto_discoveries_rejected(self):
        cases = [
            # Same origin + id (with case/port difference)
            {
                'destinations': {'forgejo-home': {'account_type': 'personal'}},
                'repositories': [],
                'groups': [_group('team/sub', 7, 'https://gitlab.com:443')],
                'auto_discoveries': [{'url': 'https://GitLab.com/other/group', 'type': 'gitlab', 'id': 7}],
            },
            # Same origin + owner (case-insensitive)
            {
                'destinations': {'forgejo-home': {'account_type': 'personal'}},
                'repositories': [],
                'groups': [_group('team/sub', 7)],
                'auto_discoveries': [{'url': 'https://gitlab.com/Team/Sub', 'type': 'gitlab', 'id': 99}],
            },
        ]
        for doc in cases:
            with self.subTest(doc=doc):
                with self.assertRaises(ValueError) as ctx:
                    translate_groups(doc)
                self.assertEqual(str(ctx.exception), DUPLICATE_GROUP_MESSAGE)

                with self.assertRaises(ValueError) as ctx:
                    parse_manifest(doc)
                self.assertEqual(str(ctx.exception), DUPLICATE_GROUP_MESSAGE)

                with self.assertRaises(ValueError) as ctx:
                    forgejo_dispatch.plan_dispatch(doc, {}, owner='dest', port=3300)
                self.assertEqual(str(ctx.exception), DUPLICATE_GROUP_MESSAGE)

                raw = _encode(doc)
                api = FakeForgejoAPI(owner='dest', control_repo='dest/control', control_files={'config.json': raw})
                cas = ForgejoFile(api, 'dest', 'control', 'config.json', expected_id=99)
                with self.assertRaises(ValueError) as ctx:
                    onboard_auto_discoveries_forgejo(raw, cas, lambda _: FakeSourceAPI(), api, owner='dest', stream=io.StringIO())
                self.assertEqual(str(ctx.exception), DUPLICATE_GROUP_MESSAGE)

                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / 'manifest').write_bytes(raw)
                    status = forgejo_schedule.main(
                        [
                            'run',
                            '--manifest', str(root / 'manifest'),
                            '--token-file', 'unused',
                            '--timestamp-secret-file', 'unused',
                            '--journal-dir', directory,
                            '--owner', 'dest',
                            '--lock', str(root / 'lock'),
                            '--config-repo', 'a/b',
                            '--asset-max-bytes', '100',
                        ]
                    )
                    self.assertEqual(status, 2)
                    self.assertFalse((root / 'lock').exists())


class GeneratedNameAndPrefixMigrationTests(unittest.TestCase):
    def test_existing_child_keeps_name_and_new_induction_under_translated_group_uses_gitlab_24hex(self):
        existing_name = gitlab_generated_name('https://gitlab.com', 7, 42)
        new_name = gitlab_generated_name('https://gitlab.com', 7, 43)
        doc = {
            'destinations': {'forgejo-home': {'account_type': 'personal'}},
            'groups': [_group('team/sub', 7)],
            'auto_discoveries': [{'url': 'https://github.com/Alice', 'id': 100, 'destination_name_prefix': 'gh'}],
            'repositories': [
                {
                    'source': {'type': 'gitlab', 'url': 'https://gitlab.com/team/sub/existing', 'id': 42},
                    'mirrors': {'forgejo-home': {'name': existing_name, 'id': 7001}},
                }
            ],
        }
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
        gl_src = FakeSourceAPI(
            routes={'https://gitlab.com/api/v4/groups/team%2Fsub': {'id': 7, 'visibility': 'public', 'full_path': 'team/sub'}},
            pages_routes={
                'https://gitlab.com/api/v4/groups/team%2Fsub/projects?include_subgroups=false&with_shared=false': [
                    {
                        'id': 42,
                        'path': 'existing',
                        'visibility': 'public',
                        'path_with_namespace': 'team/sub/existing',
                        'http_url_to_repo': 'https://gitlab.com/team/sub/existing.git',
                    },
                    {
                        'id': 43,
                        'path': 'added',
                        'visibility': 'public',
                        'path_with_namespace': 'team/sub/added',
                        'http_url_to_repo': 'https://gitlab.com/team/sub/added.git',
                    },
                ]
            },
        )
        gh_src = FakeSourceAPI(
            routes={'https://api.github.com/users/Alice': {'id': 100, 'login': 'Alice', 'type': 'User'}},
            pages_routes={
                'https://api.github.com/users/Alice/repos?type=owner': [
                    {
                        'id': 11,
                        'name': 'one',
                        'full_name': 'Alice/one',
                        'clone_url': 'https://github.com/Alice/one.git',
                        'private': False,
                    }
                ]
            },
        )
        res = onboard_auto_discoveries_forgejo(
            raw,
            cas,
            lambda rule: gl_src if rule['type'] == 'gitlab' else gh_src,
            api,
            owner='dest',
            stream=io.StringIO(),
        )
        published = json.loads(res.config_bytes)
        # Operator groups[] is preserved in authoritative config (not rewritten to auto_discoveries).
        self.assertIn('groups', published)
        self.assertEqual(len(published['auto_discoveries']), 1)
        by_url = {r['source']['url']: r for r in published['repositories']}
        self.assertEqual(by_url['https://gitlab.com/team/sub/existing']['mirrors']['forgejo-home']['name'], existing_name)
        self.assertEqual(by_url['https://gitlab.com/team/sub/added']['mirrors']['forgejo-home']['name'], new_name)
        self.assertNotIn('discovered_by', by_url['https://gitlab.com/team/sub/added'])
        self.assertEqual(by_url['https://github.com/Alice/one']['mirrors']['forgejo-home']['name'], 'gh-one')
        self.assertEqual(by_url['https://github.com/Alice/one']['discovered_by'], 'https://github.com/Alice')

    def test_prefix_rejected_on_rule_with_existing_gitlab_24hex_child(self):
        gen_name = gitlab_generated_name('https://gitlab.com', 7, 42)
        docs = [
            # Migrated child with discovered_by
            {
                'destinations': {'forgejo-home': {'account_type': 'personal'}},
                'auto_discoveries': [
                    {'url': 'https://gitlab.com/team/sub', 'type': 'gitlab', 'id': 7, 'destination_name_prefix': 'custom'}
                ],
                'repositories': [
                    {
                        'source': {'type': 'gitlab', 'url': 'https://gitlab.com/team/sub/proj', 'id': 42},
                        'mirrors': {'forgejo-home': {'name': gen_name, 'id': 7001}},
                        'discovered_by': 'https://gitlab.com/team/sub',
                    }
                ],
            },
            # Legacy child without discovered_by under the same group
            {
                'destinations': {'forgejo-home': {'account_type': 'personal'}},
                'auto_discoveries': [
                    {'url': 'https://gitlab.com/team/sub', 'type': 'gitlab', 'id': 7, 'destination_name_prefix': 'custom'}
                ],
                'repositories': [
                    {
                        'source': {'type': 'gitlab', 'url': 'https://gitlab.com/team/sub/proj', 'id': 42},
                        'mirrors': {'forgejo-home': {'name': gen_name, 'id': 7001}},
                    }
                ],
            },
        ]
        for doc in docs:
            with self.subTest(doc=doc):
                with self.assertRaises(ValueError) as ctx:
                    normalize_config(doc)
                self.assertEqual(str(ctx.exception), PREFIX_WITH_GENERATED_MESSAGE)

                with self.assertRaises(ValueError) as ctx:
                    parse_manifest(doc)
                self.assertEqual(str(ctx.exception), PREFIX_WITH_GENERATED_MESSAGE)

                raw = _encode(doc)
                api = FakeForgejoAPI(owner='dest', control_repo='dest/control', control_files={'config.json': raw})
                cas = ForgejoFile(api, 'dest', 'control', 'config.json', expected_id=99)
                with self.assertRaises(ValueError) as ctx:
                    onboard_auto_discoveries_forgejo(raw, cas, lambda _: FakeSourceAPI(), api, owner='dest', stream=io.StringIO())
                self.assertEqual(str(ctx.exception), PREFIX_WITH_GENERATED_MESSAGE)

    def test_reserved_gitlab_24hex_prefix_or_override_refused(self):
        gen_name = 'gitlab-' + ('ab' * 12)
        bad_docs = [
            {
                'destinations': {'forgejo-home': {'account_type': 'personal'}},
                'auto_discoveries': [{'url': 'https://github.com/Alice', 'destination_name_prefix': gen_name}],
                'repositories': [],
            },
            {
                'destinations': {'forgejo-home': {'account_type': 'personal'}},
                'auto_discoveries': [
                    {
                        'url': 'https://github.com/Alice',
                        'destination_name_prefix': 'gitlab',
                        'overrides': {'ab' * 12: {'visibility': 'private'}},
                    }
                ],
                'repositories': [],
            },
            {
                'destinations': {'forgejo-home': {'account_type': 'personal'}},
                'auto_discoveries': [
                    {
                        'url': 'https://github.com/Alice',
                        'overrides': {'proj': {'name': gen_name}},
                    }
                ],
                'repositories': [],
            },
        ]
        for bad in bad_docs:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    normalize_config(bad)
                with self.assertRaises(ValueError):
                    parse_manifest(bad)

        # Prefix 'gitlab' + discovered repo '<24hex>' producing 'gitlab-<24hex>' is also refused at onboarding.
        doc = {
            'destinations': {'forgejo-home': {'account_type': 'personal'}},
            'auto_discoveries': [{'url': 'https://github.com/Alice', 'id': 100, 'destination_name_prefix': 'gitlab'}],
            'repositories': [],
        }
        raw = _encode(doc)
        api = FakeForgejoAPI(owner='dest', control_repo='dest/control', control_files={'config.json': raw})
        cas = ForgejoFile(api, 'dest', 'control', 'config.json', expected_id=99)
        src = FakeSourceAPI(
            routes={'https://api.github.com/users/Alice': {'id': 100, 'login': 'Alice', 'type': 'User'}},
            pages_routes={
                'https://api.github.com/users/Alice/repos?type=owner': [
                    {
                        'id': 11,
                        'name': 'ab' * 12,
                        'full_name': 'Alice/' + ('ab' * 12),
                        'clone_url': 'https://github.com/Alice/' + ('ab' * 12) + '.git',
                        'private': False,
                    }
                ]
            },
        )
        with self.assertRaisesRegex(ValueError, 'invalid destination repository name'):
            onboard_auto_discoveries_forgejo(raw, cas, lambda _: src, api, owner='dest', stream=io.StringIO())
        self.assertEqual(api.replace_puts, 0)

    def test_translated_group_never_falls_back_to_user_namespace(self):
        doc = {
            'destinations': {'forgejo-home': {'account_type': 'personal'}},
            'groups': [_group('alice', 7)],
            'repositories': [],
        }
        raw = _encode(doc)
        api = FakeForgejoAPI(owner='dest', control_repo='dest/control', control_files={'config.json': raw})
        cas = ForgejoFile(api, 'dest', 'control', 'config.json', expected_id=99)
        # /groups/alice returns 404; /users?username=alice must never be used for a groups[] rule.
        src = FakeSourceAPI(
            routes={'https://gitlab.com/api/v4/users?username=alice': [{'id': 7, 'username': 'alice'}]},
            pages_routes={},
        )
        with self.assertRaises(HTTPError) as ctx:
            onboard_auto_discoveries_forgejo(raw, cas, lambda _: src, api, owner='dest', stream=io.StringIO())
        ctx.exception.close()
        self.assertEqual(src.requests, [('GET', 'https://gitlab.com/api/v4/groups/alice')])
