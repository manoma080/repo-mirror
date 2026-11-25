import tempfile
from pathlib import Path
from unittest.mock import patch

import unittest

assert_raises = unittest.TestCase().assertRaisesRegex

from repo_mirror.journal import FailureJournal
from repo_mirror.repository import mirror_releases
from repo_mirror.sources import source_marker
from test_reconcile_assets import Source, Destination, SOURCE


class ReleaseIdentityTests(unittest.TestCase):
  @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'fixture-signing-secret-0123456789abcdef'})
  def test_pinned_retry_survives_source_rename_and_clears(self):
    old = {**SOURCE, 'mirror': {'source_id': 12, 'destination_id': 34}}
    new = {**old, 'owner': 'bob'}
    dest = Destination()
    with tempfile.TemporaryDirectory() as directory:
        journal = FailureJournal(Path(directory) / 'journal')
        with assert_raises(RuntimeError, 'release asset uploads failed'):
            mirror_releases(old, 'demo', 'dest', Source(), dest, 'https://api.github.com/repos/alice/demo',
                            'https://api.github.com/repos/dest/mirror', source_marker(old, 'demo'),
                            initializing=False, journal=journal)
        stable = journal.id_key(old['base_url'], 12, 'https://api.github.com', 34, 'v1', 'changed')
        assert stable in journal.failures
        dest.fail = False
        mirror_releases(new, 'demo', 'dest', Source(), dest, 'https://api.github.com/repos/bob/demo',
                        'https://api.github.com/repos/dest/mirror', source_marker(old, 'demo'),
                        initializing=False, journal=journal)
        assert stable not in journal.failures

  @patch.dict('os.environ', {'MIRROR_TIMESTAMP_SECRET': 'fixture-signing-secret-0123456789abcdef'})
  def test_legacy_record_migrates_and_clears_after_success(self):
    old = {**SOURCE, 'owner': 'old'}
    new = {**SOURCE, 'owner': 'new', 'mirror': {'source_id': 12, 'destination_id': 34}}
    dest = Destination()
    dest.fail = False
    with tempfile.TemporaryDirectory() as directory:
        journal = FailureJournal(Path(directory) / 'journal')
        legacy = journal.key(old, 'demo', 'dest', 'v1', 'changed')
        journal.set(legacy, True)
        mirror_releases(new, 'demo', 'dest', Source(), dest, 'https://api.github.com/repos/new/demo',
                        'https://api.github.com/repos/dest/mirror', source_marker(old, 'demo'),
                        initializing=False, journal=journal, journal_source=old, journal_name='demo')
        self.assertEqual(journal.failures, set())

  def test_selection_scopes_stable_keys_to_destination_origin_and_id(self):
    from repo_mirror import repository
    source = {**SOURCE, 'mirror': {'source_id': 12, 'destination_id': 34}}
    releases = [{'id': 1, 'tag_name': 'v3'}, {'id': 2, 'tag_name': 'v2'}, {'id': 3, 'tag_name': 'v1'}]
    class Src:
        def pages(self, url):
            return releases
        def request(self, method, url):
            from urllib.error import HTTPError
            raise HTTPError(url, 404, '', {}, None)
    class Dst:
        def pages(self, url):
            return releases
    with tempfile.TemporaryDirectory() as directory:
        journal = FailureJournal(Path(directory) / 'journal')
        foreign = journal.id_key(source['base_url'], 12, 'https://other.example', 34, 'v2', 'a')
        journal.set(foreign, True)
        with patch.object(repository, 'select_existing_releases', return_value=[]) as select:
            mirror_releases(source, 'demo', 'dest', Src(), Dst(), 'https://source/repo',
                            'https://api.github.com/repos/dest/mirror', 'marker', initializing=False, journal=journal)
            self.assertEqual(select.call_args.args[2], set())
        own = journal.id_key(source['base_url'], 12, 'https://api.github.com', 34, 'v2', 'a')
        journal.set(own, True)
        with patch.object(repository, 'select_existing_releases', return_value=[]) as select:
            mirror_releases(source, 'demo', 'dest', Src(), Dst(), 'https://source/repo',
                            'https://api.github.com/repos/dest/mirror', 'marker', initializing=False, journal=journal)
            self.assertEqual(select.call_args.args[2], {'v2'})
