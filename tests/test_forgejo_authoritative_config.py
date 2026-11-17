import base64
import hashlib
import unittest

from repo_mirror.forgejo_authoritative_config import GitHubConfigCAS


def blob(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


class ContentsAPI:
    def __init__(self, content):
        self.content = content
        self.writes = 0
        self.race = False

    def request(self, method, url, data=None):
        if method == 'GET':
            return {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(self.content).decode(), 'sha': blob(self.content)}
        if self.race:
            self.content = b'external edit'
        if data['sha'] != blob(self.content):
            raise ValueError('conflict')
        self.content = base64.b64decode(data['content'])
        self.writes += 1
        return {'content': {'sha': blob(self.content)}}


class AuthoritativeConfigTests(unittest.TestCase):
    def test_conditional_publication_and_readback(self):
        api = ContentsAPI(b'old')
        cas = GitHubConfigCAS(api, 'owner', 'repo', 'main', 'config-forgejo.json')
        self.assertEqual(cas.read(), b'old')
        cas.replace(b'old', b'new')
        self.assertEqual(cas.read(), b'new')
        self.assertEqual(api.writes, 1)

    def test_rejects_edit_between_read_and_write(self):
        api = ContentsAPI(b'old')
        cas = GitHubConfigCAS(api, 'owner', 'repo', 'main', 'config-forgejo.json')
        api.race = True
        with self.assertRaises(ValueError):
            cas.replace(b'old', b'new')
        self.assertEqual(api.content, b'external edit')
        self.assertEqual(api.writes, 0)

    def test_rejects_wrong_expected_bytes(self):
        api = ContentsAPI(b'changed')
        cas = GitHubConfigCAS(api, 'owner', 'repo', 'main', 'config-forgejo.json')
        with self.assertRaises(ValueError):
            cas.replace(b'old', b'new')
        self.assertEqual(api.writes, 0)
