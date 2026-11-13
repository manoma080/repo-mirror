import io
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from repo_mirror.forgejo_destination_adapter import ForgejoDestinationAdapter
from repo_mirror.repository import mirror_releases


class Client:
    origin = 'http://127.0.0.1:3300'
    base = '/api/v1/repos/team/project'

    def __init__(self, existing):
        self.existing = existing
        self.calls = []

    def list_releases(self):
        return [self.existing] if self.existing else []

    def list_assets(self, release):
        self.calls.append(('assets', release))
        return []

    def _json(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == 'GET' and '/tags/' in path and self.existing is None:
            raise HTTPError(path, 404, 'missing', {}, io.BytesIO())
        if method == 'POST':
            return dict(body, id=7)
        return self.existing or {'id': 7, 'tag_name': 'v1'}


class EmptyBodyGuardTests(unittest.TestCase):
    def test_empty_source_never_writes_release_or_touches_assets(self):
        for body in ('', None):
            for destination_body in (None, '', 'existing notes', '[repo-mirror:pending:v3]bad'):
                for exists in (False, True):
                    with self.subTest(body=body, destination_body=destination_body, exists=exists):
                        release = {'id': 1, 'tag_name': 'v1', 'name': 'name', 'body': body,
                                   'draft': False, 'prerelease': False}
                        existing = {'id': 7, 'tag_name': 'v1', 'name': 'name', 'body': destination_body,
                                    'draft': False, 'prerelease': False} if exists else None
                        client = Client(existing)
                        adapter = ForgejoDestinationAdapter('team/project', 'token', client=client)
                        source_calls = []
                        class Source:
                            def pages(self, url):
                                source_calls.append(url)
                                return [] if url.endswith('/assets') else [release]
                            def request(self, method, url):
                                return release
                        with patch.dict(os.environ, {'MIRROR_TIMESTAMP_SECRET': 'secret'}):
                            with self.assertRaisesRegex(Exception, 'empty.*body'):
                                mirror_releases({'type': 'github', 'owner': 'alice'}, 'demo', 'team',
                                                Source(), adapter, 'https://source/repo', adapter.target,
                                                'marker', initializing=True)
                        self.assertFalse(any(call[0] != 'GET' for call in client.calls), client.calls)
                        self.assertFalse(any(url.endswith('/assets') for url in source_calls))
