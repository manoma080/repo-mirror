import unittest

from repo_mirror.source_filter import filter_plan


class SourceFilterTests(unittest.TestCase):
    def test_github_api_source_accepts_public_url_alias(self):
        source = {"type": "github", "base_url": "https://api.github.com", "owner": "alice"}
        entry = (1, source, "demo")
        self.assertEqual(filter_plan([entry], "https://github.com/alice/demo"), [entry])
        with self.assertRaises(ValueError):
            filter_plan([], "https://github.com/alice/demo")

    def test_duplicate_github_aliases_rejected(self):
        entry = (1, {"type": "github", "base_url": "https://api.github.com", "owner": "alice"}, "demo")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            filter_plan([entry], "https://api.github.com/alice/demo,https://github.com/alice/demo")

    def setUp(self):
        self.one = {"base_url": "https://github.com", "owner": "fixture-owner"}
        self.two = {"base_url": "https://github.com", "owner": "fixture-org-b"}
        self.plan = [(1, self.one, "fixture-app"), (1, self.one, "Other"), (2, self.two, "fixture-project")]

    def test_single_selects_only_exact_repository(self):
        self.assertEqual(filter_plan(self.plan, "https://github.com/fixture-owner/fixture-app"), self.plan[:1])

    def test_blank_means_all(self):
        for value in ("", "  ", None):
            with self.subTest(value=value):
                self.assertEqual(filter_plan(self.plan, value), self.plan)

    def test_multi_select_preserves_plan_order(self):
        value = " https://github.com/fixture-org-b/fixture-project, https://github.com/fixture-owner/fixture-app "
        self.assertEqual(filter_plan(self.plan, value), [self.plan[0], self.plan[2]])

    def test_unknown_and_nonexact_urls_rejected(self):
        for value in (
            "https://github.com/fixture-owner",
            "https://github.com/fixture-owner/Missing",
            "https://github.com/fixture-owner/Fixture-App",
            "https://github.com/fixture-owner/fixture-app/",
            "https://github.com/fixture-owner/fixture-app.git",
            "https://github.com/fixture-owner/fixture-app?x=1",
            "https://github.com/fixture-owner/fixture-app#x",
            "ssh://github.com/fixture-owner/fixture-app",
            "https://github.com/fixture-owner/fixture-app,",
            "https://github.com/fixture-owner/fixture-app,$(touch pwned)",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                filter_plan(self.plan, value)

    def test_credentials_never_appear_in_errors(self):
        secret = "super-secret-value"
        with self.assertRaises(ValueError) as caught:
            filter_plan(self.plan, f"https://user:{secret}@github.com/fixture-owner/fixture-app")
        self.assertNotIn(secret, str(caught.exception))

    def test_duplicate_requested_url_rejected(self):
        url = "https://github.com/fixture-owner/fixture-app"
        with self.assertRaises(ValueError):
            filter_plan(self.plan, f"{url}, {url}")

    def test_ambiguous_discovered_source_rejected(self):
        with self.assertRaises(ValueError):
            filter_plan(self.plan + [self.plan[0]], "https://github.com/fixture-owner/fixture-app")

    def test_different_hosts_are_distinct(self):
        forge = {"base_url": "https://forge.example", "owner": "fixture-owner"}
        entry = (3, forge, "fixture-app")
        self.assertEqual(
            filter_plan(self.plan + [entry], "https://forge.example/fixture-owner/fixture-app"), [entry]
        )


if __name__ == "__main__":
    unittest.main()
