import unittest

from repo_mirror import adhoc


class WorkflowInputTests(unittest.TestCase):
    def test_selected_case_alias_uses_configured_source_and_settings(self):
        import json, os, tempfile
        from unittest.mock import patch
        from repo_mirror import cli
        config = {
            "destinations": {"main": {"account_type": "personal"}},
            "repositories": [{"source": {"url": "https://github.com/Alice/One"}, "mirrors": {"main": {}}}],
        }
        env = {"MIRROR_TIMESTAMP_SECRET": "test", "GH_TOKEN": "token", "DESTINATION_OWNER": "dest",
               "MIRROR_SOURCE_URLS": "https://github.com/alice/one", "MIRROR_DESTINATION_NAME": "copy", "MIRROR_VISIBILITY": "public"}
        with tempfile.NamedTemporaryFile(mode="w") as f:
            json.dump(config, f); f.flush()
            with patch.dict(os.environ, env), patch.object(cli, "get_existing", return_value=None), patch.object(cli, "mirror_repository") as run:
                cli.main(["--config", f.name])
        selected, name = run.call_args.args[:2]
        self.assertEqual((selected["base_url"], selected["owner"], name), ("https://github.com", "Alice", "One"))
        self.assertEqual(selected["mirror"], {"name": "copy", "visibility": "public"})

    def test_custom_destination_is_saved_and_used(self):
        from repo_mirror.config_named import normalize_config
        from repo_mirror.sources import destination_name
        config = {"destinations": {"main": {"account_type": "personal"}}, "repositories": []}
        adhoc.add_to_config(config, "https://github.com/alice/one", destination="my-copy", visibility="public")
        source = normalize_config(config)["repositories"][0]
        self.assertEqual(destination_name(source, "one"), "my-copy")
        self.assertEqual(source["mirror"]["visibility"], "public")

    def test_public_creation_uses_visibility_mapping(self):
        import io
        from urllib.error import HTTPError
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
        source = {"type": "github", "base_url": "https://github.com", "owner": "a", "name": "x",
                  "mirror": {"name": "copy", "visibility": "public"}}
        dst = Dst()
        with self.assertRaisesRegex(RuntimeError, "stop after creation"):
            repository.mirror_repository(source, "x", "dest", Src(), dst, "token")
        self.assertIs(dst.created["private"], False)
        self.assertEqual(dst.created["name"], "copy")

    def test_rejects_multiple_urls_with_custom_name_without_mutating(self):
        config = {"destinations": {"main": {"account_type": "personal"}}, "repositories": []}
        with self.assertRaises(ValueError):
            adhoc.add_to_config(config, "https://github.com/a/x,https://github.com/a/y", destination="same")
        self.assertEqual(config["repositories"], [])

    def test_config_writer_rejects_destination_collision(self):
        import json, tempfile, os
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "config.json")
            original = {
                "destinations": {"main": {"account_type": "personal"}},
                "repositories": [{"source": {"url": "https://github.com/a/x"}, "mirrors": {"main": {}}}],
            }
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
                adhoc.add_to_config(
                    {"destinations": {"main": {"account_type": "personal"}}, "repositories": []},
                    "https://github.com/a/x",
                    destination=name,
                )

    def test_remove_explicit_source(self):
        config = {
            "destinations": {"main": {"account_type": "personal"}},
            "repositories": [
                {"source": {"url": "https://github.com/Alice/one"}, "mirrors": {"main": {}}},
                {"source": {"url": "https://github.com/Alice/two"}, "mirrors": {"main": {}}},
            ],
        }
        self.assertTrue(adhoc.remove_from_config(config, "https://github.com/alice/one"))
        self.assertEqual([e["source"]["url"] for e in config["repositories"]], ["https://github.com/Alice/two"])

    def test_unspecified_visibility_preserves_existing_mapping(self):
        config = {
            "destinations": {"main": {"account_type": "personal"}},
            "repositories": [{"source": {"url": "https://github.com/a/x"}, "mirrors": {"main": {"name": "copy", "visibility": "public"}}}],
        }
        self.assertFalse(adhoc.add_to_config(config, "https://github.com/a/x"))
        self.assertEqual(config["repositories"][0]["mirrors"]["main"]["visibility"], "public")

    def test_duplicate_selected_urls_rejected_before_mutation(self):
        config = {"destinations": {"main": {"account_type": "personal"}}, "repositories": []}
        with self.assertRaisesRegex(ValueError, "duplicate"):
            adhoc.add_to_config(config, "https://github.com/a/x,https://github.com/A/X")
        self.assertEqual(config["repositories"], [])

    def test_explicit_private_changes_existing_visibility(self):
        config = {
            "destinations": {"main": {"account_type": "personal"}},
            "repositories": [{"source": {"url": "https://github.com/a/x"}, "mirrors": {"main": {"visibility": "public"}}}],
        }
        self.assertTrue(adhoc.add_to_config(config, "https://github.com/a/x", visibility="private"))
        self.assertEqual(config["repositories"][0]["mirrors"]["main"]["visibility"], "private")

    def test_cli_preserves_saved_visibility_on_selection(self):
        import json, os, tempfile
        from unittest.mock import patch
        from repo_mirror import cli
        config = {
            "destinations": {"main": {"account_type": "personal"}},
            "repositories": [{"source": {"url": "https://github.com/a/x"}, "mirrors": {"main": {"visibility": "public"}}}],
        }
        env = {"MIRROR_TIMESTAMP_SECRET": "test", "GH_TOKEN": "token", "DESTINATION_OWNER": "dest",
               "MIRROR_SOURCE_URLS": "https://github.com/a/x", "MIRROR_VISIBILITY": "unchanged"}
        with tempfile.NamedTemporaryFile(mode="w") as f:
            json.dump(config, f); f.flush()
            with patch.dict(os.environ, env, clear=True), patch.object(cli, "get_existing", return_value=None), patch.object(cli, "mirror_repository") as run:
                cli.main(["--config", f.name])
        self.assertEqual(run.call_args.args[0]["mirror"]["visibility"], "public")
