import tempfile
import unittest
from pathlib import Path

from repo_mirror.migration_intent import IntentStore


class MigrationIntentTests(unittest.TestCase):
    def test_intent_survives_restart_and_rejects_changed_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'intent.json'
            binding = {'source_id': 42, 'destination_id': 9, 'old': 'old/repo', 'new': 'new/repo'}
            store = IntentStore(path, 's' * 32)
            store.begin(binding)
            self.assertEqual(IntentStore(path, 's' * 32).load(), {'binding': binding, 'phase': 'prepared'})
            with self.assertRaisesRegex(ValueError, 'binding'):
                store.begin({**binding, 'destination_id': 10})
            self.assertEqual(store.load()['binding'], binding)

    def test_symlink_intent_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'intent.json'
            target = Path(directory) / 'target'
            IntentStore(target, 's' * 32).begin({'source_id': 42})
            path.symlink_to(target)
            with self.assertRaises((OSError, ValueError)):
                IntentStore(path, 's' * 32).load()

    def test_intent_rejects_tampering_and_out_of_order_transitions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'intent.json'
            store = IntentStore(path, 's' * 32)
            store.begin({'source_id': 42})
            with self.assertRaisesRegex(ValueError, 'phase'):
                store.advance('prepared', 'renamed')
            store.advance('prepared', 'synced')
            self.assertEqual(IntentStore(path, 's' * 32).load()['phase'], 'synced')
            path.write_text(path.read_text().replace('synced', 'renamed'))
            with self.assertRaisesRegex(ValueError, 'intent'):
                store.load()
