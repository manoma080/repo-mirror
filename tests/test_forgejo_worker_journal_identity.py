import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from repo_mirror.forgejo_sync import _sync_requested, parse_manifest
from repo_mirror.migration_attestation import forgejo_claim, sign


class WorkerJournalIdentityTests(unittest.TestCase):
    def run_worker(self, url, source_id=None, destination_id=None, legacy=None):
        source = {'url': url}
        mirror = {'name': 'current'}
        if source_id is not None:
            source['id'] = source_id
        if destination_id is not None:
            mirror['id'] = destination_id
        if legacy:
            mirror['legacy'] = legacy
        config = {'destinations': {'local': {}}, 'repositories': [
            {'source': source, 'mirrors': {'local': mirror}}]}
        entry = parse_manifest(config)[0]
        secret = 'fixture-signing-secret-0123456789abcdef'
        if legacy:
            entry['signature'] = sign(forgejo_claim(entry, 3300, 'dest'), secret)
        identity = entry['identity']
        src = Mock()
        src.request.return_value = {'id': source_id, 'full_name': '/'.join(url.split('/')[-2:]), 'clone_url': url + '.git'}
        src.pages.return_value = []
        dest = Mock(run_pin=(destination_id, 'marker'))
        dest.begin_initialization.return_value = False
        adapter = Mock(target='http://127.0.0.1:3300/api/v1/repos/dest/current')
        with tempfile.TemporaryDirectory() as tmp, \
             patch('repo_mirror.forgejo_sync.PublicForgejoSourceAdapter', return_value=src), \
             patch('repo_mirror.forgejo_sync.ForgejoSourceAdapter', return_value=src), \
             patch('repo_mirror.forgejo_sync.ForgejoDestinationAdapter', return_value=adapter), \
             patch('repo_mirror.forgejo_sync.validate_source_release_listing'), \
             patch('repo_mirror.forgejo_sync.validate_source_latest'), \
             patch('repo_mirror.forgejo_sync.fetch_source_latest'), \
             patch('repo_mirror.forgejo_sync.sync_refs'), \
             patch('repo_mirror.forgejo_sync.snapshot_refs', return_value=(None, {}, {})), \
             patch('repo_mirror.forgejo_sync.mirror_releases') as releases:
            result = _sync_requested(SimpleNamespace(port=3300, owner='dest'), [identity], dest,
                                     None, 'token', secret, tmp, mappings={identity: entry})
        self.assertEqual(result, 0)
        return releases.call_args

    def test_pinned_forgejo_worker_preserves_origins_and_signed_legacy_alias(self):
        call = self.run_worker('https://git.example/new/repo', 11, 22,
                               {'url': 'https://git.example/old/repo', 'name': 'old-copy', 'signature': 'a' * 64})
        self.assertEqual(call.kwargs['journal_identity'], ('https://git.example', 11, 'http://127.0.0.1:3300', 22))
        self.assertEqual(call.kwargs['journal_source']['owner'], 'old')
        self.assertEqual(call.kwargs['journal_name'], 'repo')
        self.assertTrue(call.kwargs['recovery_target'].endswith('/old-copy'))

    def test_pinned_github_worker_uses_github_origin(self):
        call = self.run_worker('https://github.com/manoma080/mirror-repo', 11, 22)
        self.assertEqual(call.kwargs['journal_identity'], ('https://github.com', 11, 'http://127.0.0.1:3300', 22))

    def test_partial_pins_keep_legacy_name_identity(self):
        call = self.run_worker('https://git.example/alice/repo', 11)
        self.assertIsNone(call.kwargs.get('journal_identity'))
