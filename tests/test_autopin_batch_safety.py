import copy
import unittest
from urllib.error import HTTPError

from repo_mirror import adhoc
from test_adhoc_autopin import config


class BatchSource:
    def __init__(self, dst, bad_clone=False):
        self.dst = dst
        self.bad_clone = bad_clone

    def request(self, method, url, *args):
        name = url.rsplit('/', 1)[-1]
        changed = self.dst.final_checks >= 2 and name == 'one'
        return {'id': 99 if changed and not self.bad_clone else (1 if name == 'one' else 2),
                'clone_url': 'https://github.com/alice/' + ('evil' if changed and self.bad_clone else name) + '.git'}


class BatchDestination:
    def __init__(self):
        self.repos = {}
        self.final_checks = 0

    def request(self, method, url, payload=None):
        if url.endswith('/user'):
            return {'login': 'bot'}
        if url.endswith('/actions/permissions'):
            return {'enabled': False} if method == 'GET' else None
        if method == 'POST':
            name = payload['name']
            self.repos[name] = {'id': len(self.repos) + 10, 'full_name': 'bot/' + name, 'description': payload['description']}
            return self.repos[name].copy()
        name = url.rsplit('/', 1)[-1]
        if name not in self.repos:
            raise HTTPError(url, 404, 'missing', {}, None)
        if len(self.repos) == 2:
            self.final_checks += 1
        return self.repos[name].copy()


class BatchSafetyTests(unittest.TestCase):
    def test_first_source_clone_url_rechecked_after_last_destination(self):
        cfg = config()
        before = copy.deepcopy(cfg)
        dst = BatchDestination()
        src = BatchSource(dst, bad_clone=True)
        with self.assertRaisesRegex(ValueError, 'source clone URL'):
            adhoc.add_to_config_pinned(cfg, 'https://github.com/alice/one,https://github.com/alice/two', src, src, dst, owner='bot', account_type='personal')
        self.assertEqual(cfg, before)

    def test_first_source_rechecked_after_last_destination(self):
        cfg = config()
        before = copy.deepcopy(cfg)
        dst = BatchDestination()
        src = BatchSource(dst)
        with self.assertRaisesRegex(ValueError, 'source repository id changed'):
            adhoc.add_to_config_pinned(cfg, 'https://github.com/alice/one,https://github.com/alice/two', src, src, dst, owner='bot', account_type='personal')
        self.assertEqual(cfg, before)
