import json
import os
import tempfile
import unittest
from unittest.mock import patch

from repo_mirror.adhoc import add_to_config, one_time_sources, parse_repo_url
from repo_mirror.source_filter import filter_plan


class ParseRepoUrlTests(unittest.TestCase):
    def test_github_and_forgejo(self):
        self.assertEqual(parse_repo_url("https://github.com/a/b"), ("github", "https://github.com", "a", "b"))
        self.assertEqual(parse_repo_url("https://git.example/a/b"), ("forgejo", "https://git.example", "a", "b"))

    def test_rejects_non_exact_urls(self):
        for value in (
            "",
            "http://github.com/a/b",
            "https://user:pw@github.com/a/b",
            "https://github.com/a",
            "https://github.com/a/b/c",
            "https://github.com/a/b?x=1",
            "https://github.com/a/..",
            "https://github.com:443/a/b",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError) as ctx:
                parse_repo_url(value)
            self.assertNotIn("pw", str(ctx.exception))


class AddToConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "destination_account_type": "personal",
            "sources": [
                {"type": "github", "base_url": "https://github.com", "owner": "alice", "repos": ["one"]},
                {"type": "github", "base_url": "https://github.com", "owner": "bob", "repos": "all"},
            ],
        }

    def test_adds_new_repo_to_existing_owner(self):
        self.assertTrue(add_to_config(self.config, "https://github.com/alice/two"))
        self.assertEqual(self.config["sources"][0]["repos"], ["one", "two"])
        self.assertEqual(len(self.config["sources"]), 2)

    def test_adds_new_owner_entry(self):
        self.assertTrue(add_to_config(self.config, "https://git.example/carol/x"))
        self.assertEqual(
            self.config["sources"][-1],
            {"type": "forgejo", "base_url": "https://git.example", "owner": "carol", "repos": ["x"]},
        )

    def test_noop_when_already_present_or_covered(self):
        self.assertFalse(add_to_config(self.config, "https://github.com/alice/one,https://github.com/bob/any"))
        self.assertEqual(self.config["sources"][0]["repos"], ["one"])


class OneTimeSourcesTests(unittest.TestCase):
    def setUp(self):
        self.configured = {"type": "github", "base_url": "https://github.com", "owner": "alice", "repos": ["one"]}
        self.plan = [(1, self.configured, "one")]

    def test_uncovered_url_becomes_source_and_config_untouched(self):
        sources = one_time_sources("https://github.com/bob/new", self.plan)
        self.assertEqual(
            sources, [{"type": "github", "base_url": "https://github.com", "owner": "bob", "repos": ["new"]}]
        )
        self.assertEqual(self.configured["repos"], ["one"])

    def test_covered_url_is_not_duplicated(self):
        self.assertEqual(one_time_sources("https://github.com/alice/one", self.plan), [])

    def test_blank_selects_nothing(self):
        for value in (None, "", "  "):
            self.assertEqual(one_time_sources(value, self.plan), [])

    def test_filter_selects_one_time_entry(self):
        extra = one_time_sources("https://git.example/carol/x", self.plan)[0]
        plan = self.plan + [(2, extra, "x")]
        self.assertEqual(filter_plan(plan, "https://git.example/carol/x"), [plan[1]])


class ConfigWriterTests(unittest.TestCase):
    def test_writer_round_trip_format(self):
        from repo_mirror import adhoc

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"sources": []}, f, indent=2)
                f.write("\n")
            adhoc.main(["--config", path, "--urls", "https://github.com/alice/one"])
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["sources"][0]["repos"], ["one"])
            self.assertTrue(open(path, encoding="utf-8").read().endswith("}\n"))


if __name__ == "__main__":
    unittest.main()
