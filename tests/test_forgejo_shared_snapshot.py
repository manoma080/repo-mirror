import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import unittest
import tempfile

from repo_mirror.forgejo_git import APIError, ForgejoDestination, sync_refs


def exercise_rewrites_and_deletes_using_shared_snapshot(tmp_path, wipe, archive_ok=False, moved_tag=False, race=None):
    def git(*args):
        return subprocess.check_output(['git', *map(str, args)], stderr=subprocess.DEVNULL).decode().strip()
    source, dest = tmp_path / 'source.git', tmp_path / 'dest.git'
    for path in (source, dest):
        git('init', '--bare', '--initial-branch=main', path)
    work = tmp_path / 'work'
    git('init', '--initial-branch=main', work)
    git('-C', work, 'config', 'user.email', 'test@example.com')
    git('-C', work, 'config', 'user.name', 'Test')
    (work / 'file').write_text('one')
    if wipe:
        for n in range(3):
            (work / f'extra{n}').write_text('old')
    git('-C', work, 'add', '.')
    git('-C', work, 'commit', '-m', 'one')
    git('-C', work, 'push', dest, 'main', 'main:refs/heads/extra')
    git('-C', work, 'tag', 'v1')
    git('-C', work, 'push', dest, 'v1')
    git('-C', work, 'checkout', '--orphan', 'replacement')
    if wipe:
        for n in range(3):
            (work / f'extra{n}').unlink()
    (work / 'file').write_text('two')
    git('-C', work, 'commit', '-am', 'two')
    git('-C', work, 'branch', '-M', 'main')
    git('-C', work, 'push', source, 'main')
    if moved_tag:
        git('-C', work, 'tag', '-f', 'v1')
        git('-C', work, 'push', source, 'v1')

    archives = {}
    state = {'gets': 0, 'raced': False, 'writes': []}
    class API:
        base_url = 'http://127.0.0.1:3300'
        def token(self): return 'local'
        def request(self, method, path, payload=None):
            if method == 'POST':
                state['writes'].append('POST')
                archive_name = payload['name']
                archive = tmp_path / (archive_name + '.git')
                git('init', '--bare', '--initial-branch=main', archive)
                archives[archive_name] = archive
                return dict(payload, id=2, full_name='mirror/' + archive_name)
            if '-wipe-' in path:
                if not archive_ok:
                    raise APIError(500)
                archive_name = path.rsplit('/', 1)[-1]
                if archive_name not in archives:
                    raise APIError(404)
                archive = archives[archive_name]
                if method == 'PATCH':
                    git('--git-dir', archive, 'symbolic-ref', 'HEAD', 'refs/heads/' + payload['default_branch'])
                return {'id': 2, 'full_name': 'mirror/' + archive_name, 'private': True, 'has_actions': False,
                        'description': 'repo-mirror-source: github.com/source/project [repo-mirror:initializing:v1]',
                        'default_branch': git('--git-dir', archive, 'symbolic-ref', '--short', 'HEAD')}
            if method == 'PATCH':
                state['writes'].append(method)
                git('--git-dir', dest, 'symbolic-ref', 'HEAD', 'refs/heads/' + payload['default_branch'])
            if race == 'disappeared' and state['raced']:
                raise APIError(404)
            state['gets'] += 1
            return {'id': 2 if race == 'replacement' and state['raced'] else 1,
                    'full_name': 'mirror/project', 'private': True, 'has_actions': False,
                    'description': 'repo-mirror-source: github.com/source/project' + (' [repo-mirror:initialized:v1]' if race == 'marker' and state['raced'] else ''),
                    'default_branch': git('--git-dir', dest, 'symbolic-ref', '--short', 'HEAD')}
    real_run = subprocess.run
    def run(args, **kwargs):
        if race and 'fetch' in args and str(dest) not in args and any('github.com/source/project.git' in x for x in args):
            state['raced'] = True
            if race == 'source':
                git('--git-dir', source, 'update-ref', 'refs/heads/new', git('--git-dir', source, 'rev-parse', 'main'))
        if 'push' in args:
            state['writes'].append('push')
        for archive_name, archive in archives.items():
            args = [str(archive) if x == 'http://127.0.0.1:3300/mirror/' + archive_name + '.git' else x for x in args]
        args = [str(source) if x == 'https://github.com/source/project.git' else
                str(dest) if x == 'http://127.0.0.1:3300/mirror/project.git' else x for x in args]
        args = ['protocol.file.allow=always' if x == 'protocol.file.allow=never' else x for x in args]
        return real_run(args, **kwargs)
    before = git('--git-dir', dest, 'show-ref')
    with patch('repo_mirror.forgejo_git.subprocess.run', side_effect=run):
        if race:
            with unittest.TestCase().assertRaises(RuntimeError):
                sync_refs(ForgejoDestination(API(), 'mirror'), 'project', 'source/project', source_token='github')
            assert state['raced']
            assert not state['writes']
            assert git('--git-dir', dest, 'show-ref') == before
            return
        if wipe and not archive_ok:
            with unittest.TestCase().assertRaises(APIError):
                sync_refs(ForgejoDestination(API(), 'mirror'), 'project', 'source/project', source_token='github')
        else:
            sync_refs(ForgejoDestination(API(), 'mirror'), 'project', 'source/project', source_token='github')
    if wipe and not archive_ok:
        assert git('--git-dir', dest, 'show-ref') == before
        return
    if wipe:
        assert len(archives) == 1
        archive = next(iter(archives.values()))
        assert git('--git-dir', archive, 'show-ref') == before
        assert git('--git-dir', archive, 'symbolic-ref', '--short', 'HEAD') == 'main'
    assert git('--git-dir', dest, 'for-each-ref', '--format=%(refname)') == (
        'refs/heads/main\nrefs/tags/v1' if moved_tag else 'refs/heads/main')
    if moved_tag:
        assert git('--git-dir', dest, 'rev-parse', 'refs/tags/v1') == git('--git-dir', source, 'rev-parse', 'refs/tags/v1')
    assert git('--git-dir', dest, 'rev-parse', 'main') == git('--git-dir', source, 'rev-parse', 'main')


class SharedSnapshotIntegrationTests(unittest.TestCase):
    def test_races_abort_before_any_write(self):
        for race in ('source', 'replacement', 'marker', 'disappeared'):
            with self.subTest(race=race), tempfile.TemporaryDirectory() as tmp:
                exercise_rewrites_and_deletes_using_shared_snapshot(Path(tmp), False, race=race)

    def test_rewrites_and_deletes(self):
        for wipe, archive_ok in ((False, False), (True, False), (True, True)):
            with self.subTest(wipe=wipe, archive_ok=archive_ok), tempfile.TemporaryDirectory() as tmp:
                exercise_rewrites_and_deletes_using_shared_snapshot(Path(tmp), wipe, archive_ok)

    def test_moved_source_tag_is_applied_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            exercise_rewrites_and_deletes_using_shared_snapshot(Path(tmp), False, moved_tag=True)
