import json
import multiprocessing
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from repo_mirror.journal import FailureJournal


def write_failure(path, n):
    FailureJournal(path).set(f'{n:064x}:' + 'a' * 64, True)


class JournalMigrationUpgradeTests(unittest.TestCase):
    def test_concurrent_threads_and_processes_preserve_all_failures(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / 'journal')
            journal = FailureJournal(path)
            journal.intent_store('s' * 32, 'id').begin({'source_id': 1})
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda n: write_failure(path, n), range(20)))
            processes = [multiprocessing.Process(target=write_failure, args=(path, n)) for n in range(20, 30)]
            for process in processes:
                process.start()
            for process in processes:
                process.join()
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(len(journal.failures), 30)
            self.assertEqual(journal.intent_store('s' * 32, 'id').load()['phase'], 'prepared')

    def test_id_keys_scope_both_origins_and_legacy_retry_migrates(self):
        source = {'type': 'github', 'base_url': 'https://source.example', 'owner': 'old'}
        with tempfile.TemporaryDirectory() as d:
            journal = FailureJournal(Path(d) / 'failures.json')
            legacy = journal.key(source, 'repo', 'dest', 'v1', 'asset')
            new = journal.id_key('https://source.example', 12, 'https://dest.example', 34, 'v1', 'asset')
            other = journal.id_key('https://source.example', 12, 'https://other.example', 34, 'v1', 'asset')
            self.assertNotEqual(new, other)
            journal.set(legacy, True)
            self.assertTrue(journal.migrate_failure(legacy, new))
            self.assertEqual(journal.failures, {new})
            self.assertFalse(journal.migrate_failure(legacy, new))

    def test_intent_and_legacy_failures_survive_each_others_updates(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'failures.json'
            legacy = 'a' * 64 + ':' + 'b' * 64
            path.write_text(json.dumps({'version': 1, 'failures': [legacy]}))
            journal = FailureJournal(path)
            intent = journal.intent_store('s' * 32, 'migration-1')
            binding = {'source_id': 42, 'destination_id': 7}
            intent.begin(binding)
            journal.set('c' * 64 + ':' + 'd' * 64, True)
            intent.advance('prepared', 'synced')
            self.assertEqual(FailureJournal(path).failures, {legacy, 'c' * 64 + ':' + 'd' * 64})
            self.assertEqual(FailureJournal(path).intent_store('s' * 32, 'migration-1').load(), {'binding': binding, 'phase': 'synced'})
            with self.assertRaises(ValueError):
                FailureJournal(path).intent_store('x' * 32, 'migration-1').load()
