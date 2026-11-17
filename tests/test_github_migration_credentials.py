"""The migration must use the mirror credential for destinations, not workflow token."""
import unittest

from repo_mirror.github_migration import transaction_from_environment

ENV = {
    'MIRROR_MANAGEMENT_REPOSITORY': 'manoma080/repo-mirror',
    'MIRROR_MANAGEMENT_TOKEN': 'management-token',
    'MIRROR_TIMESTAMP_SECRET': 'k' * 40,
}


class _Recorder:
    def __init__(self, repos):
        self.repos = repos
        self.requests = []

    def request(self, method, url, payload=None):
        self.requests.append((method, url, payload))
        name = url.rstrip('/').rsplit('/', 1)[-1]
        if name in self.repos:
            return self.repos[name]
        raise AssertionError('unexpected request: ' + url)


class TransactionWiringTests(unittest.TestCase):
    def test_destination_lookup_uses_the_destination_credential(self):
        management = _Recorder({})
        destination = _Recorder({'mirror-old-repo': {'id': 22, 'full_name': 'owner/mirror-old-repo'}})
        transaction = transaction_from_environment(
            ENV, 'config.json', 'owner', api=management, destination_api=destination)
        self.assertIsNotNone(transaction)
        self.assertEqual(transaction.repository.get('mirror-old-repo')['id'], 22)
        self.assertEqual(management.requests, [])

    def test_management_api_is_used_when_no_destination_credential_is_supplied(self):
        management = _Recorder({'mirror-old-repo': {'id': 22, 'full_name': 'owner/mirror-old-repo'}})
        transaction = transaction_from_environment(ENV, 'config.json', 'owner', api=management)
        self.assertEqual(transaction.repository.get('mirror-old-repo')['id'], 22)

    def test_no_transaction_without_the_environment_contract(self):
        for missing in ('MIRROR_MANAGEMENT_TOKEN', 'MIRROR_TIMESTAMP_SECRET'):
            environ = {key: value for key, value in ENV.items() if key != missing}
            with self.subTest(missing=missing):
                self.assertIsNone(transaction_from_environment(environ, 'config.json', 'owner'))


if __name__ == '__main__':
    unittest.main()
