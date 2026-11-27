"""Offline tests for owner-URL workflow interface, classification, filter, add/remove, and CLI wiring."""

import copy
import json
import os
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from repo_mirror import adhoc, cli
from repo_mirror.adhoc import (
    ClassifiedURL,
    add_to_config,
    add_to_config_pinned,
    classify_source_url,
    one_time_sources,
    remove_from_config,
)
from repo_mirror.source_filter import entry_urls, filter_plan


def _http_error(url, code):
    return HTTPError(url, code, f"HTTP {code}", Message(), None)


class FakeGitLabAPI:
    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.requests = []

    def request(self, method, url, data=None):
        self.requests.append((method, url))
        if url not in self.routes:
            raise _http_error(url, 404)
        value = self.routes[url]
        if isinstance(value, Exception):
            raise value
        return value


class FakeOnboardingStore:
    def __init__(self, raw_bytes):
        self.data = raw_bytes
        self.sha = "a" * 40
        self.publish_calls = 0

    def read(self):
        return self.data, self.sha

    def publish(self, expected_sha, replacement):
        self.publish_calls += 1
        if expected_sha != self.sha:
            raise ValueError("conflict")
        self.data = replacement
        self.sha = f"{self.publish_calls:040x}"


def _base_config(auto_discoveries=None, repositories=None):
    cfg = {
        "graphql": False,
        "destinations": {"main": {"account_type": "personal"}},
        "repositories": list(repositories or []),
    }
    if auto_discoveries is not None:
        cfg["auto_discoveries"] = list(auto_discoveries)
    return cfg


class ClassifySourceUrlTests(unittest.TestCase):
    def test_declared_rule_and_repository_classification_case_insensitive(self):
        cfg = _base_config(
            auto_discoveries=[{"url": "https://github.com/Alice", "id": 100}],
            repositories=[
                {"source": {"url": "https://github.com/Bob/beta", "id": 2}, "mirrors": {"main": {"name": "m-beta", "id": 20}}}
            ],
        )
        rule_hit = classify_source_url("https://GitHub.com/alice", cfg)
        self.assertEqual(
            rule_hit,
            ClassifiedURL(
                kind="rule",
                source_type="github",
                base_url="https://github.com",
                owner="Alice",
                name=None,
                url="https://github.com/Alice",
                existing=True,
                owner_id=100,
            ),
        )
        repo_hit = classify_source_url("https://GitHub.com/bob/BETA", cfg)
        self.assertEqual(
            repo_hit,
            ClassifiedURL(
                kind="repository",
                source_type="github",
                base_url="https://github.com",
                owner="Bob",
                name="beta",
                url="https://github.com/Bob/beta",
                existing=True,
            ),
        )

    def test_new_github_and_forgejo_owner_urls_and_deeper_gitlab_paths(self):
        cfg = _base_config()
        gh_rule = classify_source_url("https://github.com/NewOwner", cfg)
        self.assertEqual(gh_rule.kind, "rule")
        self.assertEqual(gh_rule.source_type, "github")
        self.assertFalse(gh_rule.existing)
        self.assertEqual(gh_rule.url, "https://github.com/NewOwner")

        fj_rule = classify_source_url("https://forge.example/TeamOrg", cfg)
        self.assertEqual(fj_rule.kind, "rule")
        self.assertEqual(fj_rule.source_type, "forgejo")
        self.assertFalse(fj_rule.existing)
        self.assertEqual(fj_rule.url, "https://forge.example/TeamOrg")

        gl_deep = classify_source_url("https://gitlab.com/team/sub/repo", cfg)
        self.assertEqual(gl_deep.kind, "repository")
        self.assertEqual(gl_deep.source_type, "gitlab")
        self.assertEqual((gl_deep.owner, gl_deep.name), ("team/sub", "repo"))
        self.assertFalse(gl_deep.existing)

    def test_gitlab_two_segment_live_disambiguation(self):
        cfg = _base_config()
        # Group exists, project 404 -> rule
        group_api = FakeGitLabAPI(
            routes={
                "https://gitlab.com/api/v4/groups/team%2Fsub": {
                    "id": 77,
                    "visibility": "public",
                    "full_path": "team/sub",
                }
            }
        )
        group_res = classify_source_url("https://gitlab.com/team/sub", cfg, gitlab_api=group_api)
        self.assertEqual(group_res.kind, "rule")
        self.assertEqual(group_res.owner_id, 77)
        self.assertEqual(group_res.url, "https://gitlab.com/team/sub")

        # Project exists, group 404 -> repository
        proj_api = FakeGitLabAPI(
            routes={"https://gitlab.com/api/v4/projects/alice%2Fapp": {"id": 88, "visibility": "public"}}
        )
        proj_res = classify_source_url("https://gitlab.com/alice/app", cfg, gitlab_api=proj_api)
        self.assertEqual(proj_res.kind, "repository")
        self.assertEqual((proj_res.owner, proj_res.name), ("alice", "app"))

        # Both exist -> ambiguous error without echoing token
        both_api = FakeGitLabAPI(
            routes={
                "https://gitlab.com/api/v4/groups/team%2Fdup": {"id": 77, "visibility": "public", "full_path": "team/dup"},
                "https://gitlab.com/api/v4/projects/team%2Fdup": {"id": 88, "visibility": "public"},
            }
        )
        with self.assertRaisesRegex(ValueError, "^ambiguous owner or repository URL$") as ctx:
            classify_source_url("https://gitlab.com/team/dup", cfg, gitlab_api=both_api)
        self.assertNotIn("team/dup", str(ctx.exception))

        # Neither exists -> error without echoing token
        neither_api = FakeGitLabAPI()
        with self.assertRaisesRegex(ValueError, "^unknown or unsupported source repository URL$") as ctx:
            classify_source_url("https://gitlab.com/none/missing", cfg, gitlab_api=neither_api)
        self.assertNotIn("missing", str(ctx.exception))

    def test_gitlab_single_segment_user_live_probe_and_precedence(self):
        cfg = _base_config()
        with patch.object(adhoc, "_gitlab_probe_client", side_effect=AssertionError("unexpected network call")):
            user_api = FakeGitLabAPI(
                routes={"https://gitlab.com/api/v4/users?username=someuser": [{"id": 321, "username": "SomeUser"}]}
            )
            res = classify_source_url("https://gitlab.com/someuser", cfg, gitlab_api=user_api)
            self.assertEqual(
                res,
                ClassifiedURL(
                    kind="rule",
                    source_type="gitlab",
                    base_url="https://gitlab.com",
                    owner="SomeUser",
                    name=None,
                    url="https://gitlab.com/SomeUser",
                    existing=False,
                    owner_id=321,
                ),
            )
            self.assertEqual(
                user_api.requests,
                [("GET", "https://gitlab.com/api/v4/users?username=someuser")],
            )

            # Existing rule takes precedence over live probe.
            existing_cfg = _base_config(auto_discoveries=[{"url": "https://gitlab.com/someuser", "id": 321}])
            no_probe_api = FakeGitLabAPI()
            existing_res = classify_source_url("https://gitlab.com/SomeUser", existing_cfg, gitlab_api=no_probe_api)
            self.assertTrue(existing_res.existing)
            self.assertEqual(existing_res.owner_id, 321)
            self.assertEqual(no_probe_api.requests, [])

            # Missing user: empty list or 404 -> fixed error string, never echoes token.
            empty_api = FakeGitLabAPI(routes={"https://gitlab.com/api/v4/users?username=missinguser": []})
            with self.assertRaisesRegex(ValueError, "^unknown or unsupported source repository URL$") as ctx:
                classify_source_url("https://gitlab.com/missinguser", cfg, gitlab_api=empty_api)
            self.assertNotIn("missinguser", str(ctx.exception))

            not_found_api = FakeGitLabAPI()
            with self.assertRaisesRegex(ValueError, "^unknown or unsupported source repository URL$") as ctx:
                classify_source_url("https://gitlab.com/missinguser", cfg, gitlab_api=not_found_api)
            self.assertNotIn("missinguser", str(ctx.exception))

            # Malformed or mismatched responses fail closed without echoing input.
            bad_payloads = [
                [{"id": 1, "username": "someuser"}, {"id": 2, "username": "someuser2"}],
                [{"id": True, "username": "someuser"}],
                [{"id": 0, "username": "someuser"}],
                [{"id": 321, "username": "other"}],
            ]
            for payload in bad_payloads:
                bad_api = FakeGitLabAPI(routes={"https://gitlab.com/api/v4/users?username=someuser": payload})
                with self.assertRaises(ValueError) as ctx:
                    classify_source_url("https://gitlab.com/someuser", cfg, gitlab_api=bad_api)
                self.assertNotIn("someuser", str(ctx.exception))

    def test_ambiguous_config_and_credentials_not_echoed(self):
        ambig_cfg = {
            "auto_discoveries": [{"url": "https://gitlab.com/team/sub", "id": 7}],
            "repositories": [{"source": {"url": "https://gitlab.com/team/sub"}, "mirrors": {"main": {"name": "m"}}}],
        }
        with self.assertRaisesRegex(ValueError, "ambiguous source URL matches both an auto-discovery rule and a repository entry"):
            classify_source_url("https://gitlab.com/team/sub", ambig_cfg)

        secret = "secret-password-token"
        with self.assertRaises(ValueError) as ctx:
            classify_source_url(f"https://user:{secret}@github.com/owner", _base_config())
        self.assertNotIn(secret, str(ctx.exception))


class FilterPlanOwnerUrlTests(unittest.TestCase):
    def test_entry_urls_and_filter_selects_rule_children_only(self):
        alice_src = {
            "type": "github",
            "base_url": "https://github.com",
            "owner": "Alice",
            "name": "alpha",
            "discovered_by": "https://github.com/Alice",
        }
        alice_src_2 = {
            "type": "github",
            "base_url": "https://github.com",
            "owner": "Alice",
            "name": "beta",
            "discovered_by": "https://github.com/Alice",
        }
        other_src = {"type": "github", "base_url": "https://github.com", "owner": "Bob", "name": "gamma"}

        self.assertIn("https://github.com/Alice", entry_urls(alice_src, "alpha"))
        plan = [(1, alice_src, "alpha"), (2, other_src, "gamma"), (3, alice_src_2, "beta")]
        selected = filter_plan(plan, "https://github.com/alice", case_insensitive=True)
        self.assertEqual(selected, [plan[0], plan[2]])

        # Overlapping rule + child is rejected.
        with self.assertRaisesRegex(ValueError, "overlapping auto-discovery rule and child repository URL"):
            filter_plan(plan, "https://github.com/Alice,https://github.com/Alice/alpha", case_insensitive=True)

        # Empty known rule returns empty filtered plan instead of unknown URL error.
        self.assertEqual(
            filter_plan([plan[1]], "https://github.com/Alice", case_insensitive=True, known_rules={"https://github.com/Alice"}),
            [],
        )


class AddAndRemoveConfigOwnerUrlTests(unittest.TestCase):
    def test_add_writes_rule_and_no_entries_and_rejects_duplicate_and_destination_name(self):
        cfg = _base_config()
        changed = add_to_config(
            cfg,
            "https://github.com/example-owner",
            visibility="public",
            ignore_releases=True,
        )
        self.assertTrue(changed)
        self.assertEqual(cfg["repositories"], [])
        self.assertEqual(
            cfg["auto_discoveries"],
            [
                {
                    "url": "https://github.com/example-owner",
                    "visibility": "public",
                    "ignore_releases": True,
                    "destination_name_prefix": "mirror-example-owner",
                }
            ],
        )

        # Duplicate rule rejected without mutation.
        before = copy.deepcopy(cfg)
        with self.assertRaisesRegex(ValueError, "duplicate auto-discovery rule"):
            add_to_config(cfg, "https://GitHub.com/Example-Owner")
        self.assertEqual(cfg, before)

        # destination_name rejected for discovery rule.
        with self.assertRaisesRegex(ValueError, "^destination name is derived per repository for a discovery rule$"):
            add_to_config(_base_config(), "https://github.com/Alice", destination="custom-name")

    def test_add_to_config_pinned_handles_owner_rule_without_creating_repos(self):
        cfg = _base_config()
        dst = Mock()
        dst.request.side_effect = AssertionError("destination must not be called for rule addition")
        gl_api = FakeGitLabAPI(
            routes={"https://gitlab.com/api/v4/groups/team%2Fsub": {"id": 42, "visibility": "public", "full_path": "team/sub"}}
        )
        changed = add_to_config_pinned(
            cfg,
            "https://gitlab.com/team/sub",
            Mock(),
            gl_api,
            dst,
            owner="dest",
            account_type="personal",
            visibility="private",
            ignore_releases=False,
        )
        self.assertTrue(changed)
        self.assertEqual(cfg["repositories"], [])
        self.assertEqual(
            cfg["auto_discoveries"],
            [
                {
                    "url": "https://gitlab.com/team/sub",
                    "type": "gitlab",
                    "id": 42,
                    "visibility": "private",
                    "ignore_releases": False,
                    "destination_name_prefix": "mirror-team-sub",
                }
            ],
        )

    def test_add_gitlab_single_segment_user_and_reject_missing_user(self):
        with patch.object(adhoc, "_gitlab_probe_client", side_effect=AssertionError("unexpected network call")):
            cfg = _base_config()
            user_api = FakeGitLabAPI(
                routes={"https://gitlab.com/api/v4/users?username=someuser": [{"id": 321, "username": "someuser"}]}
            )
            changed = add_to_config(
                cfg,
                "https://gitlab.com/someuser",
                visibility="public",
                ignore_releases=True,
                gitlab_api=user_api,
            )
            self.assertTrue(changed)
            self.assertEqual(cfg["repositories"], [])
            self.assertEqual(
                cfg["auto_discoveries"],
                [
                    {
                        "url": "https://gitlab.com/someuser",
                        "type": "gitlab",
                        "id": 321,
                        "visibility": "public",
                        "ignore_releases": True,
                        "destination_name_prefix": "mirror-someuser",
                    }
                ],
            )

            # Duplicate addition is rejected without probing or mutating cfg.
            before = copy.deepcopy(cfg)
            dup_api = FakeGitLabAPI()
            with self.assertRaisesRegex(ValueError, "duplicate auto-discovery rule"):
                add_to_config(cfg, "https://gitlab.com/SomeUser", gitlab_api=dup_api)
            self.assertEqual(dup_api.requests, [])
            self.assertEqual(cfg, before)

            # add_to_config_pinned also adds a single-segment GitLab user rule without touching dst.
            pinned_cfg = _base_config()
            dst = Mock()
            dst.request.side_effect = AssertionError("destination must not be called for rule addition")
            pinned_api = FakeGitLabAPI(
                routes={"https://gitlab.com/api/v4/users?username=alice": [{"id": 654, "username": "Alice"}]}
            )
            pinned_changed = add_to_config_pinned(
                pinned_cfg,
                "https://gitlab.com/alice",
                Mock(),
                pinned_api,
                dst,
                owner="dest",
                account_type="personal",
                visibility="private",
                ignore_releases=False,
            )
            self.assertTrue(pinned_changed)
            self.assertEqual(pinned_cfg["repositories"], [])
            self.assertEqual(
                pinned_cfg["auto_discoveries"],
                [
                    {
                        "url": "https://gitlab.com/Alice",
                        "type": "gitlab",
                        "id": 654,
                        "visibility": "private",
                        "ignore_releases": False,
                        "destination_name_prefix": "mirror-Alice",
                    }
                ],
            )

            # Missing user (empty list or 404) is rejected with fixed string and does not mutate cfg.
            for missing_api in (
                FakeGitLabAPI(routes={"https://gitlab.com/api/v4/users?username=missinguser": []}),
                FakeGitLabAPI(),
            ):
                missing_cfg = _base_config()
                before_missing = copy.deepcopy(missing_cfg)
                with self.assertRaisesRegex(ValueError, "^unknown or unsupported source repository URL$") as ctx:
                    add_to_config(missing_cfg, "https://gitlab.com/missinguser", gitlab_api=missing_api)
                self.assertNotIn("missinguser", str(ctx.exception))
                self.assertEqual(missing_cfg, before_missing)

    def test_remove_rule_drops_discovered_by_and_keeps_entries(self):
        cfg = _base_config(
            auto_discoveries=[
                {"url": "https://github.com/Alice", "id": 100, "destination_name_prefix": "custom-Alice"}
            ],
            repositories=[
                {
                    "source": {"url": "https://github.com/Alice/alpha", "id": 11},
                    "mirrors": {"main": {"name": "custom-Alice-alpha", "id": 5001, "visibility": "private"}},
                    "discovered_by": "https://github.com/Alice",
                },
                {
                    "source": {"url": "https://github.com/Bob/beta", "id": 22},
                    "mirrors": {"main": {"name": "mirror-Bob-beta", "id": 5002}},
                },
            ],
        )
        self.assertTrue(remove_from_config(cfg, "https://GitHub.com/alice"))
        self.assertEqual(cfg["auto_discoveries"], [])
        self.assertEqual(len(cfg["repositories"]), 2)
        self.assertNotIn("discovered_by", cfg["repositories"][0])
        self.assertEqual(cfg["repositories"][0]["mirrors"]["main"]["id"], 5001)
        self.assertEqual(cfg["repositories"][0]["mirrors"]["main"]["name"], "custom-Alice-alpha")

    def test_remove_child_writes_exclude_once_and_removes_entry(self):
        cfg = _base_config(
            auto_discoveries=[
                {
                    "url": "https://github.com/Alice",
                    "id": 100,
                    "exclude": ["Existing"],
                    "overrides": {"Alpha": {"visibility": "public"}},
                }
            ],
            repositories=[
                {
                    "source": {"url": "https://github.com/Alice/alpha", "id": 11},
                    "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 5001, "visibility": "public"}},
                    "discovered_by": "https://github.com/Alice",
                },
                {
                    "source": {"url": "https://github.com/Alice/existing", "id": 12},
                    "mirrors": {"main": {"name": "mirror-Alice-existing", "id": 5002}},
                    "discovered_by": "https://github.com/Alice",
                },
            ],
        )
        self.assertTrue(remove_from_config(cfg, "https://github.com/Alice/alpha"))
        self.assertEqual(cfg["auto_discoveries"][0]["exclude"], ["Existing", "alpha"])
        self.assertNotIn("overrides", cfg["auto_discoveries"][0])
        self.assertEqual(len(cfg["repositories"]), 1)

        # Removing an already-excluded child does not duplicate in exclude (case-insensitive).
        self.assertTrue(remove_from_config(cfg, "https://github.com/Alice/existing"))
        self.assertEqual(cfg["auto_discoveries"][0]["exclude"], ["Existing", "alpha"])
        self.assertEqual(cfg["repositories"], [])


class CliOwnerWorkflowTests(unittest.TestCase):
    def test_cli_onboards_and_filters_to_rule_children_and_applies_ignore_releases(self):
        cfg = _base_config(
            auto_discoveries=[{"url": "https://github.com/Alice"}],
            repositories=[
                {
                    "source": {"url": "https://github.com/Other/stay", "id": 99},
                    "mirrors": {"main": {"name": "mirror-Other-stay", "id": 9001}},
                }
            ],
        )
        raw = (json.dumps(cfg, indent=2) + "\n").encode("utf-8")
        store = FakeOnboardingStore(raw)
        dst_calls = []

        class Dst:
            def __init__(self):
                self.repos = {}
                self._next = 5000

            def request(self, method, url, data=None):
                dst_calls.append((method, url))
                if method == "GET" and url == "https://api.github.com/user":
                    return {"login": "dest"}
                if method == "POST" and url == "https://api.github.com/user/repos":
                    self._next += 1
                    repo = {
                        "id": self._next,
                        "name": data["name"],
                        "full_name": f"dest/{data['name']}",
                        "private": True,
                        "description": data["description"],
                    }
                    self.repos[data["name"].lower()] = repo
                    return dict(repo)
                if url.endswith("/actions/permissions"):
                    return {"enabled": False} if method == "GET" else None
                if method == "GET" and url.startswith("https://api.github.com/repos/dest/"):
                    name = url.rsplit("/", 1)[-1].lower()
                    if name not in self.repos:
                        raise _http_error(url, 404)
                    return dict(self.repos[name])
                raise AssertionError(f"unexpected dst call {method} {url}")

        class Src:
            def request(self, method, url, data=None):
                if url == "https://api.github.com/users/Alice":
                    return {"id": 100, "login": "Alice", "type": "User"}
                raise _http_error(url, 404)

            def pages(self, url):
                if url == "https://api.github.com/users/Alice/repos?type=owner":
                    return [
                        {"id": 11, "name": "alpha", "full_name": "Alice/alpha", "clone_url": "https://github.com/Alice/alpha.git"}
                    ]
                raise _http_error(url, 404)

        mirrored = []

        def fake_mirror(source, name, owner, *args, **kwargs):
            mirrored.append((source["owner"], name, source.get("mirror", {}).get("ignore_releases")))
            return None

        dst = Dst()
        env = {
            "GH_TOKEN": "token",
            "DESTINATION_OWNER": "dest",
            "MIRROR_TIMESTAMP_SECRET": "secret",
            "MIRROR_SOURCE_URLS": "https://github.com/alice",
            "MIRROR_IGNORE_RELEASES": "true",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(raw)
            with (
                patch.dict(os.environ, env, clear=True),
                patch("repo_mirror.github_migration.store_from_environment", return_value=store),
                patch("repo_mirror.github_migration.transaction_from_environment", return_value=None),
                patch.object(cli, "SourceAPI", return_value=Src()),
                patch.object(cli, "API", side_effect=[Mock(), dst, Mock(), dst]),
                patch.object(cli, "mirror_repository", side_effect=fake_mirror),
            ):
                cli.main(["--config", str(path)])
        self.assertEqual(mirrored, [("Alice", "alpha", True)])
        self.assertEqual(store.publish_calls, 1)

    def test_cli_rejects_unconfigured_owner_url_and_destination_name_for_rule(self):
        cfg = _base_config(auto_discoveries=[{"url": "https://github.com/Alice", "id": 100}])
        raw = (json.dumps(cfg, indent=2) + "\n").encode("utf-8")
        store = FakeOnboardingStore(raw)

        class Src:
            def request(self, method, url, data=None):
                return {"id": 100, "login": "Alice", "type": "User"}

            def pages(self, url):
                return []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(raw)

            # Unconfigured owner URL cannot be mirrored one-time.
            env_one_time = {
                "GH_TOKEN": "token",
                "DESTINATION_OWNER": "dest",
                "MIRROR_TIMESTAMP_SECRET": "secret",
                "MIRROR_SOURCE_URLS": "https://github.com/UnconfiguredOwner",
            }
            with (
                patch.dict(os.environ, env_one_time, clear=True),
                patch("repo_mirror.github_migration.store_from_environment", return_value=store),
                patch("repo_mirror.github_migration.transaction_from_environment", return_value=None),
                patch.object(cli, "SourceAPI", return_value=Src()),
                patch.object(cli, "API", side_effect=[Mock(), Mock()]),
            ):
                with self.assertRaisesRegex(ValueError, "one-time mirroring is not supported for owner URLs; enable add_to_config"):
                    cli.main(["--config", str(path)])

            # destination_name rejected when filtering to an existing rule URL.
            env_dest_name = {
                "GH_TOKEN": "token",
                "DESTINATION_OWNER": "dest",
                "MIRROR_TIMESTAMP_SECRET": "secret",
                "MIRROR_SOURCE_URLS": "https://github.com/Alice",
                "MIRROR_DESTINATION_NAME": "custom",
            }
            with (
                patch.dict(os.environ, env_dest_name, clear=True),
                patch("repo_mirror.github_migration.store_from_environment", return_value=store),
                patch("repo_mirror.github_migration.transaction_from_environment", return_value=None),
                patch.object(cli, "SourceAPI", return_value=Src()),
                patch.object(cli, "API", side_effect=[Mock(), Mock()]),
            ):
                with self.assertRaisesRegex(ValueError, "destination name is derived per repository for a discovery rule"):
                    cli.main(["--config", str(path)])

    def test_cli_remaps_selected_old_owner_url_across_rule_relocation(self):
        cfg = _base_config(
            auto_discoveries=[{"url": "https://github.com/Alice", "id": 100}],
            repositories=[
                {
                    "source": {"url": "https://github.com/Alice/alpha", "id": 11},
                    "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 5001, "visibility": "private"}},
                    "discovered_by": "https://github.com/Alice",
                }
            ],
        )
        raw = (json.dumps(cfg, indent=2) + "\n").encode("utf-8")
        store = FakeOnboardingStore(raw)

        class Src:
            def request(self, method, url, data=None):
                if url == "https://api.github.com/users/Alice":
                    return {"id": 100, "login": "AliceNew", "type": "User"}
                raise _http_error(url, 404)

            def pages(self, url):
                if url == "https://api.github.com/users/AliceNew/repos?type=owner":
                    return [
                        {"id": 11, "name": "alpha", "full_name": "AliceNew/alpha", "clone_url": "https://github.com/AliceNew/alpha.git"}
                    ]
                raise _http_error(url, 404)

        mirrored = []

        def fake_mirror(source, name, owner, *args, **kwargs):
            mirrored.append((source["owner"], name, source.get("discovered_by")))
            return None

        env = {
            "GH_TOKEN": "token",
            "DESTINATION_OWNER": "dest",
            "MIRROR_TIMESTAMP_SECRET": "secret",
            "MIRROR_SOURCE_URLS": "https://github.com/Alice",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(raw)
            with (
                patch.dict(os.environ, env, clear=True),
                patch("repo_mirror.github_migration.store_from_environment", return_value=store),
                patch("repo_mirror.github_migration.transaction_from_environment", return_value=None),
                patch.object(cli, "SourceAPI", return_value=Src()),
                patch.object(cli, "API", side_effect=[Mock(), Mock(), Mock(), Mock()]),
                patch.object(cli, "get_existing", return_value=None),
                patch.object(cli, "mirror_repository", side_effect=fake_mirror),
            ):
                cli.main(["--config", str(path)])
        self.assertEqual(mirrored, [("AliceNew", "alpha", "https://github.com/AliceNew")])
        self.assertEqual(store.publish_calls, 1)

    def test_one_time_sources_rejects_unconfigured_owner_url(self):
        with self.assertRaisesRegex(ValueError, "one-time mirroring is not supported for owner URLs; enable add_to_config"):
            one_time_sources("https://github.com/NewOwner", [])


if __name__ == "__main__":
    unittest.main()
