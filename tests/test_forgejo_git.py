import unittest

from repo_mirror.forgejo_git import APIError, ForgejoDestination


class ProvisionTests(unittest.TestCase):
    def test_create_private_repository_with_provenance_and_actions_disabled(self):
        class API:
            def __init__(self):
                self.repo = None

            def request(self, method, path, payload=None):
                if method == "POST":
                    self.repo = dict(payload, full_name="mirror/project", has_actions=False)
                    return self.repo
                if self.repo is None:
                    raise APIError(404)
                return self.repo

        api = API()
        result = ForgejoDestination(api, "mirror").ensure_repository("project", "manoma080/project")
        self.assertTrue(result["private"])
        self.assertFalse(result["has_actions"])
        self.assertEqual(result["description"], "repo-mirror-source: github.com/manoma080/project")


class SyncPlanTests(unittest.TestCase):
    def test_rejects_divergent_branch_and_moved_tag(self):
        from repo_mirror.forgejo_git import plan_updates

        with self.assertRaises(RuntimeError):
            plan_updates({"refs/heads/main": "new"}, {"refs/heads/main": "old"}, lambda a, b: False)
        with self.assertRaises(RuntimeError):
            plan_updates({"refs/tags/v1": "new"}, {"refs/tags/v1": "old"}, lambda a, b: True)

    def test_preserves_destination_only_refs(self):
        from repo_mirror.forgejo_git import plan_updates

        result = plan_updates(
            {"refs/heads/main": "new"},
            {"refs/heads/main": "old", "refs/heads/local": "other"},
            lambda a, b: True,
        )
        self.assertEqual(result, [("refs/heads/main", "new", "old")])


class DefaultBranchTests(unittest.TestCase):
    def test_syncs_default_after_first_push_and_reads_it_back(self):
        import subprocess
        from unittest.mock import patch
        from repo_mirror.forgejo_git import sync_refs

        oid = 'a' * 40
        class API:
            base_url = 'http://127.0.0.1:3300'
            def __init__(self):
                self.branch = 'master'
                self.calls = []
            def token(self):
                return 'local-token'
            def request(self, method, path, payload=None):
                self.calls.append((method, path, payload))
                if method == 'PATCH':
                    self.branch = payload['default_branch']
                return {'full_name': 'mirror/project', 'private': True,
                        'description': 'repo-mirror-source: github.com/manoma080/project',
                        'has_actions': False, 'default_branch': self.branch}
        api = API()
        destination = ForgejoDestination(api, 'mirror')
        remote_reads = 0
        def run(args, **kwargs):
            nonlocal remote_reads
            output = b''
            if 'for-each-ref' in args:
                output = f'{oid} refs/heads/main\n'.encode()
            elif '--symref' in args:
                output = f'ref: refs/heads/main\tHEAD\n{oid}\tHEAD\n'.encode()
            elif 'ls-remote' in args:
                remote_reads += 1
                if remote_reads > 1:
                    output = f'{oid}\trefs/heads/main\n'.encode()
            return subprocess.CompletedProcess(args, 0, output, b'')
        with patch('repo_mirror.forgejo_git.subprocess.run', side_effect=run), patch.dict('os.environ', {'GITHUB_TOKEN': 'secret'}):
            sync_refs(destination, 'project', 'manoma080/project')
        self.assertIn(('PATCH', '/api/v1/repos/mirror/project', {'default_branch': 'main'}), api.calls)
        self.assertEqual(api.calls[-1][0], 'GET')
        self.assertEqual(api.branch, 'main')


    def test_existing_default_and_failure_paths(self):
        import subprocess
        from unittest.mock import patch
        from repo_mirror.forgejo_git import sync_refs

        oid = 'a' * 40
        for scenario in ('same', 'changed', 'invalid_head', 'readback', 'wrong_repo', 'patch_failure'):
            with self.subTest(scenario=scenario):
                class API:
                    base_url = 'http://127.0.0.1:3300'
                    def __init__(self):
                        self.branch = 'main' if scenario == 'same' else 'old'
                        self.patches = 0
                        self.gets = 0
                    def token(self):
                        return 'local'
                    def request(self, method, path, payload=None):
                        if method == 'PATCH':
                            self.patches += 1
                            if scenario == 'patch_failure':
                                raise APIError(500)
                            if scenario != 'readback':
                                self.branch = payload['default_branch']
                        else:
                            self.gets += 1
                        return {'full_name': 'mirror/other' if scenario == 'wrong_repo' and self.gets > 1 else 'mirror/project',
                                'private': True, 'has_actions': False,
                                'description': 'repo-mirror-source: github.com/manoma080/project',
                                'default_branch': self.branch}
                api = API()
                head_reads = 0
                def run(args, **kwargs):
                    nonlocal head_reads
                    output = b''
                    if 'for-each-ref' in args:
                        output = f'{oid} refs/heads/main\n'.encode()
                    elif '--symref' in args:
                        head_reads += 1
                        branch = 'other' if scenario == 'changed' and head_reads > 1 else 'main'
                        output = b'' if scenario == 'invalid_head' else f'ref: refs/heads/{branch}\tHEAD\n{oid}\tHEAD\n'.encode()
                    elif 'ls-remote' in args:
                        output = f'{oid}\trefs/heads/main\n'.encode()
                    return subprocess.CompletedProcess(args, 0, output, b'')
                with patch('repo_mirror.forgejo_git.subprocess.run', side_effect=run), patch.dict('os.environ', {'GITHUB_TOKEN': 'secret'}):
                    if scenario == 'same':
                        self.assertEqual(sync_refs(ForgejoDestination(api, 'mirror'), 'project', 'manoma080/project'), 0)
                        self.assertEqual(api.patches, 0)
                    else:
                        with self.assertRaises(RuntimeError):
                            sync_refs(ForgejoDestination(api, 'mirror'), 'project', 'manoma080/project')
                        if scenario in ('changed', 'invalid_head', 'wrong_repo'):
                            self.assertEqual(api.patches, 0)


class FinalRefConsistencyTests(unittest.TestCase):
    def test_final_ref_consistency(self):
        import subprocess
        from unittest.mock import patch
        from repo_mirror.forgejo_git import sync_refs

        a, b, c = 'a' * 40, 'b' * 40, 'c' * 40
        snapshot = {'refs/heads/main': a, 'refs/heads/feature': b, 'refs/tags/v1': c}
        for scenario in ('source_add', 'source_delete', 'source_change', 'tag_change',
                         'destination_missing', 'destination_changed', 'destination_extra'):
            with self.subTest(scenario=scenario):
                class API:
                    base_url = 'http://127.0.0.1:3300'
                    branch = 'old'
                    def token(self):
                        return 'local'
                    def request(self, method, path, payload=None):
                        if method == 'PATCH':
                            self.branch = payload['default_branch']
                        return {'full_name': 'mirror/project', 'private': True,
                                'description': 'repo-mirror-source: github.com/manoma080/project',
                                'has_actions': False, 'default_branch': self.branch}
                api = API()
                source_final = snapshot.copy()
                destination_final = snapshot.copy()
                if scenario == 'source_add':
                    source_final['refs/heads/new'] = b
                elif scenario == 'source_delete':
                    del source_final['refs/heads/feature']
                elif scenario == 'source_change':
                    source_final['refs/heads/feature'] = c
                elif scenario == 'tag_change':
                    source_final['refs/tags/v1'] = b
                elif scenario == 'destination_missing':
                    del destination_final['refs/tags/v1']
                elif scenario == 'destination_changed':
                    destination_final['refs/heads/feature'] = c
                else:
                    destination_final['refs/heads/local'] = c
                def listing(refs):
                    return ''.join(f'{oid}\t{ref}\n' for ref, oid in refs.items()).encode()
                def run(args, **kwargs):
                    output = b''
                    if 'for-each-ref' in args:
                        output = listing(snapshot)
                    elif '--symref' in args:
                        output = f'ref: refs/heads/main\tHEAD\n{a}\tHEAD\n'.encode()
                    elif 'ls-remote' in args:
                        is_source = any('github.com/' in arg for arg in args)
                        output = listing(source_final if is_source else
                                         destination_final if api.branch == 'main' else snapshot)
                    return subprocess.CompletedProcess(args, 0, output, b'')
                with patch('repo_mirror.forgejo_git.subprocess.run', side_effect=run):
                    if scenario == 'destination_extra':
                        self.assertEqual(sync_refs(ForgejoDestination(api, 'mirror'), 'project',
                                                   'manoma080/project', source_token='secret'), 0)
                    else:
                        with self.assertRaises(RuntimeError):
                            sync_refs(ForgejoDestination(api, 'mirror'), 'project',
                                      'manoma080/project', source_token='secret')


class TransportTests(unittest.TestCase):
    def test_initial_push_uses_separate_credentials_and_no_secret_arguments(self):
        import subprocess
        from unittest.mock import patch

        from repo_mirror.forgejo_git import sync_refs

        class API:
            base_url = "http://127.0.0.1:3300"

            def token(self):
                return "forge-secret"

            def request(self, method, path, payload=None):
                return {"full_name": "mirror/project", "private": True,
                        "description": "repo-mirror-source: github.com/manoma080/project",
                        "has_actions": False, "default_branch": "main"}

        class Destination(ForgejoDestination):
            def __init__(self):
                super().__init__(API(), "mirror")

        calls = []
        oid = "a" * 40

        def run(args, **kwargs):
            calls.append((args, kwargs["env"].copy()))
            output = b""
            if "for-each-ref" in args:
                output = f"{oid} refs/heads/main\n".encode()
            if "--symref" in args:
                output = f"ref: refs/heads/main\tHEAD\n{oid}\tHEAD\n".encode()
            elif "ls-remote" in args and len([c for c, _ in calls if "ls-remote" in c and "--symref" not in c]) > 1:
                output = f"{oid}\trefs/heads/main\n".encode()
            return subprocess.CompletedProcess(args, 0, output, b"")

        issued = []
        def provider():
            token = f'github-refresh-{len(issued)}'
            issued.append(token)
            return token

        with patch("repo_mirror.forgejo_git.subprocess.run", side_effect=run):
            sync_refs(Destination(), "project", "manoma080/project", source_token=provider)
        source_tokens = [env['MIRROR_TOKEN'] for args, env in calls
                         if any('github.com/' in arg for arg in args)]
        self.assertEqual(source_tokens, issued)
        self.assertGreater(len(set(source_tokens)), 1)
        self.assertTrue(any("push" in args for args, _ in calls))
        self.assertTrue(all("secret" not in " ".join(args) for args, _ in calls))
        self.assertTrue(
            all(env.get("MIRROR_TOKEN") != "github-secret" for args, env in calls if "push" in args)
        )


class SafetyTests(unittest.TestCase):
    def test_rejects_unowned_existing_repository_before_modifying(self):
        class API:
            def request(self, method, path, payload=None):
                assert method == "GET"
                return {
                    "full_name": "mirror/project",
                    "private": True,
                    "description": "unrelated",
                    "has_actions": True,
                }

        with self.assertRaises(RuntimeError):
            ForgejoDestination(API(), "mirror").ensure_repository("project", "manoma080/project")

    def test_local_api_rejects_nonlocal_and_insecure_token_file(self):
        import os
        import tempfile
        from pathlib import Path

        from repo_mirror.forgejo_git import LocalForgejoAPI

        with self.assertRaises(ValueError):
            LocalForgejoAPI("http://localhost.evil.example:3300", "/unused")
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "token"
            token.write_text("secret")
            os.chmod(token, 0o644)
            api = LocalForgejoAPI("http://127.0.0.1:3300", token)
            with self.assertRaises(ValueError):
                api.token()
            os.chmod(token, 0o600)
            self.assertEqual(api.token(), "secret")


if __name__ == "__main__":
    unittest.main()
