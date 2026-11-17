"""Conditional publication of a Forgejo manifest through GitHub Contents API.

The file SHA is a Git blob identity, not a lock on unrelated branch changes.
Callers must separately serialize and authenticate migration transactions.
"""
import base64
import binascii
import hashlib
import re
from urllib.parse import quote


class GitHubConfigCAS:
    def __init__(self, api, owner, repo, branch, path):
        if not all(isinstance(x, str) and x for x in (owner, repo, branch, path)):
            raise ValueError('invalid config location')
        if not all(re.fullmatch(r'[A-Za-z0-9_.-]+', x) for x in (owner, repo)):
            raise ValueError('invalid repository')
        if path not in ('config-forgejo.json', 'config.json') or branch != 'main':
            raise ValueError('unsupported config location')
        self.api = api
        self.url = f'https://api.github.com/repos/{quote(owner, safe="")}/{quote(repo, safe="")}/contents/{path}'
        self.branch = branch

    def _read(self):
        result = self.api.request('GET', self.url + '?ref=' + quote(self.branch, safe=''))
        if not isinstance(result, dict) or result.get('type') != 'file' or result.get('encoding') != 'base64':
            raise ValueError('invalid config response')
        content, sha = result.get('content'), result.get('sha')
        if not isinstance(content, str) or not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise ValueError('invalid config response')
        try:
            data = base64.b64decode(content.replace('\n', ''), validate=True)
        except (ValueError, binascii.Error):
            raise ValueError('invalid config encoding') from None
        actual = hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest()
        if actual != sha:
            raise ValueError('config blob mismatch')
        return data, sha

    def read(self):
        return self._read()[0]

    def replace(self, before, after):
        if not isinstance(before, bytes) or not isinstance(after, bytes) or before == after:
            raise ValueError('invalid config replacement')
        current, sha = self._read()
        if current != before:
            raise ValueError('configuration changed')
        self.api.request('PUT', self.url, {'message': 'Update Forgejo migration configuration',
                                            'branch': self.branch, 'sha': sha,
                                            'content': base64.b64encode(after).decode('ascii')})
        if self.read() != after:
            raise ValueError('config publication readback mismatch')
