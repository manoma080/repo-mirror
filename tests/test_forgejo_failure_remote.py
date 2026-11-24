import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repo_mirror import forgejo_failure_remote as sync
from repo_mirror.journal import FailureJournal


FIRST = 'a' * 64 + '.json'
SECOND = 'dispatch-' + 'b' * 64 + '.json'
KEY = 'c' * 64 + ':' + 'd' * 64
EMPTY = {'version': 1, 'failures': []}
FAILED = {'version': 1, 'failures': [KEY]}


def document(journals):
    return (json.dumps({'version': 1, 'journals': journals}, sort_keys=True,
                       indent=2, ensure_ascii=True) + '\n').encode()


class Remote:
    def __init__(self, value=None):
        self.value = value
        self.writes = []
        self.failure: str | None = None

    def read(self):
        return self.value

    def replace(self, before, after):
        if before != self.value:
            raise RuntimeError('remote changed')
        self.writes.append((before, after))
        if self.failure == 'before':
            raise OSError('offline')
        if self.failure == 'ignore':
            return
        self.value = after
        if self.failure == 'after':
            raise OSError('lost response')


class FailureRemoteTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)

    def put(self, name, payload):
        (self.directory / name).write_text(json.dumps(payload), encoding='utf-8')

    def test_snapshot_excludes_other_state_and_preserves_version_two(self):
        legacy = {'version': 2, 'failures': [KEY], 'intents': {'1' * 64: {
            'intent': {'binding': {'some': 'value'}, 'phase': 'prepared'}, 'signature': '2' * 64}}}
        self.put(SECOND, EMPTY)
        self.put(FIRST, legacy)
        self.put('migrations.json', {'unrelated': True})
        self.put(FIRST + '.lock', {})
        self.assertEqual(sync.snapshot(self.directory), document({FIRST: legacy, SECOND: EMPTY}))

    def test_fresh_install_restores_before_work_and_publishes_clear(self):
        remote = Remote(document({FIRST: FAILED}))
        with sync.session(self.directory, remote):
            journal = FailureJournal(self.directory / FIRST)
            self.assertEqual(journal.failures, {KEY})
            journal.set(KEY, False)
        self.assertEqual(remote.value, document({FIRST: EMPTY}))
        with sync.session(self.directory, remote):
            pass
        self.assertEqual(len(remote.writes), 1)

    def test_publication_failure_and_lost_response_are_recoverable(self):
        for failure in ('before', 'after'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                remote = Remote(document({FIRST: FAILED}))
                remote.failure = failure
                with self.assertRaises(OSError), sync.session(path, remote):
                    (path / FIRST).write_bytes(json.dumps(EMPTY).encode())
                local = (path / FIRST).read_bytes()
                remote.failure = None
                with sync.session(path, remote):
                    self.assertEqual(remote.value, document({FIRST: EMPTY}))
                    self.assertEqual((path / FIRST).read_bytes(), local)
                self.assertEqual(len(remote.writes), 2 if failure == 'before' else 1)

    def test_canceled_run_is_synced_before_next_run(self):
        remote = Remote()
        with self.assertRaises(KeyboardInterrupt), sync.session(self.directory, remote):
            self.put(FIRST, FAILED)
            raise KeyboardInterrupt
        self.assertIsNone(remote.value)
        with sync.session(self.directory, remote):
            self.assertEqual(remote.value, document({FIRST: FAILED}))

    def test_interrupted_restore_resumes_only_from_same_remote_base(self):
        remote = Remote(document({FIRST: FAILED, SECOND: EMPTY}))
        write = sync._atomic_write

        def interrupted(directory, name, raw, *, create=False):
            if name == SECOND:
                raise KeyboardInterrupt
            write(directory, name, raw, create=create)

        with patch.object(sync, '_atomic_write', interrupted), self.assertRaises(KeyboardInterrupt):
            with sync.session(self.directory, remote):
                self.fail('workers ran before restore completed')
        base = remote.value
        remote.value = document({FIRST: FAILED, SECOND: FAILED})
        with self.assertRaises(ValueError), sync.session(self.directory, remote):
            self.fail('workers ran against a replaced restore base')
        remote.value = base
        with sync.session(self.directory, remote):
            self.assertEqual(sync.snapshot(self.directory), base)
        self.assertFalse(remote.writes)

    def test_unconfirmed_replace_does_not_clear_the_fence(self):
        remote = Remote(document({FIRST: FAILED}))
        remote.failure = 'ignore'
        with self.assertRaises(ValueError), sync.session(self.directory, remote):
            self.put(FIRST, EMPTY)
        remote.failure = None
        with sync.session(self.directory, remote):
            self.assertEqual(remote.value, document({FIRST: EMPTY}))

    def test_missing_local_journal_after_admission_does_not_clear_remote(self):
        original = document({FIRST: FAILED})
        remote = Remote(original)
        with self.assertRaises(ValueError), sync.session(self.directory, remote):
            (self.directory / FIRST).unlink()
        with self.assertRaises(ValueError), sync.session(self.directory, remote):
            self.fail('workers ran after journal loss')
        self.assertEqual(remote.value, original)
        self.assertFalse(remote.writes)

    def test_divergence_after_cancellation_preserves_exact_local_bytes(self):
        remote = Remote(document({FIRST: FAILED}))
        with self.assertRaises(KeyboardInterrupt), sync.session(self.directory, remote):
            self.put(FIRST, EMPTY)
            raise KeyboardInterrupt
        local = (self.directory / FIRST).read_bytes()
        remote.value = document({SECOND: FAILED})
        with self.assertRaises(ValueError), sync.session(self.directory, remote):
            self.fail('workers must not start on divergence')
        self.assertEqual((self.directory / FIRST).read_bytes(), local)
        self.assertFalse(remote.writes)

    def test_unfenced_local_difference_is_not_adopted(self):
        self.put(FIRST, FAILED)
        remote = Remote()
        with self.assertRaises(ValueError), sync.session(self.directory, remote):
            self.fail('workers must not start with stale local state')
        self.assertFalse(remote.writes)

    def test_nested_legacy_intent_type_difference_is_not_adopted(self):
        def legacy(value):
            return {'version': 2, 'failures': [], 'intents': {'1' * 64: {
                'intent': {'binding': {'number': value}, 'phase': 'prepared'},
                'signature': '2' * 64}}}

        self.put(FIRST, legacy(True))
        remote = Remote(document({FIRST: legacy(1)}))
        with self.assertRaises(ValueError), sync.session(self.directory, remote):
            self.fail('workers must not start with differing legacy intent bytes')
        self.assertFalse(remote.writes)

    def test_rejects_unsafe_local_file_and_remote_filename(self):
        self.put('migrations.json', FAILED)
        (self.directory / FIRST).symlink_to('migrations.json')
        with self.assertRaises((ValueError, OSError)):
            sync.snapshot(self.directory)
        (self.directory / FIRST).unlink()
        with self.assertRaises(ValueError), sync.session(self.directory, Remote(document({'../migrations.json': EMPTY}))):
            self.fail('invalid remote must not be restored')
        self.assertEqual(json.loads((self.directory / 'migrations.json').read_bytes()), FAILED)

    def test_rejects_invalid_and_oversized_journals(self):
        for payload in (b'{"version":1,"version":1,"failures":[]}',
                        b'{"version":true,"failures":[]}',
                        b' ' * (sync.MAX_BYTES + 1)):
            with self.subTest(length=len(payload)):
                (self.directory / FIRST).write_bytes(payload)
                with self.assertRaises(ValueError):
                    sync.snapshot(self.directory)


if __name__ == '__main__':
    unittest.main()
