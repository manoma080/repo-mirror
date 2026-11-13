"""End-to-end wipe safety through Forgejo's shared snapshot callbacks.

The Forgejo HTTP API is faked; all Git objects, refs and pushes are real local
repositories. No network or live Forgejo state is touched.
"""
import re
import subprocess
from unittest.mock import patch

import tempfile
import unittest
from pathlib import Path

from repo_mirror.forgejo_git import APIError, ForgejoDestination, sync_refs


def git(*args):
    return subprocess.check_output(['git', *map(str, args)], stderr=subprocess.PIPE).decode().strip()


class Scenario:
    def __init__(self, root, *, files=1, retained=1, failure=None):
        self.root, self.failure = root, failure
        self.source, self.dest, self.work = (root / x for x in ('source.git', 'dest.git', 'work'))
        for path in (self.source, self.dest):
            git('init', '-q', '--bare', '--initial-branch=main', path)
        git('init', '-q', '--initial-branch=main', self.work)
        git('-C', self.work, 'config', 'user.name', 'Test')
        git('-C', self.work, 'config', 'user.email', 'test@example.com')
        for i in range(4):
            (self.work / f'file{i}').write_text(str(i))
        commits = []
        for i in range(4):
            git('-C', self.work, 'add', '-A')
            git('-C', self.work, 'commit', '-qm', f'old{i}', '--allow-empty')
            commits.append(git('-C', self.work, 'rev-parse', 'HEAD'))
        git('-C', self.work, 'tag', '-a', 'old-tag', '-m', 'old', commits[1])
        git('-C', self.work, 'push', '-q', self.dest, 'main', 'main:refs/heads/extra', 'refs/tags/old-tag')
        git('-C', self.work, 'checkout', '-q', '--orphan', 'replacement')
        git('-C', self.work, 'rm', '-q', '-rf', '.')
        for i in range(files):
            (self.work / f'new{i}').write_text('new')
        git('-C', self.work, 'add', '-A')
        git('-C', self.work, 'commit', '-qm', 'replacement', '--allow-empty')
        git('-C', self.work, 'branch', '-M', 'main')
        git('-C', self.work, 'push', '-q', self.source, 'main')
        if retained:
            # A source tag preserves exactly the first N old commits.
            git('-C', self.work, 'tag', '-a', 'rescue', '-m', 'rescue', commits[retained - 1])
            git('-C', self.work, 'push', '-q', self.source, 'refs/tags/rescue')
        self.before = git('ls-remote', '--refs', self.dest)
        self.before_head = git('--git-dir', self.dest, 'symbolic-ref', 'HEAD')
        self.archives, self.calls, self.destination_writes = {}, [], []
        self.metadata = {'project': {'id': 1, 'full_name': 'mirror/project',
            'private': True, 'has_actions': False,
            'description': 'repo-mirror-source: github.com/source/project [repo-mirror:initialized:v1]'}}

    base_url = 'http://127.0.0.1:3300'

    def token(self):
        return 'local-test-token'

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == 'POST':
            assert payload is not None
            if self.failure == 'create':
                raise APIError(500)
            name = payload['name']
            archive = self.root / (name + '.git')
            # Deliberately start with a different HEAD to exercise default setting.
            git('init', '-q', '--bare', '--initial-branch=unborn', archive)
            self.archives[name] = archive
            self.metadata[name] = {'id': 2, 'full_name': 'mirror/' + name,
                'private': payload['private'] and self.failure != 'privacy',
                'has_actions': self.failure == 'actions', 'description': payload['description']}
            return dict(self.metadata[name], default_branch='unborn')
        name = path.rsplit('/', 1)[-1]
        archive = '-wipe-' in name
        if archive and name not in self.archives:
            raise APIError(404)
        repo = self.archives[name] if archive else self.dest
        if method == 'PATCH':
            assert payload is not None
            if not archive:
                self.destination_writes.append(('PATCH', payload))
            if 'description' in payload:
                self.metadata[name]['description'] = payload['description']
            if 'has_actions' in payload and not (archive and self.failure == 'actions'):
                self.metadata[name]['has_actions'] = payload['has_actions']
            if 'default_branch' in payload and not (archive and self.failure == 'default'):
                git('--git-dir', repo, 'symbolic-ref', 'HEAD', 'refs/heads/' + payload['default_branch'])
        return dict(self.metadata[name],
                    default_branch=git('--git-dir', repo, 'symbolic-ref', '--short', 'HEAD'))

    def sync(self):
        real_run = subprocess.run
        def run(args, **kwargs):
            original = list(args)
            destination_url = self.base_url + '/mirror/project.git'
            if 'push' in args and destination_url in args:
                self.destination_writes.append(('push', original))
            mapping = {'https://github.com/source/project.git': str(self.source), destination_url: str(self.dest)}
            mapping.update({self.base_url + '/mirror/' + name + '.git': str(path) for name, path in self.archives.items()})
            args = [mapping.get(x, x) for x in args]
            args = ['protocol.file.allow=always' if x == 'protocol.file.allow=never' else x for x in args]
            result = real_run(args, **kwargs)
            if ('ls-remote' in original and '--refs' in original and self.archives
                    and any(self.base_url + '/mirror/' + n + '.git' in original for n in self.archives)):
                if self.failure == 'archive-id':
                    self.metadata[next(iter(self.archives))]['id'] = 99
                elif self.failure == 'destination-id':
                    self.metadata['project']['id'] = 99
                elif self.failure == 'archive-provenance':
                    self.metadata[next(iter(self.archives))]['description'] = 'wrong source'
                elif self.failure == 'destination-state':
                    self.metadata['project']['description'] = 'repo-mirror-source: github.com/source/project [repo-mirror:initializing:v1]'
            if (self.failure == 'refs' and 'ls-remote' in original and self.archives
                    and any(self.base_url + '/mirror/' + n + '.git' in original for n in self.archives)
                    and result.stdout and 'refs/heads/' in result.stdout.decode()):
                # Corrupt readback, not the actual local repository.
                result.stdout = b''
            return result
        with patch('repo_mirror.forgejo_git.subprocess.run', side_effect=run):
            return sync_refs(ForgejoDestination(self, 'mirror'), 'project', 'source/project', source_token='test')


class ForgejoWipeParityTests(unittest.TestCase):
    def test_wipe_thresholds_use_both_file_count_and_all_source_refs(self):
        for files, retained, archives in ((1, 1, 1), (2, 1, 0), (1, 2, 0), (0, 0, 1)):
            with self.subTest(files=files, retained=retained), tempfile.TemporaryDirectory() as tmp:
                scenario = Scenario(Path(tmp), files=files, retained=retained)
                scenario.sync()
                self.assertEqual(len(scenario.archives), archives)
                self.assertEqual(git('ls-remote', '--refs', scenario.dest), git('ls-remote', '--refs', scenario.source))

    def test_archive_timestamp_privacy_actions_refs_and_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = Scenario(Path(tmp))
            scenario.sync()
            self.assertEqual(len(scenario.archives), 1)
            name, archive = next(iter(scenario.archives.items()))
            self.assertRegex(name, r'^project-wipe-\d{8}T\d{6}Z$')
            posts = [payload for method, _, payload in scenario.calls if method == 'POST']
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0]['name'], name)
            self.assertIs(posts[0]['private'], True)
            self.assertIs(posts[0]['auto_init'], False)
            self.assertIs(posts[0]['has_actions'], False)
            self.assertEqual(posts[0]['description'],
                             'repo-mirror-source: github.com/source/project [repo-mirror:initializing:v1]')
            self.assertEqual(scenario.metadata['project']['id'], 1)
            self.assertEqual(scenario.metadata[name]['id'], 2)
            self.assertEqual(scenario.metadata['project']['description'],
                             'repo-mirror-source: github.com/source/project [repo-mirror:initialized:v1]')
            self.assertEqual(scenario.metadata[name]['description'], posts[0]['description'])
            self.assertEqual(git('ls-remote', '--refs', archive), scenario.before)
            self.assertEqual(git('--git-dir', archive, 'symbolic-ref', 'HEAD'), scenario.before_head)
            self.assertTrue(any(m == 'GET' and p.endswith('/' + name) for m, p, _ in scenario.calls))

    def test_archive_failure_aborts_before_destination_update(self):
        for failure in ('create', 'privacy', 'actions', 'refs', 'default',
                        'archive-id', 'destination-id', 'archive-provenance', 'destination-state'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                scenario = Scenario(Path(tmp), failure=failure)
                with self.assertRaises((APIError, RuntimeError)):
                    scenario.sync()
                self.assertEqual(len(scenario.archives), 0 if failure == 'create' else 1)
                self.assertTrue(any(m == 'POST' for m, _, _ in scenario.calls))
                if failure == 'default':
                    self.assertTrue(any(m == 'PATCH' and '-wipe-' in p and 'default_branch' in data
                                        for m, p, data in scenario.calls))
                if failure == 'refs':
                    archive = next(iter(scenario.archives.values()))
                    self.assertEqual(git('ls-remote', '--refs', archive), scenario.before)
                self.assertEqual(scenario.destination_writes, [])
                self.assertEqual(git('ls-remote', '--refs', scenario.dest), scenario.before)
                self.assertEqual(git('--git-dir', scenario.dest, 'symbolic-ref', 'HEAD'), scenario.before_head)
