import unittest

import repo_mirror.git_sync as git_sync


class WipeDetectionTests(unittest.TestCase):
    def test_replaced_paths_with_same_count_do_not_trigger(self):
        self.assertFalse(git_sync.is_wipe({b"a", b"b", b"c", b"d"}, {b"e", b"f", b"g", b"h"}, {1}, set()))

    def test_both_losses_at_exact_threshold_trigger(self):
        self.assertTrue(hasattr(git_sync, "is_wipe"), "wipe detection is not implemented")
        self.assertTrue(git_sync.is_wipe({b"a", b"b", b"c", b"d"}, {b"a"}, {1, 2, 3, 4}, {1}))

    def test_net_file_count_boundaries_and_growth(self):
        old_files = set(range(8))
        for new_count, expected in ((9, False), (3, False), (2, True), (1, True), (0, True)):
            with self.subTest(new_count=new_count):
                new_files = set(range(100, 100 + new_count))
                self.assertEqual(git_sync.is_wipe(old_files, new_files, {1}, set()), expected)
