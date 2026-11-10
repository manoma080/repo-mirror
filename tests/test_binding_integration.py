import copy
import hashlib
import os
import unittest
from unittest.mock import patch

from repo_mirror import one_time_draft_binding as binding


class FakeSource:
    def __init__(self):
        self.contents = {name: name.encode() for name in binding.FILES}
        self.assets = [dict(id=i, name=name, size=len(data), browser_download_url='https://git.etawen.dev/earthonion/yt_adskip_ps4/releases/download/v1.36/' + name) for i, (name, data) in enumerate(self.contents.items(), 1)]
        self.release = dict(id=141, tag_name='v1.36', name='v1.36', body='GoldHEN plugin (release + debug PRX)', draft=False, prerelease=False, target_commitish='eacb279874b53a176d86504249cf0d50ab276336', assets=self.assets)
    def request(self, method, url, data=None):
        assert method == 'GET'
        return copy.deepcopy(self.assets if url.endswith('/assets') else self.release)
    def download(self, url, output):
        output.write(self.contents[url.rsplit('/', 1)[-1]])


class FakeDestination:
    def __init__(self, source):
        self.repo = {'full_name':'manoma080/mirror-earthonion-yt_adskip_ps4', 'owner':{'login':'manoma080'}, 'description':binding.SOURCE}
        assets = [dict(id=i+10, name=n, size=len(c), state='uploaded', digest='sha256:'+hashlib.sha256(c).hexdigest()) for i,(n,c) in enumerate(source.contents.items())]
        assets.append(dict(id=99, name='TIMESTAMPS.txt', size=587, state='uploaded', digest='sha256:96f8c48f6d31b0ea87b053aef1a076b1519ed96d177406815e43580cedb90a48'))
        self.release = dict(id=437729977, tag_name='untagged-446b630fff7654a5b702', name='v1.36', body='GoldHEN plugin (release + debug PRX)', draft=True, prerelease=False, target_commitish='main', assets=assets)
        self.patches = []
        self.other_releases = []
        self.reads = 0
        self.change_at = None
    def request(self, method, url, data=None):
        if method == 'PATCH':
            self.patches.append(copy.deepcopy(data)); self.release.update(data)
            return copy.deepcopy(self.release)
        if url == binding.TARGET:
            return copy.deepcopy(self.repo)
        self.reads += 1
        if self.change_at == self.reads:
            self.release['name'] = 'changed'
        return copy.deepcopy(self.release)
    def pages(self, url):
        if url == binding.TARGET + '/releases':
            return copy.deepcopy([self.release] + self.other_releases)
        return copy.deepcopy(self.release['assets'])


class BindingIntegrationTests(unittest.TestCase):
    def setUp(self):
        env_patch = patch.dict(os.environ, {'MIRROR_TIMESTAMP_SECRET': 'test-secret'}); env_patch.start(); self.addCleanup(env_patch.stop)
        self.src = FakeSource(); self.dst = FakeDestination(self.src)
        self.hashes = {n:hashlib.sha256(c).hexdigest() for n,c in self.src.contents.items()}
        self.hash_patch = patch.object(binding, 'FILES', self.hashes); self.hash_patch.start(); self.addCleanup(self.hash_patch.stop)
    def run_binding(self):
        return binding.reconcile(self.src, self.dst, 'test-secret')
    def test_other_tagged_draft_never_writes(self):
        self.dst.other_releases.append(dict(id=9, tag_name='different', draft=True))
        with self.assertRaises(ValueError): self.run_binding()
        self.assertEqual(self.dst.patches, [])
    def test_other_untagged_draft_never_writes(self):
        self.dst.other_releases.append(dict(id=9, tag_name='untagged-other', draft=True, name='unrelated'))
        with self.assertRaises(ValueError): self.run_binding()
        self.assertEqual(self.dst.patches, [])
    def test_competing_exact_tag_never_writes(self):
        self.dst.other_releases.append(dict(id=9, tag_name='v1.36', draft=False))
        with self.assertRaises(ValueError): self.run_binding()
        self.assertEqual(self.dst.patches, [])
    def test_patch_only_body_then_idempotent(self):
        self.assertEqual(self.run_binding(), 'bound')
        self.assertEqual(set(self.dst.patches[0]), {'body'})
        self.assertEqual(self.run_binding(), 'already bound')
        self.assertEqual(len(self.dst.patches), 1)
    def test_volatile_repository_clone_token_changes_between_reads(self):
        original = self.dst.request
        count = 0
        def request(method, url, data=None):
            nonlocal count
            result = original(method, url, data)
            if url == binding.TARGET:
                count += 1
                result['temp_clone_token'] = 'ephemeral-' + str(count)
            return result
        self.dst.request = request
        self.assertEqual(self.run_binding(), 'bound')
        self.assertEqual(len(self.dst.patches), 1)

    def test_asset_evidence_mutation_between_reads_stops(self):
        for field, value in [('id', 12345), ('digest', 'sha256:bad'),
                             ('size', 1), ('name', 'replacement'), ('state', 'starter')]:
            with self.subTest(field=field):
                self.setUp()
                original = self.src.download
                def download(url, output):
                    original(url, output)
                    self.dst.release['assets'][0][field] = value
                self.src.download = download
                with self.assertRaises(ValueError):
                    self.run_binding()
                self.assertEqual(self.dst.patches, [])

    def test_source_download_count_change_does_not_block(self):
        original = self.src.download
        def download(url, output):
            original(url, output)
            for asset in self.src.assets:
                asset['download_count'] = asset.get('download_count', 0) + 1
        self.src.download = download
        self.assertEqual(self.run_binding(), 'bound')
        self.assertEqual(len(self.dst.patches), 1)

    def test_source_download_url_swap_after_validation_never_writes(self):
        original = self.src.download
        def download(url, output):
            original(url, output)
            self.src.assets[0]['browser_download_url'] = 'https://example.org/replacement'
        self.src.download = download
        with self.assertRaisesRegex(ValueError, 'state changed before write'):
            self.run_binding()
        self.assertEqual(self.dst.patches, [])

    def test_unrelated_fields_changed_after_validation_never_write(self):
        for field in ('url', 'unknown_url', 'unknown_at', 'unknown_count', 'temp_clone_token'):
            with self.subTest(field=field):
                self.setUp()
                self.dst.release['metadata'] = {field: 'before'}
                original = self.src.download
                def download(url, output):
                    original(url, output)
                    self.dst.release['metadata'][field] = 'after'
                self.src.download = download
                with self.assertRaisesRegex(ValueError, 'state changed before write'):
                    self.run_binding()
                self.assertEqual(self.dst.patches, [])

    def test_changed_between_snapshot_and_patch_stops(self):
        self.dst.change_at = 2
        with self.assertRaises(ValueError): self.run_binding()
        self.assertEqual(self.dst.patches, [])
    def test_corrupted_post_patch_readback_fails(self):
        original = self.dst.request
        def request(method, url, data=None):
            result = original(method, url, data)
            if method == 'PATCH':
                self.dst.release['draft'] = False
            return result
        self.dst.request = request
        with self.assertRaises(ValueError): self.run_binding()
        self.assertEqual(len(self.dst.patches), 1)
    def test_empty_secret_never_writes(self):
        with self.assertRaises(ValueError): binding.reconcile(self.src, self.dst, '')
        self.assertEqual(self.dst.patches, [])
    def test_signature_env_mismatch_never_writes(self):
        with self.assertRaises(ValueError): binding.reconcile(self.src, self.dst, 'other-secret')
        self.assertEqual(self.dst.patches, [])
    def test_mismatch_stops(self):
        for mutation in ('owner', 'repository', 'description', 'draft', 'release_id', 'extra', 'digest', 'source', 'source_extra', 'download', 'url', 'timestamp'):
            with self.subTest(mutation=mutation):
                self.setUp()
                if mutation == 'owner': self.dst.repo['owner']['login']='other'
                if mutation == 'repository': self.dst.repo['full_name']='other/repo'
                if mutation == 'description': self.dst.repo['description']='other'
                if mutation == 'draft': self.dst.release['draft']=False
                if mutation == 'release_id': self.dst.release['id']=1
                if mutation == 'extra': self.dst.release['assets'].append(dict(name='extra'))
                if mutation == 'digest': self.dst.release['assets'][0]['digest']='sha256:'+'0'*64
                if mutation == 'source': self.src.release['tag_name']='v1.35'
                if mutation == 'source_extra': self.src.assets.append(dict(name='extra'))
                if mutation == 'download': self.src.contents[next(iter(self.src.contents))] = b'bad'
                if mutation == 'url': self.src.assets[0]['browser_download_url'] = 'https://example.org/file'
                if mutation == 'timestamp': self.dst.release['assets'][-1]['size']=0
                with self.assertRaises(ValueError): self.run_binding()
                self.assertEqual(self.dst.patches, [])
