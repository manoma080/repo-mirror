"""End-to-end scheduler tests for automatic Forgejo source-relocation migration.

The scheduler runs through its real entry point; only the three HTTP boundaries
(GitHub source metadata, the local Forgejo API and the authoritative GitHub
configuration) are replaced with a file-backed fake world. The worker protocol
is produced by the real ``report_relocation`` helper.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from repo_mirror.forgejo_sync import parse_manifest
from repo_mirror.migration_attestation import forgejo_claim, verify

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''import json, os, sys
from pathlib import Path
from repo_mirror import forgejo_migration, forgejo_schedule
from repo_mirror.forgejo_migration import ForgejoMigration
from repo_mirror.migrations_store import FileMigrations, MigrationsStore

WORLD = Path(os.environ['WORLD'])

def load(): return json.loads(WORLD.read_text())
def save(world): WORLD.write_text(json.dumps(world))

class Source:
    def __call__(self, origin, owner, name):
        metadata = load()['sources'].get(f'{origin}/{owner}/{name}')
        if metadata is None: raise ValueError('source not found')
        return dict(metadata)

class Destination:
    def get(self, name): return load()['repositories'].get(name)
    def rename(self, old, new):
        world = load()
        repo = dict(world['repositories'].pop(old))
        repo['full_name'] = repo['full_name'].rsplit('/', 1)[0] + '/' + new
        world['repositories'][new] = repo
        save(world)

class Config:
    def read(self): return load()['config'].encode('utf-8')
    def replace(self, before, after):
        world = load()
        if world.get('fail_publish'): raise ValueError('network failure')
        if world['config'].encode('utf-8') != before: raise ValueError('configuration changed')
        world['config'] = after.decode('utf-8')
        save(world)

def build_migration(args, secret):
    if not getattr(args, 'config_repo', None):
        return None
    store = MigrationsStore(FileMigrations(os.path.join(args.journal_dir, 'migrations.json')), secret)
    return ForgejoMigration(store, Config(), Destination(), Source(), secret, args.port, args.owner)

forgejo_migration.build_migration = build_migration
sys.exit(forgejo_schedule.main(sys.argv[1:]))
'''
WORKER = r'''import json, os, sys
from pathlib import Path
from repo_mirror.forgejo_sync import report_relocation
args = sys.argv
config = json.loads(Path(args[args.index('--config') + 1]).read_text())
url = args[-1]
entry = next(repo for repo in config['repositories'] if repo['source']['url'] == url)
mirror = entry['mirrors'][next(iter(entry['mirrors']))]
if url == os.environ.get('FAIL_URL'):
    print('FORGEJO_FAILURE: source relocation requires durable migration state and authoritative config publication',
          file=sys.stderr, flush=True)
    sys.exit(1)
if url == os.environ.get('MALFORMED_URL'):
    print('FORGEJO_RELOCATION: {"url": "%s"}' % url, file=sys.stderr, flush=True)
    sys.exit(0)
canonical = json.loads(os.environ.get('MOVES', '{}')).get(url)
if canonical:
    report_relocation({'url': url, 'canonical': canonical, 'source_id': entry['source']['id'],
                       'destination_id': mirror['id']})
'''
OLD = 'https://git.example/old/repo'
NEW = 'https://git.example/new/renamed'
SECRET = 'fixture-signing-secret-0123456789abcdef'
# Forgejo destinations carry the Forgejo marker format, not the GitHub one.
MARKER = 'repo-mirror-source: git.example/old/repo'


def document(mirror, url=OLD, source_id=11):
    return {'destinations': {'forgejo-home': {}},
            'repositories': [{'source': {'url': url, 'id': source_id},
                              'mirrors': {'forgejo-home': dict(mirror)}}]}


def text(value):
    return json.dumps(value, indent=2) + '\n'


def metadata(canonical=NEW, repository_id=11):
    owner, name = canonical.rsplit('/', 2)[-2:]
    return {'id': repository_id, 'full_name': f'{owner}/{name}',
            'html_url': canonical, 'clone_url': canonical + '.git'}


def repository(name, repository_id, description=MARKER):
    return {'id': repository_id, 'full_name': f'dest/{name}', 'private': True, 'description': description}


class AutomaticMigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.world = self.tmp / 'world.json'
        secret = self.tmp / 'secret'
        secret.write_text(SECRET)
        secret.chmod(0o600)
        self.secret = secret
        journals = self.tmp / 'journals'
        journals.mkdir(mode=0o700)
        self.journals = journals
        self.manifest = self.tmp / 'manifest.json'
        self.runner = self.tmp / 'run.py'
        self.runner.write_text(HARNESS)
        self.worker = self.tmp / 'worker.py'
        self.worker.write_text(WORKER)
        wrapper = self.tmp / 'worker.sh'
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{self.worker}" "$@"\n')
        wrapper.chmod(0o700)
        self.wrapper = wrapper

    def write_world(self, config, repositories, **extra):
        sources = {OLD: metadata(), NEW: metadata()}
        self.world.write_text(json.dumps({'config': config, 'repositories': repositories,
                                          'sources': sources, **extra}))

    def read_world(self):
        return json.loads(self.world.read_text())

    def run_scheduler(self, manifest_text, moves=None, **env):
        self.manifest.write_text(manifest_text)
        command = [sys.executable, str(self.runner), 'run', '--manifest', str(self.manifest),
                   '--token-file', 'unused', '--timestamp-secret-file', str(self.secret),
                   '--journal-dir', str(self.journals), '--owner', 'dest', '--port', '3300',
                   '--lock', str(self.tmp / 'lock'), '--sync-python', str(self.wrapper),
                   '--config-repo', 'manoma080/repo-mirror']
        environment = {**os.environ, 'WORLD': str(self.world), 'PYTHONPATH': str(ROOT),
                       'MOVES': json.dumps(moves or {OLD: NEW}), **env}
        return subprocess.run(command, env=environment, capture_output=True, text=True, timeout=120)

    def journal(self):
        pending_path = self.journals / 'migrations.json'
        if not pending_path.exists():
            return {}
        from repo_mirror.migrations_journal import parse
        return parse(pending_path.read_bytes(), SECRET).pending()

    def published(self):
        return json.loads(self.read_world()['config'])

    def assert_preserved_binding(self, entry, destination_name):
        self.assertEqual(entry['url'], NEW)
        self.assertEqual(entry['destination_name'], destination_name)
        self.assertEqual(entry['legacy_url'], OLD)
        self.assertEqual(entry['legacy_destination_name'], 'repo' if destination_name == 'renamed' else 'mirror-copy')
        self.assertEqual((entry['source_id'], entry['destination_id']), (11, 22))
        verify(forgejo_claim(entry, 3300, 'dest'), entry['signature'], SECRET)

    def test_move_preserves_custom_destination_name_and_publishes_configuration(self):
        config = text(document({'name': 'mirror-copy', 'id': 22}))
        self.write_world(config, {'mirror-copy': repository('mirror-copy', 22)})
        result = self.run_scheduler(config)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Migration 1: OK', result.stdout)
        published = self.published()
        self.assertEqual(published['destinations'], document({'name': 'mirror-copy', 'id': 22})['destinations'])
        entry = parse_manifest(published)[0]
        self.assert_preserved_binding(entry, 'mirror-copy')
        self.assertEqual(set(self.read_world()['repositories']), {'mirror-copy'})
        self.assertEqual(self.journal(), {})

    def test_defaulted_destination_name_is_renamed_only_after_the_pins_hold(self):
        config = text(document({'id': 22}))
        self.write_world(config, {'repo': repository('repo', 22)})
        result = self.run_scheduler(config)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        repositories = self.read_world()['repositories']
        self.assertEqual(set(repositories), {'renamed'})
        self.assertEqual(repositories['renamed']['id'], 22)
        self.assertEqual(repositories['renamed']['full_name'], 'dest/renamed')
        self.assert_preserved_binding(parse_manifest(self.published())[0], 'renamed')
        self.assertEqual(self.journal(), {})

    def test_destination_name_collision_stops_before_rename(self):
        config = text(document({'id': 22}))
        self.write_world(config, {'repo': repository('repo', 22),
                                  'renamed': repository('renamed', 99, 'another mirror')})
        for _ in range(2):
            result = self.run_scheduler(config)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn('destination collision', result.stdout)
            self.assertEqual(self.read_world()['config'], config)
            self.assertEqual(set(self.read_world()['repositories']), {'repo', 'renamed'})
            pending = self.journal()
            self.assertEqual(len(pending), 1)
            self.assertEqual(next(iter(pending.values()))['phase'], 'synced')

    def test_configuration_changed_by_another_writer_stops_before_recording(self):
        config = text(document({'name': 'mirror-copy', 'id': 22}))
        edited = json.dumps(document({'name': 'mirror-copy', 'id': 22}))
        self.write_world(edited, {'mirror-copy': repository('mirror-copy', 22)})
        result = self.run_scheduler(config)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('Migration 1: FAILED: configuration changed by another writer', result.stdout)
        self.assertEqual(self.read_world()['repositories'], {'mirror-copy': repository('mirror-copy', 22)})
        self.assertFalse((self.journals / 'migrations.json').exists())

    def test_crash_after_rename_resumes_from_the_recorded_phase(self):
        config = text(document({'id': 22}))
        self.write_world(config, {'repo': repository('repo', 22)}, fail_publish=True)
        result = self.run_scheduler(config)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('Migration 1: FAILED: configuration publication failed', result.stdout)
        world = self.read_world()
        self.assertEqual(set(world['repositories']), {'renamed'})
        self.assertEqual(world['config'], config)
        pending = self.journal()
        self.assertEqual([entry['phase'] for entry in pending.values()], ['renamed'])
        # The rename survived; the next run finishes it without resyncing.
        world['fail_publish'] = False
        self.world.write_text(json.dumps(world))
        result = self.run_scheduler(config)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Repository 1: OK (migration resumed)', result.stdout)
        self.assert_preserved_binding(parse_manifest(self.published())[0], 'renamed')
        self.assertEqual(self.journal(), {})
        # A following run sees the published document and does nothing.
        result = self.run_scheduler(self.read_world()['config'], moves={})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('Migration 1', result.stdout)

    def test_unpinned_or_unverified_relocation_fails_closed(self):
        config = text(document({'name': 'mirror-copy', 'id': 22}))
        self.write_world(config, {'mirror-copy': repository('mirror-copy', 22)})
        result = self.run_scheduler(config, FAIL_URL=OLD)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('source relocation requires durable migration state and authoritative config publication',
                      result.stdout)
        self.assertNotIn('Migration 1', result.stdout)
        self.assertEqual(self.read_world()['config'], config)
        self.assertFalse((self.journals / 'migrations.json').exists())

    def test_malformed_relocation_report_is_rejected(self):
        config = text(document({'name': 'mirror-copy', 'id': 22}))
        self.write_world(config, {'mirror-copy': repository('mirror-copy', 22)})
        result = self.run_scheduler(config, MALFORMED_URL=OLD)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('invalid relocation report', result.stdout)
        self.assertEqual(self.read_world()['config'], config)
        self.assertFalse((self.journals / 'migrations.json').exists())

    def test_second_move_keeps_the_original_recovery_binding(self):
        config = text(document({'name': 'mirror-copy', 'id': 22}))
        self.write_world(config, {'mirror-copy': repository('mirror-copy', 22)})
        self.assertEqual(self.run_scheduler(config).returncode, 0)
        first = parse_manifest(self.published())[0]
        self.assertEqual(first['legacy_url'], OLD)
        # The same repository moves again; the original binding must survive.
        second_url = 'https://git.example/third/renamed-again'
        world = self.read_world()
        world['sources'][NEW] = metadata(second_url)
        world['sources'][second_url] = metadata(second_url)
        self.world.write_text(json.dumps(world))
        result = self.run_scheduler(self.read_world()['config'], moves={NEW: second_url})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        entry = parse_manifest(self.published())[0]
        self.assertEqual(entry['url'], second_url)
        self.assertEqual(entry['destination_name'], 'mirror-copy')
        self.assertEqual(entry['legacy_url'], OLD)
        self.assertEqual(entry['legacy_destination_name'], 'mirror-copy')
        self.assertEqual((entry['source_id'], entry['destination_id']), (11, 22))
        verify(forgejo_claim(entry, 3300, 'dest'), entry['signature'], SECRET)
        self.assertEqual(self.journal(), {})

    def test_destination_name_that_tracks_the_source_follows_later_moves(self):
        config = text(document({'id': 22}))
        self.write_world(config, {'repo': repository('repo', 22)})
        self.assertEqual(self.run_scheduler(config).returncode, 0)
        self.assertEqual(parse_manifest(self.published())[0]['destination_name'], 'renamed')
        second_url = 'https://git.example/third/renamed-again'
        world = self.read_world()
        world['sources'][NEW] = metadata(second_url)
        world['sources'][second_url] = metadata(second_url)
        self.world.write_text(json.dumps(world))
        result = self.run_scheduler(self.read_world()['config'], moves={NEW: second_url})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # A destination name that still equals the derived name keeps tracking
        # the source; only an explicitly custom name is frozen.
        self.assertEqual(set(self.read_world()['repositories']), {'renamed-again'})
        entry = parse_manifest(self.published())[0]
        self.assertEqual(entry['url'], second_url)
        self.assertEqual(entry['destination_name'], 'renamed-again')
        self.assertEqual(entry['legacy_url'], OLD)
        self.assertEqual(entry['legacy_destination_name'], 'repo')
        self.assertEqual(self.journal(), {})

    def test_tampered_journal_blocks_the_batch(self):
        config = text(document({'id': 22}))
        self.write_world(config, {'repo': repository('repo', 22)}, fail_publish=True)
        self.assertEqual(self.run_scheduler(config).returncode, 1)
        path = self.journals / 'migrations.json'
        path.write_bytes(path.read_bytes().replace(b'"phase": "renamed"', b'"phase": "configured"'))
        result = self.run_scheduler(config)
        # A journal that fails authentication stops the batch before any worker.
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('Batch configuration or lock error', result.stderr)
        self.assertNotIn('Repository 1', result.stdout)
        self.assertEqual(self.read_world()['config'], config)

    def test_pending_intent_without_publication_target_stops_the_batch(self):
        config = text(document({'id': 22}))
        self.write_world(config, {'repo': repository('repo', 22)}, fail_publish=True)
        self.assertEqual(self.run_scheduler(config).returncode, 1)
        # Drop the publication target: the recorded intent must not be ignored.
        command = [sys.executable, str(self.runner), 'run', '--manifest', str(self.manifest),
                   '--token-file', 'unused', '--timestamp-secret-file', str(self.secret),
                   '--journal-dir', str(self.journals), '--owner', 'dest', '--port', '3300',
                   '--lock', str(self.tmp / 'lock'), '--sync-python', str(self.wrapper)]
        environment = {**os.environ, 'WORLD': str(self.world), 'PYTHONPATH': str(ROOT),
                       'MOVES': json.dumps({OLD: NEW})}
        result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('Migration: FAILED: configuration publication is not configured', result.stdout)
        self.assertNotIn('Batch:', result.stdout)
