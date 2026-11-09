import unittest
from unittest.mock import Mock

import repo_mirror.git_sync as git_sync


def create_archive(*args):
    if not hasattr(git_sync, "create_archive"):
        raise AssertionError("archive creation is not implemented")
    return git_sync.create_archive(*args)


class ArchiveCreationTests(unittest.TestCase):
    def test_creates_private_archive_and_disables_actions_before_return(self):
        api = Mock()
        api.request.side_effect = [
            {"full_name": "owner/mirror-wipe-20261109T120000Z", "private": True},
            None,
            {"full_name": "owner/mirror-wipe-20261109T120000Z", "private": True},
            {"enabled": False},
        ]
        url = create_archive(api, "owner", "mirror", "organization", "20261109T120000Z")
        self.assertEqual(url, "https://github.com/owner/mirror-wipe-20261109T120000Z.git")
        self.assertEqual(api.request.call_args_list[0].args[2]["private"], True)
        self.assertEqual(api.request.call_args_list[1].args[0], "PUT")
        self.assertEqual(api.request.call_args_list[1].args[2], {"enabled": False})

    def test_rejects_mismatched_archive_identity_before_disabling_actions(self):
        api = Mock()
        api.request.return_value = {"full_name": "other/archive", "private": True}
        with self.assertRaises(RuntimeError):
            create_archive(api, "owner", "mirror", "organization", "20261109T120000Z")
        self.assertEqual(api.request.call_count, 1)

    def test_fails_closed_if_actions_still_enabled(self):
        api = Mock()
        api.request.side_effect = [
            {"full_name": "owner/mirror-wipe-20261109T120000Z", "private": True},
            None,
            {"full_name": "owner/mirror-wipe-20261109T120000Z", "private": True},
            {"enabled": True},
        ]
        with self.assertRaises(RuntimeError):
            create_archive(api, "owner", "mirror", "organization", "20261109T120000Z")
