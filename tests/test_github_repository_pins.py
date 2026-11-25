import unittest
from unittest.mock import Mock, patch

from repo_mirror import repository
from repo_mirror.sources import source_marker, validate_source


class GitHubRepositoryPinsTest(unittest.TestCase):
    def source(self):
        return {"type": "github", "base_url": "https://github.com", "owner": "alice", "name": "demo",
                "mirror": {"source_id": 11, "destination_id": 22}}

    def test_destination_replaced_during_git_never_enters_release_sync(self):
        source = self.source()
        src, dst = Mock(), Mock()
        src.request.return_value = {"id": 11, "clone_url": "https://github.com/alice/demo.git", "default_branch": "main"}
        original = {"id": 22, "description": source_marker(source, "demo"), "default_branch": "main"}
        replaced = {"id": 23, "description": source_marker(source, "demo"), "default_branch": "main"}
        dst.request.side_effect = [original, None, replaced]
        with patch("repo_mirror.repository.mirror_git"), patch("repo_mirror.repository.mirror_releases") as releases:
            with self.assertRaisesRegex(ValueError, "destination.*id"):
                repository.mirror_repository(source, "demo", "dest", src, dst, "token")
        releases.assert_not_called()

    def test_source_id_mismatch_stops_before_destination_writes(self):
        source = self.source()
        src, dst = Mock(), Mock()
        src.request.return_value = {"id": 12, "clone_url": "https://github.com/alice/demo.git"}
        dst.request.return_value = {"id": 22, "description": source_marker(source, "demo")}
        with patch("repo_mirror.repository.mirror_git") as mirror, patch("repo_mirror.repository.mirror_releases"):
            with self.assertRaisesRegex(ValueError, "source.*id"):
                repository.mirror_repository(source, "demo", "dest", src, dst, "token")
        self.assertTrue(all(call.args[0] == "GET" for call in dst.request.call_args_list))
        mirror.assert_not_called()

    def test_config_accepts_positive_integer_pins_and_rejects_invalid_values(self):
        validate_source(self.source())
        for key in ("source_id", "destination_id"):
            for value in (True, False, 0, -1, 1.0, "1", None):
                with self.subTest(key=key, value=value):
                    source = self.source()
                    source["mirror"][key] = value
                    with self.assertRaises(ValueError):
                        validate_source(source)

    def test_runtime_rejects_invalid_pins_even_when_equal_to_api_id(self):
        for key, value in (("source_id", True), ("destination_id", True)):
            with self.subTest(key=key):
                source = self.source()
                source["mirror"][key] = value
                src, dst = Mock(), Mock()
                src.request.return_value = {"id": 1 if key == "source_id" else 11, "clone_url": "https://github.com/alice/demo.git"}
                dst.request.return_value = {"id": 1 if key == "destination_id" else 22, "description": source_marker(source, "demo")}
                with patch("repo_mirror.repository.mirror_git") as mirror, patch("repo_mirror.repository.mirror_releases"):
                    with self.assertRaises(ValueError):
                        repository.mirror_repository(source, "demo", "dest", src, dst, "token")
                self.assertTrue(all(c.args[0] == "GET" for c in dst.request.call_args_list))
                mirror.assert_not_called()

    def test_matching_pins_allow_normal_runtime(self):
        source = self.source()
        src, dst = Mock(), Mock()
        src.request.return_value = {"id": 11, "clone_url": "https://github.com/alice/demo.git", "default_branch": "main"}
        dst.request.return_value = {"id": 22, "description": source_marker(source, "demo"), "default_branch": "main"}
        with patch("repo_mirror.repository.mirror_git") as mirror, patch("repo_mirror.repository.mirror_releases"):
            repository.mirror_repository(source, "demo", "dest", src, dst, "token")
        mirror.assert_called_once()
        self.assertIn("PUT", [c.args[0] for c in dst.request.call_args_list])

    def test_pins_do_not_bypass_clone_url_guard(self):
        source = self.source()
        src, dst = Mock(), Mock()
        src.request.return_value = {"id": 11, "clone_url": "https://github.com/attacker/demo.git"}
        dst.request.return_value = {"id": 22, "description": source_marker(source, "demo")}
        with patch("repo_mirror.repository.mirror_git") as mirror:
            with self.assertRaisesRegex(ValueError, "clone URL"):
                repository.mirror_repository(source, "demo", "dest", src, dst, "token")
        self.assertTrue(all(c.args[0] == "GET" for c in dst.request.call_args_list))
        mirror.assert_not_called()

    def test_destination_pin_rejects_missing_or_wrong_id_before_writes(self):
        from urllib.error import HTTPError
        for returned in (None, {}, {"id": 23}, {"id": True}):
            with self.subTest(returned=returned):
                source = self.source()
                src, dst = Mock(), Mock()
                src.request.return_value = {"id": 11, "clone_url": "https://github.com/alice/demo.git"}
                repo = None if returned is None else dict(returned, description=source_marker(source, "demo"))
                if repo is None:
                    dst.request.side_effect = HTTPError('url', 404, 'missing', {}, None)
                else:
                    dst.request.return_value = repo
                with patch("repo_mirror.repository.mirror_git") as mirror, patch("repo_mirror.repository.mirror_releases"):
                    with self.assertRaisesRegex(ValueError, "destination.*id"):
                        repository.mirror_repository(source, "demo", "dest", src, dst, "token")
                self.assertTrue(all(call.args[0] == "GET" for call in dst.request.call_args_list))
                mirror.assert_not_called()
