import unittest

from repo_mirror import adhoc


class WorkflowInputTests(unittest.TestCase):
    def test_selected_all_repo_missing_from_discovery_fails_before_writes(self):
        import json, os, tempfile
        from unittest.mock import patch
        from repo_mirror import cli
        source = {"type": "github", "base_url": "https://api.github.com", "owner": "Alice", "repos": "all"}
        env = {"MIRROR_TIMESTAMP_SECRET": "test", "GH_TOKEN": "token", "DESTINATION_OWNER": "dest",
               "MIRROR_SOURCE_URLS": "https://github.com/alice/missing", "MIRROR_DESTINATION_NAME": "copy", "MIRROR_VISIBILITY": "public"}
        with tempfile.NamedTemporaryFile(mode="w") as f:
            json.dump({"destination_account_type": "personal", "sources": [source]}, f); f.flush()
            with patch.dict(os.environ, env), patch.object(cli, "discover", return_value=[]), patch.object(cli, "get_existing") as preflight, patch.object(cli, "mirror_repository") as run:
                with self.assertRaisesRegex(ValueError, "not discovered"):
                    cli.main(["--config", f.name])
                preflight.assert_not_called()
                run.assert_not_called()

    def test_selected_case_alias_uses_configured_source_and_settings(self):
        import json, os, tempfile
        from unittest.mock import patch
        from repo_mirror import cli
        source = {"type": "github", "base_url": "https://api.github.com", "owner": "Alice", "repos": ["One"]}
        config = {"destination_account_type": "personal", "sources": [source]}
        env = {"MIRROR_TIMESTAMP_SECRET": "test", "GH_TOKEN": "token", "DESTINATION_OWNER": "dest",
               "MIRROR_SOURCE_URLS": "https://github.com/alice/one", "MIRROR_DESTINATION_NAME": "copy", "MIRROR_VISIBILITY": "public"}
        with tempfile.NamedTemporaryFile(mode="w") as f:
            json.dump(config, f); f.flush()
            with patch.dict(os.environ, env), patch.object(cli, "get_existing", return_value=None), patch.object(cli, "mirror_repository") as run:
                cli.main(["--config", f.name])
        selected, name = run.call_args.args[:2]
        self.assertEqual((selected["base_url"], selected["owner"], name), ("https://api.github.com", "Alice", "One"))
        self.assertEqual(selected["destinations"]["one"], {"name": "copy", "visibility": "public"})

    def test_remove_github_api_base_alias_and_refuse_all(self):
        source = {"type": "github", "base_url": "https://api.github.com/", "owner": "Alice", "repos": ["One", "Two"]}
        config = {"sources": [source]}
        self.assertTrue(adhoc.remove_from_config(config, "https://github.com/alice/one"))
        self.assertEqual(source["repos"], ["Two"])
        source["repos"] = "all"
        with self.assertRaisesRegex(ValueError, "cannot remove"):
            adhoc.remove_from_config(config, "https://github.com/alice/two")
        self.assertEqual(source["repos"], "all")

    def test_custom_destination_is_saved_and_used(self):
        from repo_mirror.sources import destination_name
        config = {"sources": []}
        adhoc.add_to_config(config, "https://github.com/alice/one", destination="my-copy", visibility="public")
        source = config["sources"][0]
        self.assertEqual(destination_name(source, "one"), "my-copy")
        self.assertEqual(source["destinations"]["one"]["visibility"], "public")

    def test_public_creation_uses_visibility_mapping(self):
        import io
        from urllib.error import HTTPError
        from unittest.mock import patch
        from repo_mirror import repository
        class Src:
            def request(self, method, url):
                return {"clone_url": "https://github.com/a/x.git"}
        class Dst:
            def __init__(self): self.created = None
            def request(self, method, url, data=None):
                if method == "GET": raise HTTPError(url, 404, "", {}, io.BytesIO())
                if method == "POST":
                    self.created = data
                    return {"full_name": "dest/copy"}
                raise RuntimeError("stop after creation")
        source = {"type": "github", "base_url": "https://github.com", "owner": "a", "repos": ["x"],
                  "destinations": {"x": {"name": "copy", "visibility": "public"}}}
        dst = Dst()
        with self.assertRaisesRegex(RuntimeError, "stop after creation"):
            repository.mirror_repository(source, "x", "dest", Src(), dst, "token")
        self.assertIs(dst.created["private"], False)
        self.assertEqual(dst.created["name"], "copy")

    def test_removal_refuses_all_without_mutation(self):
        config = {"sources": [{"type": "github", "base_url": "https://github.com", "owner": "a", "repos": "all"}]}
        with self.assertRaises(ValueError):
            adhoc.remove_from_config(config, "https://github.com/a/one")
        self.assertEqual(config["sources"][0]["repos"], "all")

    def test_rejects_multiple_urls_with_custom_name_without_mutating(self):
        config = {"sources": []}
        with self.assertRaises(ValueError):
            adhoc.add_to_config(config, "https://github.com/a/x,https://github.com/a/y", destination="same")
        self.assertEqual(config["sources"], [])

    def test_config_writer_rejects_destination_collision(self):
        import json, tempfile, os
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "config.json")
            original = {"sources": [{"type": "github", "base_url": "https://github.com", "owner": "a", "repos": ["x"]}]}
            with open(path, "w") as f:
                json.dump(original, f)
            with self.assertRaises(ValueError):
                adhoc.main(["--config", path, "--urls", "https://github.com/b/y", "--destination", "mirror-a-x"])
            with open(path) as f:
                self.assertEqual(json.load(f), original)

    def test_workflow_removal_skips_entire_mirror_job(self):
        from pathlib import Path
        text = Path(".github/workflows/mirror.yml").read_text()
        mirror_job = text.split("  mirror:\n", 1)[1]
        self.assertIn("if: ${{ !inputs.remove_from_config }}", mirror_job.split("    steps:", 1)[0])
        self.assertIn("inputs.add_to_config || inputs.remove_from_config", text)

    def test_invalid_custom_names_are_rejected_without_echo(self):
        for name in ("../escape", "bad name", "x" * 101, ".", ""):
            if not name:
                continue
            with self.subTest(name=name), self.assertRaises(ValueError):
                adhoc.add_to_config({"sources": []}, "https://github.com/a/x", destination=name)

    def test_remove_explicit_source(self):
        config = {"sources": [{"type": "github", "base_url": "https://github.com", "owner": "Alice", "repos": ["one", "two"]}]}
        self.assertTrue(adhoc.remove_from_config(config, "https://github.com/alice/one"))
        self.assertEqual(config["sources"][0]["repos"], ["two"])

    def test_unspecified_visibility_preserves_existing_mapping(self):
        config = {"sources": [{"type": "github", "base_url": "https://github.com", "owner": "a", "repos": ["x"], "destinations": {"x": {"name": "copy", "visibility": "public"}}}]}
        self.assertFalse(adhoc.add_to_config(config, "https://github.com/a/x"))
        self.assertEqual(config["sources"][0]["destinations"]["x"]["visibility"], "public")

    def test_duplicate_selected_urls_rejected_before_mutation(self):
        config = {"sources": []}
        with self.assertRaisesRegex(ValueError, "duplicate"):
            adhoc.add_to_config(config, "https://github.com/a/x,https://github.com/A/X")
        self.assertEqual(config["sources"], [])

    def test_config_writer_rejects_missing_all_repo_without_mutation(self):
        import json, tempfile, os
        from unittest.mock import patch
        original = {"sources": [{"type": "github", "base_url": "https://api.github.com", "owner": "Alice", "repos": "all"}]}
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "config.json")
            with open(path, "w") as f: json.dump(original, f)
            with open(path, "rb") as f: before = f.read()
            with patch("repo_mirror.sources.discover", return_value=["Other"]):
                with self.assertRaisesRegex(ValueError, "not discovered"):
                    adhoc.main(["--config", path, "--urls", "https://github.com/alice/missing", "--destination", "copy", "--visibility", "public"])
            with open(path, "rb") as f: self.assertEqual(f.read(), before)

    def test_config_writer_accepts_case_alias_of_discovered_all_repo(self):
        import json, tempfile, os
        from unittest.mock import patch
        original = {"sources": [{"type": "github", "base_url": "https://api.github.com", "owner": "Alice", "repos": "all"}]}
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "config.json")
            with open(path, "w") as f: json.dump(original, f)
            with patch("repo_mirror.sources.discover", return_value=["Existing"]):
                adhoc.main(["--config", path, "--urls", "https://github.com/alice/existing", "--destination", "copy", "--visibility", "public"])
            with open(path) as f: config = json.load(f)
            self.assertEqual(config["sources"][0]["destinations"]["existing"], {"name": "copy", "visibility": "public"})

    def test_config_writer_discovers_all_before_writing(self):
        import json, tempfile, os
        from unittest.mock import patch
        source = {"type": "github", "base_url": "https://github.com", "owner": "a", "repos": "all"}
        original = {"sources": [source]}
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "config.json")
            with open(path, "w") as f: json.dump(original, f)
            with patch("repo_mirror.sources.discover", side_effect=lambda source, api: ["x"] if source["repos"] == "all" else source["repos"]):
                with self.assertRaisesRegex(ValueError, "duplicate destination"):
                    adhoc.main(["--config", path, "--urls", "https://github.com/b/y", "--destination", "mirror-a-x"])
            with open(path) as f: self.assertEqual(json.load(f), original)

    def test_discovery_failure_leaves_config_unchanged(self):
        import json, tempfile, os
        from unittest.mock import patch
        original = {"sources": [{"type": "github", "base_url": "https://github.com", "owner": "a", "repos": "all"}]}
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "config.json")
            with open(path, "w") as f: json.dump(original, f)
            with patch("repo_mirror.sources.discover", side_effect=RuntimeError("unavailable")):
                with self.assertRaisesRegex(RuntimeError, "unavailable"):
                    adhoc.main(["--config", path, "--urls", "https://github.com/b/y", "--destination", "copy"])
            with open(path) as f: self.assertEqual(json.load(f), original)

    def test_explicit_private_changes_existing_visibility(self):
        config = {"sources": [{"type": "github", "base_url": "https://github.com", "owner": "a", "repos": ["x"], "destinations": {"x": {"visibility": "public"}}}]}
        self.assertTrue(adhoc.add_to_config(config, "https://github.com/a/x", visibility="private"))
        self.assertEqual(config["sources"][0]["destinations"]["x"]["visibility"], "private")

    def test_cli_preserves_saved_visibility_on_selection(self):
        import json, os, tempfile
        from unittest.mock import patch
        from repo_mirror import cli
        source = {"type": "github", "base_url": "https://github.com", "owner": "a", "repos": ["x"], "destinations": {"x": {"visibility": "public"}}}
        env = {"MIRROR_TIMESTAMP_SECRET": "test", "GH_TOKEN": "token", "DESTINATION_OWNER": "dest", "MIRROR_SOURCE_URLS": "https://github.com/a/x", "MIRROR_VISIBILITY": "unchanged"}
        with tempfile.NamedTemporaryFile(mode="w") as f:
            json.dump({"destination_account_type": "personal", "sources": [source]}, f); f.flush()
            with patch.dict(os.environ, env, clear=True), patch.object(cli, "get_existing", return_value=None), patch.object(cli, "mirror_repository") as run:
                cli.main(["--config", f.name])
        self.assertEqual(run.call_args.args[0]["destinations"]["x"]["visibility"], "public")
