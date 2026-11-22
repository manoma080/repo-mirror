import json
import unittest
from urllib.parse import quote
from repo_mirror.forgejo_git import APIError

class GroupOnboardingTests(unittest.TestCase):
    def test_launcher_accepts_group_only_manifest(self):
        from scheduler.auto_update import valid_manifest
        cfg = {'destinations': {'forgejo-home': {}}, 'repositories': [], 'groups': [
            {'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'team/sub', 'id': 7, 'repos': 'all'}]}
        self.assertTrue(valid_manifest(cfg))
        cfg['groups'][0]['id'] = True
        self.assertFalse(valid_manifest(cfg))

    def test_scheduler_requires_config_authority_before_lock(self):
        import tempfile
        from pathlib import Path
        from repo_mirror.forgejo_schedule import main
        cfg = {'destinations': {'forgejo-home': {}}, 'repositories': [], 'groups': [
            {'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'team/sub', 'id': 7, 'repos': 'all'}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'manifest').write_text(json.dumps(cfg))
            status = main(['run', '--manifest', str(root/'manifest'), '--token-file', 'x', '--timestamp-secret-file', 'x', '--journal-dir', directory, '--owner', 'local', '--lock', str(root/'lock'), '--asset-max-bytes', '100'])
            self.assertEqual(status, 2)
            self.assertFalse((root/'lock').exists())

    def test_scheduler_invokes_onboarding_before_worker(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from repo_mirror.forgejo_schedule import main
        cfg = {'destinations': {'forgejo-home': {}}, 'repositories': [], 'groups': [
            {'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'team', 'id': 7, 'repos': 'all'}]}
        entry = {'source': {'type': 'gitlab', 'url': 'https://gitlab.com/team/p', 'id': 42},
                 'mirrors': {'forgejo-home': {'name': 'gitlab-p', 'id': 9}}}
        published = json.dumps({**cfg, 'repositories': [entry]}).encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'manifest').write_text(json.dumps(cfg))
            (root/'secret').write_text('x'*32); (root/'secret').chmod(0o600)
            (root/'journal').mkdir(mode=0o700)
            events = []
            def onboard(*args, **kwargs):
                events.append('publish')
                return published
            def spawn(command, **kwargs):
                snapshot = json.loads(Path(command[command.index('--config')+1]).read_text())
                self.assertNotIn('groups', snapshot)
                self.assertEqual(snapshot['repositories'], [entry])
                events.append('worker')
                from unittest.mock import Mock
                return Mock(poll=lambda: 0, pid=999999999, wait=lambda: 0)
            with patch('repo_mirror.forgejo_migration.build_migration', return_value=SimpleNamespace(cas=object())), \
                 patch('repo_mirror.gitlab_onboarding_forgejo.onboard', side_effect=onboard), \
                 patch('repo_mirror.gitlab_onboarding_forgejo.AnonymousGroupAPI'), \
                 patch('repo_mirror.forgejo_schedule.subprocess.Popen', side_effect=spawn):
                result = main(['run', '--manifest', str(root/'manifest'), '--token-file', 'unused',
                               '--timestamp-secret-file', str(root/'secret'), '--journal-dir', str(root/'journal'),
                               '--owner', 'local', '--lock', str(root/'lock'), '--config-repo', 'a/b', '--asset-max-bytes', '100'])
            self.assertEqual(result, 0)
            self.assertEqual(events, ['publish', 'worker'])

    def test_publish_then_repeat_without_creating(self):
        from repo_mirror.gitlab_onboarding_forgejo import onboard
        group = {'type': 'gitlab', 'base_url': 'https://gitlab.com', 'owner': 'team/sub', 'id': 7, 'repos': 'all'}
        doc = {'destinations': {'forgejo-home': {}}, 'repositories': [], 'groups': [group]}
        before = json.dumps(doc).encode()
        class Source:
            def request(self, method, url):
                assert url.endswith('/groups/' + quote('team/sub', safe=''))
                return {'id': 7, 'visibility': 'public', 'full_path': 'team/sub'}
            def pages(self, url):
                return [{'id': 42, 'path': 'p', 'path_with_namespace': 'team/sub/p', 'visibility': 'public', 'http_url_to_repo': 'https://gitlab.com/team/sub/p.git'}]
        class CAS:
            data = before
            def read(self): return self.data
            def replace(self, old, new):
                assert self.data == old
                self.data = new
        class Destination:
            repos = {}
            def request(self, method, path, payload=None):
                if method == 'GET':
                    if path == '/api/v1/user': return {'login':'local'}
                    if path.rsplit('/', 1)[-1] not in self.repos: raise APIError(404)
                    return self.repos[path.rsplit('/', 1)[-1]]
                assert method == 'POST'
                name = payload['name']
                self.repos[name] = {'id':9, 'full_name':'local/'+name, 'private':True, 'has_actions':False, 'description':payload['description']}
                return self.repos[name]
        class Project:
            def __init__(self, origin, path, project_id): pass
            def project(self): return {}
        cas, dst = CAS(), Destination()
        result = onboard(before, Source(), dst, cas, 'local', project_factory=Project)
        entry = json.loads(result)['repositories'][0]
        self.assertEqual(entry['source']['id'], 42)
        self.assertEqual(entry['mirrors']['forgejo-home']['id'], 9)
        self.assertEqual(result, cas.read())
        self.assertEqual(onboard(result, Source(), dst, cas, 'local', project_factory=Project), result)
        self.assertEqual(len(dst.repos), 1)

        # A failed publication leaves a partial destination and must not be
        # adopted by a fresh retry, even if its provenance marker matches.
        from repo_mirror.gitlab_onboarding_forgejo import PartialOnboarding
        class FailedCAS(CAS):
            data = before
            def replace(self, old, new): raise RuntimeError('conflict')
        failed, partial = FailedCAS(), Destination()
        partial.repos = {}
        with self.assertRaises(PartialOnboarding) as error:
            onboard(before, Source(), partial, failed, 'local', project_factory=Project)
        self.assertEqual(len(error.exception.names), 1)
        self.assertEqual(failed.read(), before)
        with self.assertRaisesRegex(ValueError, 'unpinned existing destination'):
            onboard(before, Source(), partial, failed, 'local', project_factory=Project)

    def test_noop_rechecks_authority_after_discovery(self):
        from repo_mirror.gitlab_onboarding_forgejo import onboard
        group = {'type':'gitlab','base_url':'https://gitlab.com','owner':'team','id':7,'repos':'all'}
        doc = json.dumps({'destinations':{'forgejo-home':{}},'repositories':[],'groups':[group]}).encode()
        class CAS:
            calls = 0
            def read(self):
                self.calls += 1
                return doc if self.calls == 1 else b'changed'
        class Source:
            def request(self, method, url): return {'id':7,'visibility':'public','full_path':'team'}
            def pages(self, url): return []
        with self.assertRaisesRegex(ValueError, 'configuration changed'):
            onboard(doc, Source(), object(), CAS(), 'local')

    def test_launcher_rejects_malformed_config_authority(self):
        # A separate configuration repository is now supported; only an
        # unparseable name is refused, and before any state is created.
        from scheduler.auto_update import main
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            result = main(['--remote','https://github.com/owner/one.git','--state-dir',str(root/'state'),
                           '--job-lock',str(root/'job'), '--', '--lock',str(root/'lock'), '--config-repo','owner/'])
            self.assertEqual(result, 2)
            self.assertFalse((root/'state').exists())

    def test_scheduler_excludes_removed_group_mapping_but_keeps_explicit(self):
        import hashlib, tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch, Mock
        from repo_mirror.forgejo_schedule import main
        group = {'type':'gitlab','base_url':'https://gitlab.com','owner':'team','id':7,'repos':'all'}
        name = 'gitlab-' + hashlib.sha256(b'https://gitlab.com\0' + b'7\0' + b'42').hexdigest()[:24]
        removed = {'source':{'type':'gitlab','url':'https://gitlab.com/team/gone','id':42},'mirrors':{'forgejo-home':{'name':name,'id':9}}}
        explicit = {'source':{'url':'https://github.com/owner/keep'},'mirrors':{'forgejo-home':{}}}
        cfg = {'destinations':{'forgejo-home':{}},'repositories':[removed,explicit],'groups':[group]}
        doc = json.dumps(cfg).encode()
        class Source:
            def request(self, method, url): return {'id':7,'visibility':'public','full_path':'team'}
            def pages(self, url): return []
        class CAS:
            def read(self): return doc
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root/'manifest').write_bytes(doc)
            (root/'secret').write_text('x'*32); (root/'secret').chmod(0o600); (root/'journal').mkdir(mode=0o700)
            launched = []
            def spawn(command, **kwargs):
                snapshot = json.loads(Path(command[command.index('--config')+1]).read_text())
                launched.append((command[-1], snapshot['repositories']))
                return Mock(poll=lambda:0, pid=999999999, wait=lambda:0)
            with patch('repo_mirror.forgejo_migration.build_migration', return_value=SimpleNamespace(cas=CAS())), \
                 patch('repo_mirror.gitlab_onboarding_forgejo.AnonymousGroupAPI', return_value=Source()), \
                 patch('repo_mirror.forgejo_git.LocalForgejoAPI'), \
                 patch('repo_mirror.forgejo_schedule.subprocess.Popen', side_effect=spawn):
                status = main(['run','--manifest',str(root/'manifest'),'--token-file','unused','--timestamp-secret-file',str(root/'secret'),
                               '--journal-dir',str(root/'journal'),'--owner','local','--lock',str(root/'lock'),
                               '--config-repo','a/b','--asset-max-bytes','100'])
            self.assertEqual(status, 0)
            self.assertEqual(launched, [('https://github.com/owner/keep', [explicit])])
            self.assertEqual((root/'manifest').read_bytes(), doc)

    def test_launcher_runs_scheduler_with_group_snapshot(self):
        import tempfile, subprocess, sys
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from scheduler import auto_update
        from repo_mirror.forgejo_schedule import main as schedule
        group = {'type':'gitlab','base_url':'https://gitlab.com','owner':'team','id':7,'repos':'all'}
        doc = json.dumps({'destinations':{'forgejo-home':{}},'repositories':[],'groups':[group]}).encode()
        class Source:
            def request(self, method, url): return {'id':7,'visibility':'public','full_path':'team'}
            def pages(self, url): return []
        class CAS:
            def read(self): return doc
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); repo = root/'repo'; repo.mkdir(); (repo/'repo_mirror').mkdir()
            (repo/'repo_mirror'/'forgejo_schedule.py').write_text('# scheduler present')
            (repo/'config-forgejo.json').write_bytes(doc)
            def git(*args): subprocess.run(['git','-C',str(repo),*args],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            git('init','-b','main'); git('config','user.email','test@example.com'); git('config','user.name','test')
            git('add','.'); git('commit','-m','fixture')
            helper = root/'helper.py'
            helper.write_text('import os,sys\na=sys.argv[sys.argv.index("--")+1:]\na[-2]='+repr(str(repo))+'\nos.execvp(a[0],a)\n')
            (root/'secret').write_text('x'*32); (root/'secret').chmod(0o600); (root/'journal').mkdir(mode=0o700)
            real_checked = auto_update.checked_process
            entered = []
            def checked(command, **kwargs):
                if kwargs.get('scheduler'):
                    entered.append(True)
                    with patch('repo_mirror.forgejo_migration.build_migration',return_value=SimpleNamespace(cas=CAS())), \
                         patch('repo_mirror.gitlab_onboarding_forgejo.AnonymousGroupAPI',return_value=Source()), \
                         patch('repo_mirror.forgejo_git.LocalForgejoAPI'):
                        return schedule(command[3:])
                return real_checked(command, **kwargs)
            with patch.object(auto_update, 'checked_process', side_effect=checked):
                status = auto_update.main(['--remote','https://github.com/a/b.git','--state-dir',str(root/'state'),
                    '--job-lock',str(root/'job'),'--helper',str(helper),'--', '--lock',str(root/'lock'),
                    '--token-file','unused','--timestamp-secret-file',str(root/'secret'),'--journal-dir',str(root/'journal'),
                    '--owner','local','--config-repo','a/b','--asset-max-bytes','100'])
            self.assertEqual(status, 0)
            self.assertEqual(entered,[True])

    def test_removed_group_does_not_schedule_its_reserved_mapping(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from repo_mirror.forgejo_schedule import main
        entry = {'source':{'type':'gitlab','url':'https://gitlab.com/team/gone','id':42},
                 'mirrors':{'forgejo-home':{'name':'gitlab-3cc78bced3ec5ce37b80b6c2','id':9}}}
        doc = json.dumps({'destinations':{'forgejo-home':{}},'repositories':[entry]}).encode()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'manifest').write_bytes(doc); (root/'secret').write_text('x'*32)
            (root/'secret').chmod(0o600); (root/'journal').mkdir(mode=0o700)
            with patch('repo_mirror.forgejo_schedule.subprocess.Popen') as worker:
                worker.side_effect = AssertionError('removed group was scheduled')
                status=main(['run','--manifest',str(root/'manifest'),'--token-file','unused','--timestamp-secret-file',str(root/'secret'),
                             '--journal-dir',str(root/'journal'),'--owner','local','--lock',str(root/'lock'),'--asset-max-bytes','100'])
            self.assertEqual(status,0)
            worker.assert_not_called()
