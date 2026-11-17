import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repo_mirror.forgejo_git import ForgejoDestination, LocalForgejoAPI


class ForgejoTokenBindingTests(unittest.TestCase):
    def test_personal_create_uses_same_token_as_identity_after_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / 'token'
            token_file.write_text('first')
            token_file.chmod(0o600)
            calls = []
            repo = {'id': 1, 'full_name': 'mirror/project', 'private': True,
                    'description': 'repo-mirror-source: github.com/alice/project [repo-mirror:initializing:v1]',
                    'has_actions': False}

            class Connection:
                def __init__(self, *args, **kwargs): pass
                def request(self, method, path, body=None, headers=None):
                    calls.append((method, path, headers['Authorization']))
                    self.method, self.path = method, path
                    if path == '/api/v1/user':
                        token_file.write_text('second')
                def getresponse(self):
                    method, path = self.method, self.path
                    missing = len(calls) == 1
                    payload = {'login': 'mirror'} if path == '/api/v1/user' else repo
                    class Response:
                        status = 404 if missing else 200
                        def read(self): return json.dumps(payload).encode()
                    return Response()
                def close(self): pass

            with patch('repo_mirror.forgejo_git.http.client.HTTPConnection', Connection):
                ForgejoDestination(LocalForgejoAPI('http://localhost', token_file), 'mirror').ensure_repository('project', 'alice/project')
            identity = next(auth for method, path, auth in calls if path == '/api/v1/user')
            create = next(auth for method, path, auth in calls if method == 'POST')
            self.assertEqual(identity, create)
