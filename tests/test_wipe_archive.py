import unittest
from unittest.mock import Mock

import repo_mirror.git_sync as git_sync


def create_archive(*args):
    if not hasattr(git_sync, "create_archive"):
        raise AssertionError("archive creation is not implemented")
    return git_sync.create_archive(*args)


class ArchiveCreationTests(unittest.TestCase):
    def test_archive_default_is_patched_and_verified(self):
        api = Mock()
        api.request.side_effect = [{"default_branch": "bisect/oot-1"}, {}, {"default_branch": "nx2"}]
        git_sync.set_archive_default(api, "https://api.github.com/repos/owner/archive", "refs/heads/nx2")
        self.assertEqual(api.request.call_args_list[1].args, (
            "PATCH", "https://api.github.com/repos/owner/archive", {"default_branch": "nx2"}
        ))
        self.assertEqual(api.request.call_args_list[-1].args[0], "GET")

    def test_archive_default_already_correct_does_not_patch(self):
        api = Mock()
        api.request.return_value = {"default_branch": "nx2"}
        git_sync.set_archive_default(api, "target", "refs/heads/nx2")
        api.request.assert_called_once_with("GET", "target")

    def test_archive_default_missing_fails_closed(self):
        for value in (None, "", {}):
            with self.subTest(value=value):
                api = Mock()
                api.request.return_value = {"default_branch": value}
                with self.assertRaisesRegex(RuntimeError, "missing"):
                    git_sync.set_archive_default(api, "target", "refs/heads/nx2")
                api.request.assert_called_once_with("GET", "target")
        with self.assertRaisesRegex(RuntimeError, "missing"):
            git_sync.set_archive_default(Mock(), "target", "refs/heads/")

    def test_archive_default_readback_mismatch_fails(self):
        api = Mock()
        api.request.side_effect = [{"default_branch": "other"}, {}, {"default_branch": "other"}]
        with self.assertRaisesRegex(RuntimeError, "verified"):
            git_sync.set_archive_default(api, "target", "refs/heads/nx2")

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


    def test_long_mirror_names_produce_distinct_archives_at_same_timestamp(self):
        class API:
            def __init__(self):
                self.names = []

            def request(self, method, url, data=None):
                if method == "POST":
                    self.names.append(data["name"])
                    return {"full_name": "owner/" + data["name"], "private": True}
                if url.endswith("/actions/permissions"):
                    return {"enabled": False}
                return {"full_name": "owner/" + url.rsplit("/", 1)[-1], "private": True}

        api = API()
        for name in ("a" * 90 + "x", "a" * 90 + "y"):
            create_archive(api, "owner", name, "organization", "20261109T120000Z")
        self.assertEqual(len(set(api.names)), 2)
        self.assertTrue(all(len(name) <= 100 for name in api.names))

    def test_archive_name_helper_normal_hash_fallback_and_validation(self):
        self.assertEqual(
            git_sync.archive_name("mirror-Alice-alpha", "20261109T120000Z"),
            "mirror-Alice-alpha-wipe-20261109T120000Z",
        )
        long_a = git_sync.archive_name("a" * 90 + "x", "20261109T120000Z")
        long_b = git_sync.archive_name("a" * 90 + "y", "20261109T120000Z")
        self.assertEqual(len(long_a), 100)
        self.assertEqual(len(long_b), 100)
        self.assertNotEqual(long_a, long_b)
        self.assertTrue(long_a.endswith("-wipe-20261109T120000Z"))
        for bad_name, bad_ts in (("", "20261109T120000Z"), ("..", "20261109T120000Z"), ("mirror", "bad-ts")):
            with self.assertRaisesRegex(ValueError, "invalid archive identity"):
                git_sync.archive_name(bad_name, bad_ts)
