import unittest

from test_forgejo_destination_adapter import make_adapter


class ForgejoSpaceBodyTests(unittest.TestCase):
    def test_release_reads_preserve_raw_space_and_marker_bytes(self):
        for value in ('', ' ', '  ', ' \n[repo-mirror:pending:v3]\n '):
            with self.subTest(value=value):
                adapter, client = make_adapter()
                client.release['body'] = value
                client.releases = [client.release]
                self.assertEqual(adapter.pages(adapter.target + '/releases')[0]['body'], value)
                self.assertEqual(adapter.request('GET', adapter.target + '/releases/7')['body'], value)

    def test_empty_payloads_are_written_as_one_space(self):
        for value in (None, '', ' '):
            with self.subTest(value=value):
                adapter, client = make_adapter()
                client.release['body'] = 'old'
                adapter.request('PATCH', adapter.target + '/releases/7', {'body': value})
                self.assertEqual(client.calls[-2][2]['body'], ' ')
                self.assertEqual(client.release['body'], ' ')

    def test_other_whitespace_is_preserved(self):
        for value in ('  ', '\t', '\n'):
            with self.subTest(value=value):
                adapter, client = make_adapter()
                adapter.request('PATCH', adapter.target + '/releases/7', {'body': value})
                self.assertEqual(client.release['body'], value)

    def test_empty_write_requires_exact_space_on_readback(self):
        for persisted in (None, '', '  '):
            with self.subTest(persisted=persisted):
                adapter, client = make_adapter()
                original = client._json
                def ignore(method, path, body=None):
                    result = original(method, path, body)
                    if method == 'GET':
                        result['body'] = persisted
                    return result
                client._json = ignore
                with self.assertRaisesRegex(Exception, 'metadata not persisted'):
                    adapter.request('PATCH', adapter.target + '/releases/7', {'body': ''})
