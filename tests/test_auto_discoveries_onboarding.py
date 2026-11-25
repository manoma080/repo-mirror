"""Offline tests for ``auto_discoveries`` onboarding on the GitHub runner."""

import io
import json
import os
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from repo_mirror import cli
from repo_mirror.auto_discoveries_onboarding import (
    PartialOnboarding,
    discover_rule_repositories,
    onboard_auto_discoveries,
    report_orphans,
    resolve_owner,
)
from repo_mirror.config_named import normalize_config
from repo_mirror.gitlab_onboarding_github import OnboardingPublicationError
from repo_mirror.sources import source_marker


def _http_error(url, code):
    return HTTPError(url, code, f"HTTP {code}", Message(), None)


class FakeStore:
    def __init__(self, raw_bytes):
        self.data = raw_bytes
        self.sha = "a" * 40
        self.publish_calls = 0
        self.read_calls = 0

    def read(self):
        self.read_calls += 1
        return self.data, self.sha

    def publish(self, expected_sha, replacement):
        self.publish_calls += 1
        if expected_sha != self.sha:
            raise ValueError("conflict")
        self.data = replacement
        self.sha = f"{self.publish_calls:040x}"


class FakeSourceAPI:
    def __init__(self, routes=None, pages_routes=None):
        self.routes = dict(routes or {})
        self.pages_routes = dict(pages_routes or {})
        self.requests = []
        self.page_requests = []

    def request(self, method, url, data=None):
        self.requests.append((method, url))
        if url not in self.routes:
            raise _http_error(url, 404)
        value = self.routes[url]
        if isinstance(value, Exception):
            raise value
        return value

    def pages(self, url):
        self.page_requests.append(url)
        if url not in self.pages_routes:
            raise _http_error(url, 404)
        value = self.pages_routes[url]
        if isinstance(value, Exception):
            raise value
        return list(value)


class FakeDestinationAPI:
    def __init__(self, owner="dest", account_type="personal", existing=None):
        self.owner = owner
        self.account_type = account_type
        self.repos = dict(existing or {})
        self.actions = {}
        self.calls = []
        self._next_id = 5000
        self.fail_on_create = None
        self.actions_readback_override = None

    def request(self, method, url, data=None):
        self.calls.append((method, url, data))
        if method == "GET" and url == "https://api.github.com/user":
            return {"login": self.owner}
        prefix = f"https://api.github.com/repos/{self.owner}/"
        if url.startswith(prefix):
            rest = url[len(prefix):]
            if rest.endswith("/actions/permissions"):
                repo_name = rest[: -len("/actions/permissions")]
                if method == "PUT":
                    self.actions[repo_name] = bool(data.get("enabled"))
                    return None
                if method == "GET":
                    if self.actions_readback_override is not None:
                        return self.actions_readback_override
                    return {"enabled": self.actions.get(repo_name, True)}
            if method == "GET":
                repo = self.repos.get(rest.lower())
                if repo is None:
                    raise _http_error(url, 404)
                return dict(repo)
        create_url = (
            "https://api.github.com/user/repos"
            if self.account_type == "personal"
            else f"https://api.github.com/orgs/{self.owner}/repos"
        )
        if method == "POST" and url == create_url:
            name = data["name"]
            if self.fail_on_create is not None and name == self.fail_on_create:
                raise _http_error(url, 500)
            self._next_id += 1
            repo = {
                "id": self._next_id,
                "name": name,
                "full_name": f"{self.owner}/{name}",
                "private": bool(data["private"]),
                "description": data["description"],
            }
            self.repos[name.lower()] = repo
            return dict(repo)
        raise AssertionError(f"unexpected destination call: {method} {url}")


def _encode(doc):
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


def _base_doc(auto_discoveries, repositories=None, account_type="personal"):
    return {
        "destinations": {"main": {"account_type": account_type}},
        "auto_discoveries": auto_discoveries,
        "repositories": list(repositories or []),
    }


class OwnerResolutionTests(unittest.TestCase):
    def test_github_user_and_organization_resolution(self):
        user_rule = normalize_config(_base_doc([{"url": "https://github.com/Alice"}]))["auto_discoveries"][0]
        api = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 101, "login": "alice", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Alice/repos?type=owner": [
                    {
                        "id": 201,
                        "name": "alpha",
                        "full_name": "alice/alpha",
                        "clone_url": "https://github.com/alice/alpha.git",
                    }
                ]
            },
        )
        owner_info = resolve_owner(user_rule, api)
        self.assertEqual(owner_info, {"id": 101, "kind": "user"})
        self.assertEqual(
            discover_rule_repositories(user_rule, owner_info, api),
            [("alpha", 201, "https://github.com/alice/alpha.git", "alice/alpha")],
        )

        org_rule = normalize_config(_base_doc([{"url": "https://github.com/MyOrg", "id": 202}]))["auto_discoveries"][0]
        org_api = FakeSourceAPI(
            routes={"https://api.github.com/users/MyOrg": {"id": 202, "login": "MyOrg", "type": "Organization"}},
            pages_routes={
                "https://api.github.com/orgs/MyOrg/repos?type=all": [
                    {
                        "id": 301,
                        "name": "repo-one",
                        "full_name": "MyOrg/repo-one",
                        "clone_url": "https://github.com/MyOrg/repo-one.git",
                    }
                ]
            },
        )
        org_info = resolve_owner(org_rule, org_api)
        self.assertEqual(org_info, {"id": 202, "kind": "organization"})
        self.assertEqual(len(discover_rule_repositories(org_rule, org_info, org_api)), 1)

    def test_owner_id_mismatch_fails_closed(self):
        doc = _base_doc([{"url": "https://github.com/Alice", "id": 101}])
        raw = _encode(doc)
        store = FakeStore(raw)
        dst = FakeDestinationAPI()
        src = FakeSourceAPI(routes={"https://api.github.com/users/Alice": {"id": 999, "login": "Alice", "type": "User"}})
        stream = io.StringIO()
        with self.assertRaisesRegex(ValueError, "source owner identity mismatch"):
            onboard_auto_discoveries(raw, store, lambda _: src, dst, owner="dest", account_type="personal", stream=stream)
        self.assertEqual(dst.calls, [])
        self.assertEqual(store.publish_calls, 0)
        self.assertEqual(src.page_requests, [])
        output = stream.getvalue()
        self.assertIn("auto_discovery 1/1 (Alice): FAILED: source owner identity mismatch", output)
        self.assertNotIn("discovered=0", output)
        self.assertNotIn("https://", output)

    def test_forgejo_user_and_org_fallback_only_on_404(self):
        rule = normalize_config(_base_doc([{"url": "https://forge.example/TeamOrg"}]))["auto_discoveries"][0]
        api = FakeSourceAPI(
            routes={"https://forge.example/api/v1/orgs/TeamOrg": {"id": 55, "username": "TeamOrg"}},
            pages_routes={
                "https://forge.example/api/v1/orgs/TeamOrg/repos": [
                    {
                        "id": 77,
                        "name": "tool",
                        "full_name": "TeamOrg/tool",
                        "clone_url": "https://forge.example/TeamOrg/tool.git",
                    }
                ]
            },
        )
        info = resolve_owner(rule, api)
        self.assertEqual(info, {"id": 55, "kind": "organization"})
        self.assertEqual(
            api.requests,
            [
                ("GET", "https://forge.example/api/v1/users/TeamOrg"),
                ("GET", "https://forge.example/api/v1/orgs/TeamOrg"),
            ],
        )
        self.assertEqual(
            discover_rule_repositories(rule, info, api),
            [("tool", 77, "https://forge.example/TeamOrg/tool.git", "TeamOrg/tool")],
        )

        # Non-404 on /users must NOT fall back to /orgs.
        err_api = FakeSourceAPI(
            routes={
                "https://forge.example/api/v1/users/TeamOrg": _http_error(
                    "https://forge.example/api/v1/users/TeamOrg", 500
                ),
                "https://forge.example/api/v1/orgs/TeamOrg": {"id": 55, "username": "TeamOrg"},
            }
        )
        with self.assertRaises(HTTPError) as ctx:
            resolve_owner(rule, err_api)
        self.assertEqual(ctx.exception.code, 500)
        ctx.exception.close()
        self.assertEqual(len(err_api.requests), 1)

        # Forgejo owner id mismatch fails closed.
        pinned_rule = normalize_config(
            _base_doc([{"url": "https://forge.example/Alice", "id": 10}])
        )["auto_discoveries"][0]
        mismatch_api = FakeSourceAPI(
            routes={"https://forge.example/api/v1/users/Alice": {"id": 11, "login": "Alice"}}
        )
        with self.assertRaisesRegex(ValueError, "source owner identity mismatch"):
            resolve_owner(pinned_rule, mismatch_api)

    def test_gitlab_group_and_single_segment_user_namespace(self):
        group_rule = normalize_config(
            _base_doc([{"url": "https://gitlab.com/team/sub", "type": "gitlab", "id": 7}])
        )["auto_discoveries"][0]
        group_api = FakeSourceAPI(
            routes={
                "https://gitlab.com/api/v4/groups/team%2Fsub": {
                    "id": 7,
                    "visibility": "public",
                    "full_path": "team/sub",
                }
            },
            pages_routes={
                "https://gitlab.com/api/v4/groups/team%2Fsub/projects?include_subgroups=false&with_shared=false": [
                    {
                        "id": 701,
                        "path": "proj",
                        "visibility": "public",
                        "path_with_namespace": "team/sub/proj",
                        "http_url_to_repo": "https://gitlab.com/team/sub/proj.git",
                    },
                    {"id": 702, "path": "secret", "visibility": "private"},
                ]
            },
        )
        group_info = resolve_owner(group_rule, group_api)
        self.assertEqual(group_info, {"id": 7, "kind": "group"})
        self.assertEqual(
            discover_rule_repositories(group_rule, group_info, group_api),
            [("proj", 701, "https://gitlab.com/team/sub/proj.git", "team/sub/proj")],
        )

        # Private or full_path-mismatched GitLab group fails closed.
        bad_group = FakeSourceAPI(
            routes={
                "https://gitlab.com/api/v4/groups/team%2Fsub": {
                    "id": 7,
                    "visibility": "private",
                    "full_path": "team/sub",
                }
            }
        )
        with self.assertRaisesRegex(ValueError, "source owner identity mismatch"):
            resolve_owner(group_rule, bad_group)

        # Multi-segment group 404 must NOT fall back to /users.
        missing_group = FakeSourceAPI(
            routes={"https://gitlab.com/api/v4/users?username=team%2Fsub": [{"id": 7, "username": "team/sub"}]}
        )
        with self.assertRaises(HTTPError) as ctx:
            resolve_owner(group_rule, missing_group)
        self.assertEqual(ctx.exception.code, 404)
        ctx.exception.close()

        # Single-segment GitLab user namespace fallback.
        user_rule = normalize_config(
            _base_doc([{"url": "https://gitlab.com/alice", "id": 88}])
        )["auto_discoveries"][0]
        user_api = FakeSourceAPI(
            routes={"https://gitlab.com/api/v4/users?username=alice": [{"id": 88, "username": "alice"}]},
            pages_routes={
                "https://gitlab.com/api/v4/users/88/projects": [
                    {
                        "id": 801,
                        "path": "notes",
                        "visibility": "public",
                        "path_with_namespace": "alice/notes",
                        "http_url_to_repo": "https://gitlab.com/alice/notes.git",
                    },
                    {"id": 802, "path": "priv", "visibility": "private"},
                ]
            },
        )
        user_info = resolve_owner(user_rule, user_api)
        self.assertEqual(user_info, {"id": 88, "kind": "user"})
        self.assertEqual(
            discover_rule_repositories(user_rule, user_info, user_api),
            [("notes", 801, "https://gitlab.com/alice/notes.git", "alice/notes")],
        )


class DiscoveryValidationTests(unittest.TestCase):
    def test_rejects_duplicate_names_duplicate_ids_and_bad_identity(self):
        rule = normalize_config(_base_doc([{"url": "https://github.com/Alice", "id": 1}]))["auto_discoveries"][0]
        owner_info = {"id": 1, "kind": "user"}
        url = "https://api.github.com/users/Alice/repos?type=owner"

        dup_names = FakeSourceAPI(
            pages_routes={
                url: [
                    {"id": 10, "name": "Repo", "full_name": "Alice/Repo", "clone_url": "https://github.com/Alice/Repo.git"},
                    {"id": 11, "name": "repo", "full_name": "Alice/repo", "clone_url": "https://github.com/Alice/repo.git"},
                ]
            }
        )
        with self.assertRaisesRegex(ValueError, "duplicate discovered repository"):
            discover_rule_repositories(rule, owner_info, dup_names)

        dup_ids = FakeSourceAPI(
            pages_routes={
                url: [
                    {"id": 10, "name": "a", "full_name": "Alice/a", "clone_url": "https://github.com/Alice/a.git"},
                    {"id": 10, "name": "b", "full_name": "Alice/b", "clone_url": "https://github.com/Alice/b.git"},
                ]
            }
        )
        with self.assertRaisesRegex(ValueError, "duplicate discovered repository"):
            discover_rule_repositories(rule, owner_info, dup_ids)

        bad_full_name = FakeSourceAPI(
            pages_routes={
                url: [
                    {"id": 10, "name": "a", "full_name": "Evil/a", "clone_url": "https://github.com/Alice/a.git"},
                ]
            }
        )
        with self.assertRaisesRegex(ValueError, "invalid discovered repository metadata"):
            discover_rule_repositories(rule, owner_info, bad_full_name)


class OnboardingBehaviorTests(unittest.TestCase):
    def test_auto_pin_and_inducted_entries_published_in_single_cas_write(self):
        doc = _base_doc(
            [
                {
                    "url": "https://github.com/Alice",
                    "exclude": ["SkipMe"],
                    "overrides": {
                        "beta": {"name": "custom-beta", "visibility": "public", "ignore_releases": True}
                    },
                }
            ]
        )
        raw = _encode(doc)
        store = FakeStore(raw)
        dst = FakeDestinationAPI()
        src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 1234, "login": "Alice", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Alice/repos?type=owner": [
                    {"id": 11, "name": "alpha", "full_name": "Alice/alpha", "clone_url": "https://github.com/Alice/alpha.git"},
                    {"id": 12, "name": "beta", "full_name": "Alice/beta", "clone_url": "https://github.com/Alice/beta.git"},
                    {"id": 13, "name": "skipme", "full_name": "Alice/skipme", "clone_url": "https://github.com/Alice/skipme.git"},
                ]
            },
        )
        stream = io.StringIO()
        result = onboard_auto_discoveries(
            raw, store, lambda _: src, dst, owner="dest", account_type="personal", stream=stream
        )

        self.assertEqual(store.publish_calls, 1)
        published = json.loads(result.config_bytes)
        self.assertEqual(published["auto_discoveries"][0]["id"], 1234)
        self.assertEqual(len(published["repositories"]), 2)
        alpha, beta = published["repositories"]
        self.assertEqual(alpha["source"], {"url": "https://github.com/Alice/alpha", "id": 11})
        self.assertEqual(alpha["mirrors"]["main"]["name"], "mirror-Alice-alpha")
        self.assertEqual(alpha["mirrors"]["main"]["visibility"], "private")
        self.assertNotIn("ignore_releases", alpha["mirrors"]["main"])
        self.assertEqual(alpha["discovered_by"], "https://github.com/Alice")

        self.assertEqual(beta["source"], {"url": "https://github.com/Alice/beta", "id": 12})
        self.assertEqual(beta["mirrors"]["main"]["name"], "custom-beta")
        self.assertEqual(beta["mirrors"]["main"]["visibility"], "public")
        self.assertTrue(beta["mirrors"]["main"]["ignore_releases"])
        self.assertEqual(beta["discovered_by"], "https://github.com/Alice")

        self.assertEqual(dst.actions["mirror-Alice-alpha"], False)
        self.assertEqual(dst.actions["custom-beta"], False)
        post_payloads = [data for method, _, data in dst.calls if method == "POST"]
        self.assertEqual(len(post_payloads), 2)
        self.assertEqual(post_payloads[0]["private"], True)
        self.assertEqual(post_payloads[0]["auto_init"], False)
        self.assertIn("[repo-mirror:initializing:v1]", post_payloads[0]["description"])
        self.assertEqual(post_payloads[1]["private"], False)

        log = stream.getvalue()
        self.assertIn("auto_discovery 1/1 (Alice): OK discovered=3 inducted=2 excluded=1 tracked=0", log)
        self.assertNotIn("https://", log)

        # Steady-state re-run: zero destination writes, zero publish calls, byte recheck still happens.
        dst.calls.clear()
        reads_before = store.read_calls
        steady_stream = io.StringIO()
        steady = onboard_auto_discoveries(
            result.config_bytes, store, lambda _: src, dst, owner="dest", account_type="personal", stream=steady_stream
        )
        self.assertEqual(steady.config_bytes, result.config_bytes)
        self.assertEqual(store.publish_calls, 1)
        self.assertGreater(store.read_calls, reads_before)
        self.assertEqual(dst.calls, [])
        self.assertIn(
            "auto_discovery 1/1 (Alice): OK discovered=3 inducted=0 excluded=1 tracked=2",
            steady_stream.getvalue(),
        )

    def test_zero_discovery_guard_vs_empty_rule_and_all_excluded(self):
        existing_child = {
            "source": {"url": "https://github.com/Alice/alpha", "id": 11},
            "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 5001, "visibility": "private"}},
            "discovered_by": "https://github.com/Alice",
        }
        doc_with_child = _base_doc([{"url": "https://github.com/Alice", "id": 100}], [existing_child])
        raw = _encode(doc_with_child)
        store = FakeStore(raw)
        dst = FakeDestinationAPI()
        empty_src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 100, "login": "Alice", "type": "User"}},
            pages_routes={"https://api.github.com/users/Alice/repos?type=owner": []},
        )
        with self.assertRaisesRegex(
            ValueError, "auto-discovery returned zero repositories for a rule with existing entries"
        ):
            onboard_auto_discoveries(raw, store, lambda _: empty_src, dst, owner="dest", account_type="personal")
        self.assertEqual(dst.calls, [])
        self.assertEqual(store.publish_calls, 0)

        # No existing children + zero repos succeeds (and auto-pins owner id).
        doc_new = _base_doc([{"url": "https://github.com/Alice"}])
        raw_new = _encode(doc_new)
        store_new = FakeStore(raw_new)
        res_new = onboard_auto_discoveries(raw_new, store_new, lambda _: empty_src, dst, owner="dest", account_type="personal")
        self.assertEqual(json.loads(res_new.config_bytes)["auto_discoveries"][0]["id"], 100)
        self.assertEqual(store_new.publish_calls, 1)

        # Non-empty raw discovery where all repos are excluded does NOT trigger zero-discovery guard.
        doc_excluded = _base_doc(
            [{"url": "https://github.com/Alice", "id": 100, "exclude": ["alpha"]}],
            [existing_child],
        )
        raw_excluded = _encode(doc_excluded)
        store_excluded = FakeStore(raw_excluded)
        one_src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 100, "login": "Alice", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Alice/repos?type=owner": [
                    {"id": 11, "name": "alpha", "full_name": "Alice/alpha", "clone_url": "https://github.com/Alice/alpha.git"}
                ]
            },
        )
        res_excluded = onboard_auto_discoveries(
            raw_excluded, store_excluded, lambda _: one_src, dst, owner="dest", account_type="personal"
        )
        self.assertEqual(res_excluded.inducted_total, 0)
        self.assertEqual(res_excluded.orphaned_children, ())

    def test_id_first_rename_matching_and_url_id_conflict(self):
        # Existing child has old name "old-alpha" with source.id=11; upstream now lists "new-alpha" with id=11.
        child = {
            "source": {"url": "https://github.com/Alice/old-alpha", "id": 11},
            "mirrors": {"main": {"name": "mirror-Alice-old-alpha", "id": 5001, "visibility": "private"}},
            "discovered_by": "https://github.com/Alice",
        }
        doc = _base_doc([{"url": "https://github.com/Alice", "id": 100}], [child])
        raw = _encode(doc)
        store = FakeStore(raw)
        dst = FakeDestinationAPI()
        src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 100, "login": "Alice", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Alice/repos?type=owner": [
                    {"id": 11, "name": "new-alpha", "full_name": "Alice/new-alpha", "clone_url": "https://github.com/Alice/new-alpha.git"}
                ]
            },
        )
        res = onboard_auto_discoveries(raw, store, lambda _: src, dst, owner="dest", account_type="personal")
        self.assertEqual(res.inducted_total, 0)
        self.assertEqual(res.orphaned_children, ())
        self.assertEqual(store.publish_calls, 0)
        self.assertEqual(dst.calls, [])
        self.assertIn("tracked=1", res.rule_reports[0])

        # URL match with missing or different source.id fails closed before any writes.
        for bad_source in (
            {"url": "https://github.com/Alice/alpha"},
            {"url": "https://github.com/Alice/alpha", "id": 999},
        ):
            with self.subTest(bad_source=bad_source):
                conflict_doc = _base_doc(
                    [{"url": "https://github.com/Alice", "id": 100}],
                    [{"source": bad_source, "mirrors": {"main": {"name": "custom-alpha", "id": 5001}}}],
                )
                conflict_raw = _encode(conflict_doc)
                conflict_src = FakeSourceAPI(
                    routes={"https://api.github.com/users/Alice": {"id": 100, "login": "Alice", "type": "User"}},
                    pages_routes={
                        "https://api.github.com/users/Alice/repos?type=owner": [
                            {"id": 11, "name": "alpha", "full_name": "Alice/alpha", "clone_url": "https://github.com/Alice/alpha.git"}
                        ]
                    },
                )
                with self.assertRaisesRegex(ValueError, "source repository id mismatch or missing"):
                    onboard_auto_discoveries(
                        conflict_raw,
                        FakeStore(conflict_raw),
                        lambda _: conflict_src,
                        dst,
                        owner="dest",
                        account_type="personal",
                    )

    def test_destination_collision_and_live_existing_refusal(self):
        # Cross-rule planned collision fails before any destination creation.
        doc = _base_doc(
            [
                {"url": "https://github.com/Alice", "id": 1, "destination_name_prefix": "shared"},
                {"url": "https://github.com/Bob", "id": 2, "destination_name_prefix": "SHARED"},
            ]
        )
        raw = _encode(doc)
        dst = FakeDestinationAPI()
        alice_src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 1, "login": "Alice", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Alice/repos?type=owner": [
                    {"id": 11, "name": "repo", "full_name": "Alice/repo", "clone_url": "https://github.com/Alice/repo.git"}
                ]
            },
        )
        bob_src = FakeSourceAPI(
            routes={"https://api.github.com/users/Bob": {"id": 2, "login": "Bob", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Bob/repos?type=owner": [
                    {"id": 22, "name": "Repo", "full_name": "Bob/Repo", "clone_url": "https://github.com/Bob/Repo.git"}
                ]
            },
        )
        with self.assertRaisesRegex(ValueError, "duplicate destination repository"):
            onboard_auto_discoveries(
                raw,
                FakeStore(raw),
                lambda r: alice_src if r["owner"] == "Alice" else bob_src,
                dst,
                owner="dest",
                account_type="personal",
            )
        self.assertEqual(dst.calls, [])

        # Live destination already exists (even with matching marker) -> refuse without adopting or writing.
        doc2 = _base_doc([{"url": "https://github.com/Alice", "id": 1}])
        raw2 = _encode(doc2)
        marker = source_marker({"type": "github", "base_url": "https://github.com", "owner": "Alice"}, "repo")
        dst_existing = FakeDestinationAPI(
            existing={"mirror-alice-repo": {"id": 888, "full_name": "dest/mirror-Alice-repo", "description": marker}}
        )
        with self.assertRaisesRegex(ValueError, "unpinned existing destination requires manual reconciliation"):
            onboard_auto_discoveries(
                raw2, FakeStore(raw2), lambda _: alice_src, dst_existing, owner="dest", account_type="personal"
            )
        self.assertTrue(all(method == "GET" for method, _, _ in dst_existing.calls))

        # Computed destination name > 100 chars fails before any writes.
        long_doc = _base_doc([{"url": "https://github.com/Alice", "id": 1, "destination_name_prefix": "p" * 90}])
        long_raw = _encode(long_doc)
        long_src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 1, "login": "Alice", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Alice/repos?type=owner": [
                    {"id": 11, "name": "r" * 15, "full_name": "Alice/" + ("r" * 15), "clone_url": "https://github.com/Alice/" + ("r" * 15) + ".git"}
                ]
            },
        )
        dst_long = FakeDestinationAPI()
        with self.assertRaisesRegex(ValueError, "invalid destination repository name"):
            onboard_auto_discoveries(
                long_raw, FakeStore(long_raw), lambda _: long_src, dst_long, owner="dest", account_type="personal"
            )
        self.assertEqual(dst_long.calls, [])

        # Collision against legacy.destination_name fails before any writes.
        legacy_doc = _base_doc(
            [{"url": "https://github.com/Alice", "id": 1}],
            [
                {
                    "source": {"url": "https://github.com/Other/moved", "id": 99},
                    "mirrors": {
                        "main": {
                            "name": "mirror-Other-moved",
                            "id": 900,
                            "legacy": {
                                "source_owner": "Alice",
                                "source_name": "repo",
                                "destination_name": "mirror-Alice-repo",
                                "signature": "0" * 64,
                            },
                        }
                    },
                }
            ],
        )
        legacy_raw = _encode(legacy_doc)
        dst_legacy = FakeDestinationAPI()
        with self.assertRaisesRegex(ValueError, "duplicate destination repository"):
            onboard_auto_discoveries(
                legacy_raw, FakeStore(legacy_raw), lambda _: alice_src, dst_legacy, owner="dest", account_type="personal"
            )
        self.assertEqual(dst_legacy.calls, [])

    def test_multi_rule_publishes_once_per_rule(self):
        doc = _base_doc(
            [
                {"url": "https://github.com/Alice"},
                {"url": "https://github.com/Bob"},
            ]
        )
        raw = _encode(doc)
        store = FakeStore(raw)
        dst = FakeDestinationAPI()
        alice_src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 1, "login": "Alice", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Alice/repos?type=owner": [
                    {"id": 11, "name": "a1", "full_name": "Alice/a1", "clone_url": "https://github.com/Alice/a1.git"},
                    {"id": 12, "name": "a2", "full_name": "Alice/a2", "clone_url": "https://github.com/Alice/a2.git"},
                ]
            },
        )
        bob_src = FakeSourceAPI(
            routes={"https://api.github.com/users/Bob": {"id": 2, "login": "Bob", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Bob/repos?type=owner": [
                    {"id": 21, "name": "b1", "full_name": "Bob/b1", "clone_url": "https://github.com/Bob/b1.git"}
                ]
            },
        )
        res = onboard_auto_discoveries(
            raw,
            store,
            lambda r: alice_src if r["owner"] == "Alice" else bob_src,
            dst,
            owner="dest",
            account_type="personal",
        )
        self.assertEqual(store.publish_calls, 2)
        published = json.loads(res.config_bytes)
        self.assertEqual([r["id"] for r in published["auto_discoveries"]], [1, 2])
        self.assertEqual(len(published["repositories"]), 3)

    def test_partial_failure_and_no_adoption_on_retry(self):
        doc = _base_doc([{"url": "https://github.com/Alice", "id": 100}])
        raw = _encode(doc)
        store = FakeStore(raw)
        dst = FakeDestinationAPI()
        dst.fail_on_create = "mirror-Alice-beta"
        src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 100, "login": "Alice", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Alice/repos?type=owner": [
                    {"id": 11, "name": "alpha", "full_name": "Alice/alpha", "clone_url": "https://github.com/Alice/alpha.git"},
                    {"id": 12, "name": "beta", "full_name": "Alice/beta", "clone_url": "https://github.com/Alice/beta.git"},
                ]
            },
        )
        with self.assertRaises(PartialOnboarding) as ctx:
            onboard_auto_discoveries(raw, store, lambda _: src, dst, owner="dest", account_type="personal")
        self.assertIsInstance(ctx.exception, OnboardingPublicationError)
        self.assertEqual(ctx.exception.names, ("mirror-Alice-alpha", "mirror-Alice-beta"))
        self.assertEqual(store.data, raw)
        self.assertEqual(store.publish_calls, 0)
        # No rollback/delete: alpha remains remotely.
        self.assertIn("mirror-alice-alpha", dst.repos)
        self.assertFalse(any(method == "DELETE" for method, _, _ in dst.calls))

        # Retry refuses to adopt the unpinned remote destination.
        dst.fail_on_create = None
        with self.assertRaisesRegex(ValueError, "unpinned existing destination requires manual reconciliation"):
            onboard_auto_discoveries(raw, store, lambda _: src, dst, owner="dest", account_type="personal")

    def test_self_hosted_gitlab_and_orphan_diagnostics(self):
        orphan_child = {
            "source": {"type": "gitlab", "url": "https://code.example/team/sub/gone", "id": 901},
            "mirrors": {"main": {"name": "pref-gone", "id": 7001, "visibility": "private"}},
            "discovered_by": "https://code.example/team/sub",
        }
        legacy_orphan = {
            "source": {"url": "https://gitlab.com/old/group/legacy", "id": 902},
            "mirrors": {"main": {"name": "gitlab-" + ("ab" * 12), "id": 7002}},
        }
        doc = _base_doc(
            [
                {
                    "url": "https://code.example/team/sub",
                    "type": "gitlab",
                    "id": 7,
                    "destination_name_prefix": "pref",
                }
            ],
            [orphan_child, legacy_orphan],
        )
        raw = _encode(doc)
        store = FakeStore(raw)
        dst = FakeDestinationAPI()
        src = FakeSourceAPI(
            routes={
                "https://code.example/api/v4/groups/team%2Fsub": {
                    "id": 7,
                    "visibility": "public",
                    "full_path": "team/sub",
                }
            },
            pages_routes={
                "https://code.example/api/v4/groups/team%2Fsub/projects?include_subgroups=false&with_shared=false": [
                    {
                        "id": 903,
                        "path": "kept",
                        "visibility": "public",
                        "path_with_namespace": "team/sub/kept",
                        "http_url_to_repo": "https://code.example/team/sub/kept.git",
                    }
                ]
            },
        )
        stream = io.StringIO()
        res = onboard_auto_discoveries(
            raw, store, lambda _: src, dst, owner="dest", account_type="personal", stream=stream
        )
        published = json.loads(res.config_bytes)
        new_entry = published["repositories"][-1]
        self.assertEqual(new_entry["source"]["type"], "gitlab")
        self.assertEqual(new_entry["mirrors"]["main"]["name"], "pref-kept")
        self.assertIn(
            "auto_discovery 1/1 (team/sub): OK discovered=1 inducted=1 excluded=0 tracked=0 missing_upstream=1",
            stream.getvalue(),
        )
        orphan_stream = io.StringIO()
        report_orphans(res, stream=orphan_stream)
        orphan_out = orphan_stream.getvalue()
        self.assertIn("orphaned child repository team/sub/gone: destination pref-gone untouched", orphan_out)
        self.assertIn("orphaned legacy repository old/group/legacy: destination", orphan_out)
        self.assertNotIn("https://", stream.getvalue() + orphan_out)


class CliIntegrationTests(unittest.TestCase):
    def test_cli_onboards_mirrors_and_steady_state_reruns(self):
        doc = _base_doc([{"url": "https://github.com/Alice"}])
        raw = _encode(doc)
        store = FakeStore(raw)
        dst = FakeDestinationAPI()
        src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 100, "login": "Alice", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Alice/repos?type=owner": [
                    {"id": 11, "name": "alpha", "full_name": "Alice/alpha", "clone_url": "https://github.com/Alice/alpha.git"}
                ]
            },
        )
        mirrored = []

        def fake_mirror(source, name, owner, *args, **kwargs):
            mirrored.append((name, source["destinations"][name.lower()]["source_id"], source["destinations"][name.lower()]["destination_id"]))
            return None

        env = {"GH_TOKEN": "token", "DESTINATION_OWNER": "dest", "MIRROR_TIMESTAMP_SECRET": "secret"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(raw)
            with (
                patch.dict(os.environ, env, clear=True),
                patch("repo_mirror.gitlab_onboarding_github.store_from_environment", return_value=store),
                patch("repo_mirror.github_migration.transaction_from_environment", return_value=None),
                patch.object(cli, "SourceAPI", return_value=src),
                patch.object(cli, "API", side_effect=[Mock(), dst, Mock(), dst]),
                patch.object(cli, "mirror_repository", side_effect=fake_mirror),
            ):
                cli.main(["--config", str(path)])
                self.assertEqual(store.publish_calls, 1)
                self.assertEqual(len(mirrored), 1)
                self.assertEqual(mirrored[0][1], 11)
                published_id = mirrored[0][2]

                # Steady-state second run with published config bytes: zero destination writes, zero publish calls.
                path.write_bytes(store.data)
                dst.calls.clear()
                cli.main(["--config", str(path)])
                self.assertEqual(store.publish_calls, 1)
                self.assertTrue(all(method == "GET" for method, _, _ in dst.calls))
                self.assertEqual(mirrored, [("alpha", 11, published_id), ("alpha", 11, published_id)])

    def test_cli_refuses_onboarding_when_pending_migrations_remain(self):
        doc = _base_doc([{"url": "https://github.com/Alice"}])
        raw = _encode(doc)
        pending_tx = Mock()
        pending_tx.recover.return_value = 0
        pending_tx.store.pending.return_value = {"k": {"phase": "prepared"}}
        env = {"GH_TOKEN": "token", "DESTINATION_OWNER": "dest", "MIRROR_TIMESTAMP_SECRET": "secret"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(raw)
            with (
                patch.dict(os.environ, env, clear=True),
                patch("repo_mirror.github_migration.transaction_from_environment", return_value=pending_tx),
                patch.object(cli, "SourceAPI", return_value=Mock()),
                patch.object(cli, "API", return_value=Mock()),
            ):
                with self.assertRaisesRegex(ValueError, "pending migration requires reconciliation"):
                    cli.main(["--config", str(path)])


if __name__ == "__main__":
    unittest.main()
