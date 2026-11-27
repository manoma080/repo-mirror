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
    OnboardingPublicationError,
    PartialOnboarding,
    _plan_rule,
    discover_rule_repositories,
    onboard_auto_discoveries,
    report_orphans,
    resolve_owner,
)
from repo_mirror.config_named import normalize_config
from repo_mirror.git_sync import archive_name
from repo_mirror.logging_utils import safe_label
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
        self._next_id = max([5000, *(r.get("id", 0) for r in self.repos.values() if isinstance(r, dict))])
        self.fail_on_create = None
        self.fail_on_patch = None
        self.patch_response_override = None
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
            if method == "PATCH":
                if self.fail_on_patch is not None and (self.fail_on_patch is True or rest == self.fail_on_patch):
                    raise _http_error(url, 500)
                repo = self.repos.get(rest.lower())
                if repo is None:
                    raise _http_error(url, 404)
                new_name = data["name"]
                if self.patch_response_override is not None:
                    if callable(self.patch_response_override):
                        return self.patch_response_override(rest, new_name, dict(repo))
                    return dict(self.patch_response_override)
                del self.repos[rest.lower()]
                updated = dict(repo)
                updated["name"] = new_name
                updated["full_name"] = f"{self.owner}/{new_name}"
                self.repos[new_name.lower()] = updated
                return dict(updated)
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
        "graphql": False,
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
        self.assertIn("auto_discovery 1/1 (Alice): OK discovered=3 inducted=2 excluded=1 tracked=0 conflicts=0", log)
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
            "auto_discovery 1/1 (Alice): OK discovered=3 inducted=0 excluded=1 tracked=2 conflicts=0",
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

        # URL match with missing source.id still fails closed; mismatched pinned source.id becomes a per-repository conflict.
        missing_doc = _base_doc(
            [{"url": "https://github.com/Alice", "id": 100}],
            [{"source": {"url": "https://github.com/Alice/alpha"}, "mirrors": {"main": {"name": "custom-alpha", "id": 5001}}}],
        )
        missing_raw = _encode(missing_doc)
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
                missing_raw,
                FakeStore(missing_raw),
                lambda _: conflict_src,
                dst,
                owner="dest",
                account_type="personal",
            )

        mismatch_doc = _base_doc(
            [{"url": "https://github.com/Alice", "id": 100}],
            [{"source": {"url": "https://github.com/Alice/alpha", "id": 999}, "mirrors": {"main": {"name": "custom-alpha", "id": 5001}}}],
        )
        mismatch_raw = _encode(mismatch_doc)
        mismatch_store = FakeStore(mismatch_raw)
        mismatch_stream = io.StringIO()
        mismatch_res = onboard_auto_discoveries(
            mismatch_raw,
            mismatch_store,
            lambda _: conflict_src,
            dst,
            owner="dest",
            account_type="personal",
            stream=mismatch_stream,
        )
        self.assertEqual(mismatch_store.publish_calls, 0)
        self.assertEqual(dst.calls, [])
        self.assertEqual(len(mismatch_res.conflicts), 1)
        self.assertIn(
            "auto_discovery 1/1 (Alice): repository alpha: upstream identity changed (pinned id 999, now 11); "
            "entry kept, not mirrored; remove the entry and re-add it to adopt",
            mismatch_stream.getvalue(),
        )
        self.assertIn(
            "auto_discovery 1/1 (Alice): OK discovered=1 inducted=0 excluded=0 tracked=0 conflicts=1",
            mismatch_stream.getvalue(),
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
            "auto_discovery 1/1 (team/sub): OK discovered=1 inducted=1 excluded=0 tracked=0 missing_upstream=1 conflicts=0",
            stream.getvalue(),
        )
        orphan_stream = io.StringIO()
        report_orphans(res, stream=orphan_stream)
        orphan_out = orphan_stream.getvalue()
        self.assertIn("orphaned child repository team/sub/gone: destination pref-gone untouched", orphan_out)
        self.assertIn("orphaned legacy repository old/group/legacy: destination", orphan_out)
        self.assertNotIn("https://", stream.getvalue() + orphan_out)

    def test_deleted_and_recreated_upstream_reports_conflict_and_continues_rule(self):
        existing_conflict = {
            "source": {"url": "https://github.com/example-owner/example-app", "id": 1470348900},
            "mirrors": {"main": {"name": "mirror-example-owner-example-app", "id": 5001, "visibility": "private"}},
            "discovered_by": "https://github.com/example-owner",
        }
        existing_tracked = {
            "source": {"url": "https://github.com/example-owner/tracked-repo", "id": 1001},
            "mirrors": {"main": {"name": "mirror-example-owner-tracked-repo", "id": 5002, "visibility": "private"}},
            "discovered_by": "https://github.com/example-owner",
        }
        doc = _base_doc(
            [{"url": "https://github.com/example-owner", "id": 42}],
            [existing_conflict, existing_tracked],
        )
        raw = _encode(doc)
        store = FakeStore(raw)
        dst = FakeDestinationAPI()
        src = FakeSourceAPI(
            routes={
                "https://api.github.com/users/example-owner": {"id": 42, "login": "example-owner", "type": "User"},
                "https://api.github.com/repositories/1470348900": {
                    "id": 1470348900,
                    "full_name": "example-owner/moved-elsewhere",
                },
            },
            pages_routes={
                "https://api.github.com/users/example-owner/repos?type=owner": [
                    {
                        "id": 1472677129,
                        "name": "example-app",
                        "full_name": "example-owner/example-app",
                        "clone_url": "https://github.com/example-owner/example-app.git",
                    },
                    {
                        "id": 1001,
                        "name": "tracked-repo",
                        "full_name": "example-owner/tracked-repo",
                        "clone_url": "https://github.com/example-owner/tracked-repo.git",
                    },
                    {
                        "id": 1002,
                        "name": "new-repo",
                        "full_name": "example-owner/new-repo",
                        "clone_url": "https://github.com/example-owner/new-repo.git",
                    },
                ]
            },
        )
        stream = io.StringIO()
        res = onboard_auto_discoveries(
            raw, store, lambda _: src, dst, owner="dest", account_type="personal", stream=stream
        )
        self.assertEqual(store.publish_calls, 1)
        self.assertEqual(res.inducted_total, 1)
        self.assertEqual(res.orphaned_children, ())
        self.assertEqual(len(res.conflicts), 1)
        published = json.loads(res.config_bytes)
        self.assertEqual(len(published["repositories"]), 3)
        by_url = {r["source"]["url"]: r for r in published["repositories"]}
        self.assertEqual(
            by_url["https://github.com/example-owner/example-app"]["source"]["id"],
            1470348900,
        )
        self.assertIn("https://github.com/example-owner/new-repo", by_url)
        expected_conflict_line = (
            "auto_discovery 1/1 (example-owner): repository example-app: "
            "upstream identity changed (pinned id 1470348900, now 1472677129); "
            "entry kept, not mirrored; remove the entry and re-add it to adopt"
        )
        log = stream.getvalue()
        self.assertEqual(log.count(expected_conflict_line), 1)
        self.assertIn(
            "auto_discovery 1/1 (example-owner): OK discovered=3 inducted=1 excluded=0 tracked=1 conflicts=1 adopted=0",
            log,
        )
        self.assertNotIn("https://", log)

        # Second run reports the same conflict and stays no-op otherwise.
        dst.calls.clear()
        stream2 = io.StringIO()
        res2 = onboard_auto_discoveries(
            res.config_bytes, store, lambda _: src, dst, owner="dest", account_type="personal", stream=stream2
        )
        self.assertEqual(res2.config_bytes, res.config_bytes)
        self.assertEqual(store.publish_calls, 1)
        self.assertEqual(dst.calls, [])
        self.assertEqual(res2.orphaned_children, ())
        self.assertEqual(len(res2.conflicts), 1)
        log2 = stream2.getvalue()
        self.assertEqual(log2.count(expected_conflict_line), 1)
        self.assertIn(
            "auto_discovery 1/1 (example-owner): OK discovered=3 inducted=0 excluded=0 tracked=2 conflicts=1 adopted=0",
            log2,
        )

    def test_duplicate_id_and_ambiguous_url_still_fail_closed(self):
        # Two existing entries sharing the discovered source.id still raise.
        dup_id_doc = _base_doc(
            [{"url": "https://github.com/Alice", "id": 100}],
            [
                {
                    "source": {"url": "https://github.com/Alice/old-a", "id": 11},
                    "mirrors": {"main": {"name": "mirror-Alice-old-a", "id": 5001}},
                    "discovered_by": "https://github.com/Alice",
                },
                {
                    "source": {"url": "https://github.com/Other/alpha", "id": 11},
                    "mirrors": {"main": {"name": "custom-b", "id": 5002}},
                },
            ],
        )
        dup_id_raw = _encode(dup_id_doc)
        dup_id_store = FakeStore(dup_id_raw)
        dst = FakeDestinationAPI()
        alice_src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 100, "login": "Alice", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Alice/repos?type=owner": [
                    {"id": 11, "name": "alpha", "full_name": "Alice/alpha", "clone_url": "https://github.com/Alice/alpha.git"}
                ]
            },
        )
        with self.assertRaisesRegex(ValueError, "existing source mapping conflicts"):
            onboard_auto_discoveries(
                dup_id_raw, dup_id_store, lambda _: alice_src, dst, owner="dest", account_type="personal"
            )
        self.assertEqual(dup_id_store.publish_calls, 0)
        self.assertEqual(dst.calls, [])

        # Ambiguous URL match (relocated child + standalone entry matching the discovered URL) still raises.
        ambig_url_doc = _base_doc(
            [{"url": "https://github.com/Alice", "id": 100}],
            [
                {
                    "source": {"url": "https://github.com/Alice/alpha", "id": 999},
                    "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 5001}},
                    "discovered_by": "https://github.com/Alice",
                },
                {
                    "source": {"url": "https://github.com/AliceNew/alpha", "id": 888},
                    "mirrors": {"main": {"name": "custom-new-alpha", "id": 5002}},
                },
            ],
        )
        ambig_url_raw = _encode(ambig_url_doc)
        ambig_url_store = FakeStore(ambig_url_raw)
        reloc_src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 100, "login": "AliceNew", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/AliceNew/repos?type=owner": [
                    {"id": 11, "name": "alpha", "full_name": "AliceNew/alpha", "clone_url": "https://github.com/AliceNew/alpha.git"}
                ]
            },
        )
        with self.assertRaisesRegex(ValueError, "existing source mapping conflicts"):
            onboard_auto_discoveries(
                ambig_url_raw, ambig_url_store, lambda _: reloc_src, dst, owner="dest", account_type="personal"
            )
        self.assertEqual(ambig_url_store.publish_calls, 0)
        self.assertEqual(dst.calls, [])

        # One entry matching discovered source.id and another matching discovered URL still raises.
        split_doc = _base_doc(
            [{"url": "https://github.com/Alice", "id": 100}],
            [
                {
                    "source": {"url": "https://github.com/Alice/old-alpha", "id": 11},
                    "mirrors": {"main": {"name": "mirror-Alice-old-alpha", "id": 5001}},
                    "discovered_by": "https://github.com/Alice",
                },
                {
                    "source": {"url": "https://github.com/Alice/alpha", "id": 999},
                    "mirrors": {"main": {"name": "custom-alpha", "id": 5002}},
                },
            ],
        )
        split_raw = _encode(split_doc)
        split_store = FakeStore(split_raw)
        with self.assertRaisesRegex(ValueError, "existing source mapping conflicts"):
            onboard_auto_discoveries(
                split_raw, split_store, lambda _: alice_src, dst, owner="dest", account_type="personal"
            )
        self.assertEqual(split_store.publish_calls, 0)
        self.assertEqual(dst.calls, [])


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
            mirrored.append((name, source["mirror"]["source_id"], source["mirror"]["destination_id"]))
            return None

        env = {"GH_TOKEN": "token", "DESTINATION_OWNER": "dest", "MIRROR_TIMESTAMP_SECRET": "secret"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(raw)
            with (
                patch.dict(os.environ, env, clear=True),
                patch("repo_mirror.github_migration.store_from_environment", return_value=store),
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

    def test_cli_skips_conflicted_repository_and_mirrors_other_repositories(self):
        from contextlib import redirect_stderr

        doc = _base_doc(
            [{"url": "https://github.com/example-owner", "id": 42}],
            [
                {
                    "source": {"url": "https://github.com/example-owner/example-app", "id": 1470348900},
                    "mirrors": {"main": {"name": "mirror-example-owner-example-app", "id": 5001}},
                    "discovered_by": "https://github.com/example-owner",
                },
                {
                    "source": {"url": "https://github.com/example-owner/good-repo", "id": 2001},
                    "mirrors": {"main": {"name": "mirror-example-owner-good-repo", "id": 5002}},
                    "discovered_by": "https://github.com/example-owner",
                },
                {
                    "source": {"url": "https://github.com/Other/manual-repo", "id": 3001},
                    "mirrors": {"main": {"name": "mirror-Other-manual-repo", "id": 5003}},
                },
            ],
        )
        raw = _encode(doc)
        store = FakeStore(raw)
        dst = FakeDestinationAPI()
        src = FakeSourceAPI(
            routes={
                "https://api.github.com/users/example-owner": {"id": 42, "login": "example-owner", "type": "User"},
                "https://api.github.com/repositories/1470348900": {
                    "id": 1470348900,
                    "full_name": "example-owner/moved-elsewhere",
                },
            },
            pages_routes={
                "https://api.github.com/users/example-owner/repos?type=owner": [
                    {
                        "id": 1472677129,
                        "name": "example-app",
                        "full_name": "example-owner/example-app",
                        "clone_url": "https://github.com/example-owner/example-app.git",
                    },
                    {
                        "id": 2001,
                        "name": "good-repo",
                        "full_name": "example-owner/good-repo",
                        "clone_url": "https://github.com/example-owner/good-repo.git",
                    },
                ]
            },
        )
        mirrored = []

        def fake_mirror(source, name, owner, *args, **kwargs):
            mirrored.append((source["owner"], name, source["mirror"]["source_id"]))
            return None

        env = {"GH_TOKEN": "token", "DESTINATION_OWNER": "dest", "MIRROR_TIMESTAMP_SECRET": "secret"}
        stderr_buf = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(raw)
            with (
                patch.dict(os.environ, env, clear=True),
                patch("repo_mirror.github_migration.store_from_environment", return_value=store),
                patch("repo_mirror.github_migration.transaction_from_environment", return_value=None),
                patch.object(cli, "SourceAPI", return_value=src),
                patch.object(cli, "API", side_effect=[Mock(), dst]),
                patch.object(cli, "mirror_repository", side_effect=fake_mirror),
                redirect_stderr(stderr_buf),
            ):
                cli.main(["--config", str(path)])

        self.assertEqual(store.publish_calls, 0)
        self.assertEqual(
            sorted(mirrored),
            [("Other", "manual-repo", 3001), ("example-owner", "good-repo", 2001)],
        )
        stderr_out = stderr_buf.getvalue()
        self.assertIn(
            "auto_discovery 1/1 (example-owner): repository example-app: "
            "upstream identity changed (pinned id 1470348900, now 1472677129); "
            "entry kept, not mirrored; remove the entry and re-add it to adopt",
            stderr_out,
        )
        self.assertIn(
            "auto_discovery 1/1 (example-owner): OK discovered=2 inducted=0 excluded=0 tracked=1 conflicts=1 adopted=0",
            stderr_out,
        )
        self.assertIn("summary sources=3 discovery_failed=0 repositories=2 succeeded=2 failed=0", stderr_out)

    def test_cli_still_fails_when_non_conflicted_repository_mirror_fails(self):
        from contextlib import redirect_stderr

        doc = _base_doc(
            [{"url": "https://github.com/example-owner", "id": 42}],
            [
                {
                    "source": {"url": "https://github.com/example-owner/example-app", "id": 1470348900},
                    "mirrors": {"main": {"name": "mirror-example-owner-example-app", "id": 5001}},
                    "discovered_by": "https://github.com/example-owner",
                },
                {
                    "source": {"url": "https://github.com/example-owner/good-repo", "id": 2001},
                    "mirrors": {"main": {"name": "mirror-example-owner-good-repo", "id": 5002}},
                    "discovered_by": "https://github.com/example-owner",
                },
                {
                    "source": {"url": "https://github.com/Other/manual-repo", "id": 3001},
                    "mirrors": {"main": {"name": "mirror-Other-manual-repo", "id": 5003}},
                },
            ],
        )
        raw = _encode(doc)
        store = FakeStore(raw)
        dst = FakeDestinationAPI()
        src = FakeSourceAPI(
            routes={
                "https://api.github.com/users/example-owner": {"id": 42, "login": "example-owner", "type": "User"},
                "https://api.github.com/repositories/1470348900": {
                    "id": 1470348900,
                    "full_name": "example-owner/moved-elsewhere",
                },
            },
            pages_routes={
                "https://api.github.com/users/example-owner/repos?type=owner": [
                    {
                        "id": 1472677129,
                        "name": "example-app",
                        "full_name": "example-owner/example-app",
                        "clone_url": "https://github.com/example-owner/example-app.git",
                    },
                    {
                        "id": 2001,
                        "name": "good-repo",
                        "full_name": "example-owner/good-repo",
                        "clone_url": "https://github.com/example-owner/good-repo.git",
                    },
                ]
            },
        )
        mirrored = []

        def fake_mirror(source, name, owner, *args, **kwargs):
            mirrored.append(name)
            if name == "manual-repo":
                raise ValueError("source repository id mismatch")
            return None

        env = {"GH_TOKEN": "token", "DESTINATION_OWNER": "dest", "MIRROR_TIMESTAMP_SECRET": "secret"}
        stderr_buf = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(raw)
            with (
                patch.dict(os.environ, env, clear=True),
                patch("repo_mirror.github_migration.store_from_environment", return_value=store),
                patch("repo_mirror.github_migration.transaction_from_environment", return_value=None),
                patch.object(cli, "SourceAPI", return_value=src),
                patch.object(cli, "API", side_effect=[Mock(), dst]),
                patch.object(cli, "mirror_repository", side_effect=fake_mirror),
                redirect_stderr(stderr_buf),
            ):
                with self.assertRaisesRegex(RuntimeError, "0 sources and 1 repositories failed"):
                    cli.main(["--config", str(path)])

        self.assertEqual(sorted(mirrored), ["good-repo", "manual-repo"])
        self.assertIn("summary sources=3 discovery_failed=0 repositories=2 succeeded=1 failed=1", stderr_buf.getvalue())


class RuleRelocationTests(unittest.TestCase):
    def test_github_relocation_rewrites_rule_and_children_in_one_cas_write_and_steady_state(self):
        child = {
            "source": {"url": "https://github.com/Alice/alpha", "id": 11},
            "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 5001, "visibility": "private"}},
            "discovered_by": "https://github.com/Alice",
        }
        doc = _base_doc([{"url": "https://github.com/Alice", "id": 100}], [child])
        raw = _encode(doc)
        store = FakeStore(raw)
        dst = FakeDestinationAPI()
        src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 100, "login": "AliceNew", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/AliceNew/repos?type=owner": [
                    {
                        "id": 11,
                        "name": "alpha",
                        "full_name": "AliceNew/alpha",
                        "clone_url": "https://github.com/AliceNew/alpha.git",
                    },
                    {
                        "id": 12,
                        "name": "beta",
                        "full_name": "AliceNew/beta",
                        "clone_url": "https://github.com/AliceNew/beta.git",
                    },
                ]
            },
        )
        stream = io.StringIO()
        res = onboard_auto_discoveries(
            raw, store, lambda _: src, dst, owner="dest", account_type="personal", stream=stream
        )
        self.assertEqual(store.publish_calls, 1)
        self.assertEqual(res.inducted_total, 1)
        self.assertEqual(res.relocated_rules, (("https://github.com/Alice", "https://github.com/AliceNew"),))

        published = json.loads(res.config_bytes)
        rule = published["auto_discoveries"][0]
        self.assertEqual(rule["url"], "https://github.com/AliceNew")
        self.assertEqual(rule["id"], 100)
        self.assertEqual(rule["destination_name_prefix"], "mirror-Alice")

        relocated_alpha, inducted_beta = published["repositories"]
        self.assertEqual(relocated_alpha["source"], {"url": "https://github.com/AliceNew/alpha", "id": 11})
        self.assertEqual(relocated_alpha["mirrors"]["main"], {"name": "mirror-Alice-alpha", "id": 5001, "visibility": "private"})
        self.assertEqual(relocated_alpha["discovered_by"], "https://github.com/AliceNew")

        self.assertEqual(inducted_beta["source"], {"url": "https://github.com/AliceNew/beta", "id": 12})
        self.assertEqual(inducted_beta["mirrors"]["main"]["name"], "mirror-Alice-beta")
        self.assertEqual(inducted_beta["discovered_by"], "https://github.com/AliceNew")

        log = stream.getvalue()
        self.assertIn("auto_discovery 1/1 (Alice): relocated to AliceNew", log)
        self.assertIn("auto_discovery 1/1 (AliceNew): OK discovered=2 inducted=1 excluded=0 tracked=1 conflicts=0", log)
        self.assertNotIn("https://", log)

        # Steady-state re-run after relocation is a complete no-op.
        dst.calls.clear()
        steady_src = FakeSourceAPI(
            routes={"https://api.github.com/users/AliceNew": {"id": 100, "login": "AliceNew", "type": "User"}},
            pages_routes=src.pages_routes,
        )
        steady_stream = io.StringIO()
        steady = onboard_auto_discoveries(
            res.config_bytes,
            store,
            lambda _: steady_src,
            dst,
            owner="dest",
            account_type="personal",
            stream=steady_stream,
        )
        self.assertEqual(steady.config_bytes, res.config_bytes)
        self.assertEqual(steady.relocated_rules, ())
        self.assertEqual(store.publish_calls, 1)
        self.assertEqual(dst.calls, [])
        self.assertNotIn("relocated to", steady_stream.getvalue())
        self.assertIn("auto_discovery 1/1 (AliceNew): OK discovered=2 inducted=0 excluded=0 tracked=2 conflicts=0", steady_stream.getvalue())

    def test_forgejo_and_gitlab_relocations_preserve_explicit_mirror_names(self):
        # Forgejo org rename
        forgejo_doc = _base_doc(
            [{"url": "https://forge.example/OldOrg", "id": 55, "destination_name_prefix": "custom"}],
            [
                {
                    "source": {"url": "https://forge.example/OldOrg/tool", "id": 77},
                    "mirrors": {"main": {"name": "custom-tool", "id": 5001, "visibility": "private"}},
                    "discovered_by": "https://forge.example/OldOrg",
                }
            ],
        )
        forgejo_raw = _encode(forgejo_doc)
        forgejo_store = FakeStore(forgejo_raw)
        forgejo_src = FakeSourceAPI(
            routes={"https://forge.example/api/v1/orgs/OldOrg": {"id": 55, "name": "NewOrg"}},
            pages_routes={
                "https://forge.example/api/v1/orgs/NewOrg/repos": [
                    {
                        "id": 77,
                        "name": "tool",
                        "full_name": "NewOrg/tool",
                        "clone_url": "https://forge.example/NewOrg/tool.git",
                    }
                ]
            },
        )
        stream = io.StringIO()
        res = onboard_auto_discoveries(
            forgejo_raw,
            forgejo_store,
            lambda _: forgejo_src,
            FakeDestinationAPI(),
            owner="dest",
            account_type="personal",
            stream=stream,
        )
        self.assertEqual(forgejo_store.publish_calls, 1)
        published = json.loads(res.config_bytes)
        self.assertEqual(published["auto_discoveries"][0]["url"], "https://forge.example/NewOrg")
        self.assertEqual(published["repositories"][0]["source"]["url"], "https://forge.example/NewOrg/tool")
        self.assertEqual(published["repositories"][0]["mirrors"]["main"]["name"], "custom-tool")
        self.assertIn("auto_discovery 1/1 (OldOrg): relocated to NewOrg", stream.getvalue())

        # GitLab group full_path rename
        gl_doc = _base_doc(
            [{"url": "https://gitlab.com/old-team/sub", "type": "gitlab", "id": 7}],
            [
                {
                    "source": {"url": "https://gitlab.com/old-team/sub/proj", "id": 701},
                    "mirrors": {"main": {"name": "mirror-old-team-sub-proj", "id": 6001, "visibility": "private"}},
                    "discovered_by": "https://gitlab.com/old-team/sub",
                }
            ],
        )
        gl_raw = _encode(gl_doc)
        gl_store = FakeStore(gl_raw)
        gl_src = FakeSourceAPI(
            routes={
                "https://gitlab.com/api/v4/groups/old-team%2Fsub": {
                    "id": 7,
                    "visibility": "public",
                    "full_path": "new-team/sub",
                }
            },
            pages_routes={
                "https://gitlab.com/api/v4/groups/new-team%2Fsub/projects?include_subgroups=false&with_shared=false": [
                    {
                        "id": 701,
                        "path": "proj",
                        "visibility": "public",
                        "path_with_namespace": "new-team/sub/proj",
                        "http_url_to_repo": "https://gitlab.com/new-team/sub/proj.git",
                    }
                ]
            },
        )
        gl_stream = io.StringIO()
        gl_res = onboard_auto_discoveries(
            gl_raw,
            gl_store,
            lambda _: gl_src,
            FakeDestinationAPI(),
            owner="dest",
            account_type="personal",
            stream=gl_stream,
        )
        self.assertEqual(gl_store.publish_calls, 1)
        gl_pub = json.loads(gl_res.config_bytes)
        self.assertEqual(gl_pub["auto_discoveries"][0]["url"], "https://gitlab.com/new-team/sub")
        self.assertEqual(gl_pub["auto_discoveries"][0]["destination_name_prefix"], "mirror-old-team-sub")
        self.assertEqual(gl_pub["repositories"][0]["source"]["url"], "https://gitlab.com/new-team/sub/proj")
        self.assertEqual(gl_pub["repositories"][0]["mirrors"]["main"]["name"], "mirror-old-team-sub-proj")
        self.assertIn("auto_discovery 1/1 (old-team/sub): relocated to new-team/sub", gl_stream.getvalue())

    def test_id_mismatch_and_unpinned_login_mismatch_still_fail_closed(self):
        # Pinned id mismatch with different login fails closed.
        doc = _base_doc([{"url": "https://github.com/Alice", "id": 100}])
        raw = _encode(doc)
        store = FakeStore(raw)
        dst = FakeDestinationAPI()
        src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 999, "login": "AliceNew", "type": "User"}}
        )
        with self.assertRaisesRegex(ValueError, "source owner identity mismatch"):
            onboard_auto_discoveries(raw, store, lambda _: src, dst, owner="dest", account_type="personal")
        self.assertEqual(store.publish_calls, 0)
        self.assertEqual(dst.calls, [])

        # Unpinned rule cannot relocate on login mismatch.
        unpinned = _base_doc([{"url": "https://github.com/Alice"}])
        unpinned_raw = _encode(unpinned)
        unpinned_src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 100, "login": "AliceNew", "type": "User"}}
        )
        with self.assertRaisesRegex(ValueError, "source owner identity mismatch"):
            onboard_auto_discoveries(
                unpinned_raw, FakeStore(unpinned_raw), lambda _: unpinned_src, dst, owner="dest", account_type="personal"
            )
        self.assertEqual(dst.calls, [])

    def test_relocation_computed_before_404_preflight(self):
        # Alice relocates to Bob when a Bob rule already exists -> fails during prevalidation before any 404 GETs.
        doc = _base_doc(
            [
                {"url": "https://github.com/Alice", "id": 100},
                {"url": "https://github.com/Bob", "id": 200},
            ]
        )
        raw = _encode(doc)
        store = FakeStore(raw)
        dst = FakeDestinationAPI()
        alice_src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 100, "login": "Bob", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Bob/repos?type=owner": [
                    {"id": 11, "name": "alpha", "full_name": "Bob/alpha", "clone_url": "https://github.com/Bob/alpha.git"}
                ]
            },
        )
        bob_src = FakeSourceAPI(
            routes={"https://api.github.com/users/Bob": {"id": 200, "login": "Bob", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Bob/repos?type=owner": [
                    {"id": 21, "name": "beta", "full_name": "Bob/beta", "clone_url": "https://github.com/Bob/beta.git"}
                ]
            },
        )
        with self.assertRaisesRegex(ValueError, "duplicate auto-discovery rule"):
            onboard_auto_discoveries(
                raw,
                store,
                lambda r: alice_src if r["owner"] == "Alice" else bob_src,
                dst,
                owner="dest",
                account_type="personal",
            )
        self.assertEqual(dst.calls, [])
        self.assertEqual(store.publish_calls, 0)

    def test_no_relocation_when_nothing_moved(self):
        # Case-only difference on GitHub (Alice vs alice) is not a relocation.
        child = {
            "source": {"url": "https://github.com/Alice/alpha", "id": 11},
            "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 5001, "visibility": "private"}},
            "discovered_by": "https://github.com/Alice",
        }
        doc = _base_doc([{"url": "https://github.com/Alice", "id": 100}], [child])
        raw = _encode(doc)
        store = FakeStore(raw)
        dst = FakeDestinationAPI()
        src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 100, "login": "alice", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Alice/repos?type=owner": [
                    {"id": 11, "name": "alpha", "full_name": "alice/alpha", "clone_url": "https://github.com/alice/alpha.git"}
                ]
            },
        )
        stream = io.StringIO()
        res = onboard_auto_discoveries(raw, store, lambda _: src, dst, owner="dest", account_type="personal", stream=stream)
        self.assertEqual(res.relocated_rules, ())
        self.assertEqual(store.publish_calls, 0)
        self.assertEqual(dst.calls, [])
        self.assertNotIn("relocated to", stream.getvalue())


class AdoptionTests(unittest.TestCase):
    def test_adoption_happy_path_renames_creates_and_publishes_in_one_cas_write(self):
        old_child = {
            "source": {"url": "https://github.com/Alice/alpha", "id": 11},
            "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 5001, "visibility": "private"}},
            "discovered_by": "https://github.com/Alice",
        }
        tracked_child = {
            "source": {"url": "https://github.com/Alice/beta", "id": 12},
            "mirrors": {"main": {"name": "mirror-Alice-beta", "id": 5002, "visibility": "private"}},
            "discovered_by": "https://github.com/Alice",
        }
        doc = _base_doc(
            [
                {
                    "url": "https://github.com/Alice",
                    "id": 100,
                    "overrides": {"alpha": {"visibility": "public", "ignore_releases": True}},
                }
            ],
            [old_child, tracked_child],
        )
        raw = _encode(doc)
        store = FakeStore(raw)
        dst = FakeDestinationAPI(
            existing={
                "mirror-alice-alpha": {
                    "id": 5001,
                    "name": "mirror-Alice-alpha",
                    "full_name": "dest/mirror-Alice-alpha",
                    "private": True,
                }
            }
        )
        src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 100, "login": "Alice", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Alice/repos?type=owner": [
                    {
                        "id": 211,
                        "name": "alpha",
                        "full_name": "Alice/alpha",
                        "clone_url": "https://github.com/Alice/alpha.git",
                    },
                    {
                        "id": 12,
                        "name": "beta",
                        "full_name": "Alice/beta",
                        "clone_url": "https://github.com/Alice/beta.git",
                    },
                ]
            },
        )
        stamp = "20261126T090000Z"
        stream = io.StringIO()
        res = onboard_auto_discoveries(
            raw,
            store,
            lambda _: src,
            dst,
            owner="dest",
            account_type="personal",
            stream=stream,
            archive_timestamp=stamp,
        )
        self.assertEqual(store.publish_calls, 1)
        self.assertEqual(res.inducted_total, 0)
        self.assertEqual(res.adopted_total, 1)
        self.assertEqual(res.conflicts, ())
        self.assertEqual(res.orphaned_children, ())
        self.assertIn(("GET", "https://api.github.com/repositories/11"), src.requests)

        expected_archive = f"mirror-Alice-alpha-wipe-{stamp}"
        patch_calls = [c for c in dst.calls if c[0] == "PATCH"]
        self.assertEqual(
            patch_calls,
            [("PATCH", "https://api.github.com/repos/dest/mirror-Alice-alpha", {"name": expected_archive})],
        )
        self.assertIn(expected_archive.lower(), dst.repos)
        self.assertEqual(dst.repos[expected_archive.lower()]["id"], 5001)
        self.assertIn("mirror-alice-alpha", dst.repos)
        new_dest_id = dst.repos["mirror-alice-alpha"]["id"]
        self.assertNotEqual(new_dest_id, 5001)
        self.assertEqual(dst.actions["mirror-Alice-alpha"], False)

        post_payloads = [data for method, _, data in dst.calls if method == "POST"]
        self.assertEqual(len(post_payloads), 1)
        self.assertEqual(post_payloads[0]["name"], "mirror-Alice-alpha")
        self.assertEqual(post_payloads[0]["private"], False)
        self.assertEqual(post_payloads[0]["auto_init"], False)
        self.assertIn("[repo-mirror:initializing:v1]", post_payloads[0]["description"])

        published = json.loads(res.config_bytes)
        self.assertEqual(len(published["repositories"]), 2)
        by_url = {entry["source"]["url"]: entry for entry in published["repositories"]}
        adopted = by_url["https://github.com/Alice/alpha"]
        self.assertEqual(adopted["source"], {"url": "https://github.com/Alice/alpha", "id": 211})
        self.assertEqual(
            adopted["mirrors"]["main"],
            {
                "name": "mirror-Alice-alpha",
                "id": new_dest_id,
                "visibility": "public",
                "ignore_releases": True,
            },
        )
        self.assertEqual(adopted["discovered_by"], "https://github.com/Alice")

        log = stream.getvalue()
        self.assertIn(
            f"auto_discovery 1/1 (Alice): repository alpha: upstream recreated (pinned id 11, now 211); "
            f"archived as {expected_archive}; adopted",
            log,
        )
        self.assertIn(
            "auto_discovery 1/1 (Alice): OK discovered=2 inducted=0 excluded=0 tracked=1 conflicts=0 adopted=1",
            log,
        )
        self.assertNotIn("https://", log)

        # Steady-state second run is a complete no-op.
        dst.calls.clear()
        steady = onboard_auto_discoveries(
            res.config_bytes,
            store,
            lambda _: src,
            dst,
            owner="dest",
            account_type="personal",
            archive_timestamp=stamp,
        )
        self.assertEqual(steady.config_bytes, res.config_bytes)
        self.assertEqual(store.publish_calls, 1)
        self.assertEqual(dst.calls, [])
        self.assertEqual(steady.adopted_total, 0)

    def test_cli_adopts_recreated_repository_and_mirrors_in_same_run(self):
        from contextlib import redirect_stderr

        doc = _base_doc(
            [{"url": "https://github.com/Alice", "id": 100}],
            [
                {
                    "source": {"url": "https://github.com/Alice/alpha", "id": 11},
                    "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 5001, "visibility": "private"}},
                    "discovered_by": "https://github.com/Alice",
                },
                {
                    "source": {"url": "https://github.com/Alice/beta", "id": 12},
                    "mirrors": {"main": {"name": "mirror-Alice-beta", "id": 5002, "visibility": "private"}},
                    "discovered_by": "https://github.com/Alice",
                },
            ],
        )
        raw = _encode(doc)
        store = FakeStore(raw)
        dst = FakeDestinationAPI(
            existing={
                "mirror-alice-alpha": {
                    "id": 5001,
                    "name": "mirror-Alice-alpha",
                    "full_name": "dest/mirror-Alice-alpha",
                    "private": True,
                }
            }
        )
        src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 100, "login": "Alice", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Alice/repos?type=owner": [
                    {
                        "id": 211,
                        "name": "alpha",
                        "full_name": "Alice/alpha",
                        "clone_url": "https://github.com/Alice/alpha.git",
                    },
                    {
                        "id": 12,
                        "name": "beta",
                        "full_name": "Alice/beta",
                        "clone_url": "https://github.com/Alice/beta.git",
                    },
                ]
            },
        )
        mirrored = []

        def fake_mirror(source, name, owner, *args, **kwargs):
            mirrored.append((name, source["mirror"]["source_id"], source["mirror"]["destination_id"]))
            return None

        env = {"GH_TOKEN": "token", "DESTINATION_OWNER": "dest", "MIRROR_TIMESTAMP_SECRET": "secret"}
        stderr_buf = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_bytes(raw)
            with (
                patch.dict(os.environ, env, clear=True),
                patch("repo_mirror.github_migration.store_from_environment", return_value=store),
                patch("repo_mirror.github_migration.transaction_from_environment", return_value=None),
                patch.object(cli, "SourceAPI", return_value=src),
                patch.object(cli, "API", side_effect=[Mock(), dst]),
                patch.object(cli, "mirror_repository", side_effect=fake_mirror),
                redirect_stderr(stderr_buf),
            ):
                cli.main(["--config", str(path)])

        self.assertEqual(store.publish_calls, 1)
        patch_calls = [c for c in dst.calls if c[0] == "PATCH"]
        self.assertEqual(len(patch_calls), 1)
        archived_name = patch_calls[0][2]["name"]
        self.assertTrue(archived_name.startswith("mirror-Alice-alpha-wipe-"))
        self.assertIn(archived_name.lower(), dst.repos)
        new_alpha_id = dst.repos["mirror-alice-alpha"]["id"]
        self.assertEqual(
            sorted(mirrored),
            [("alpha", 211, new_alpha_id), ("beta", 12, 5002)],
        )
        stderr_out = stderr_buf.getvalue()
        self.assertIn(
            f"auto_discovery 1/1 (Alice): repository alpha: upstream recreated (pinned id 11, now 211); "
            f"archived as {safe_label(archived_name)}; adopted",
            stderr_out,
        )
        self.assertIn(
            "auto_discovery 1/1 (Alice): OK discovered=2 inducted=0 excluded=0 tracked=1 conflicts=0 adopted=1",
            stderr_out,
        )
        self.assertIn("summary sources=2 discovery_failed=0 repositories=2 succeeded=2 failed=0", stderr_out)

    def test_adoption_refuses_when_pinned_id_still_alive_or_lookup_errors(self):
        child = {
            "source": {"url": "https://github.com/Alice/alpha", "id": 11},
            "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 5001, "visibility": "private"}},
            "discovered_by": "https://github.com/Alice",
        }
        doc = _base_doc([{"url": "https://github.com/Alice", "id": 100}], [child])
        raw = _encode(doc)

        # Case 1: GET /repositories/11 returns 200 (moved/transferred) -> conflict, no rename.
        store_alive = FakeStore(raw)
        dst_alive = FakeDestinationAPI(
            existing={
                "mirror-alice-alpha": {
                    "id": 5001,
                    "name": "mirror-Alice-alpha",
                    "full_name": "dest/mirror-Alice-alpha",
                }
            }
        )
        src_alive = FakeSourceAPI(
            routes={
                "https://api.github.com/users/Alice": {"id": 100, "login": "Alice", "type": "User"},
                "https://api.github.com/repositories/11": {"id": 11, "full_name": "Alice/moved-alpha"},
            },
            pages_routes={
                "https://api.github.com/users/Alice/repos?type=owner": [
                    {
                        "id": 211,
                        "name": "alpha",
                        "full_name": "Alice/alpha",
                        "clone_url": "https://github.com/Alice/alpha.git",
                    }
                ]
            },
        )
        stream_alive = io.StringIO()
        res_alive = onboard_auto_discoveries(
            raw, store_alive, lambda _: src_alive, dst_alive, owner="dest", account_type="personal", stream=stream_alive
        )
        self.assertEqual(store_alive.publish_calls, 0)
        self.assertEqual(dst_alive.calls, [])
        self.assertEqual(res_alive.adopted_total, 0)
        self.assertEqual(len(res_alive.conflicts), 1)
        self.assertIn("conflicts=1 adopted=0", stream_alive.getvalue())

        # Case 2: GET /repositories/11 fails with HTTP 500 -> fixed safe message, keeps conflict, no rename.
        store_err = FakeStore(raw)
        dst_err = FakeDestinationAPI(
            existing={
                "mirror-alice-alpha": {
                    "id": 5001,
                    "name": "mirror-Alice-alpha",
                    "full_name": "dest/mirror-Alice-alpha",
                }
            }
        )
        src_err = FakeSourceAPI(
            routes={
                "https://api.github.com/users/Alice": {"id": 100, "login": "Alice", "type": "User"},
                "https://api.github.com/repositories/11": _http_error("https://api.github.com/repositories/11", 500),
            },
            pages_routes=src_alive.pages_routes,
        )
        stream_err = io.StringIO()
        res_err = onboard_auto_discoveries(
            raw, store_err, lambda _: src_err, dst_err, owner="dest", account_type="personal", stream=stream_err
        )
        self.assertEqual(store_err.publish_calls, 0)
        self.assertEqual(dst_err.calls, [])
        self.assertEqual(res_err.adopted_total, 0)
        self.assertEqual(len(res_err.conflicts), 1)
        self.assertIn(
            "auto_discovery 1/1 (Alice): repository alpha: upstream identity changed (pinned id 11, now 211); "
            "could not confirm upstream repository deletion; entry kept, not mirrored",
            stream_err.getvalue(),
        )
        self.assertIn("conflicts=1 adopted=0", stream_err.getvalue())

    def test_adoption_refuses_custom_destination_name_from_overrides_or_mirrors(self):
        # Case 1: overrides.<repo>.name set -> refuses adoption.
        doc_override = _base_doc(
            [{"url": "https://github.com/Alice", "id": 100, "overrides": {"alpha": {"name": "custom-alpha"}}}],
            [
                {
                    "source": {"url": "https://github.com/Alice/alpha", "id": 11},
                    "mirrors": {"main": {"name": "custom-alpha", "id": 5001, "visibility": "private"}},
                    "discovered_by": "https://github.com/Alice",
                }
            ],
        )
        raw_override = _encode(doc_override)
        store_override = FakeStore(raw_override)
        dst = FakeDestinationAPI(
            existing={"custom-alpha": {"id": 5001, "name": "custom-alpha", "full_name": "dest/custom-alpha"}}
        )
        src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 100, "login": "Alice", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Alice/repos?type=owner": [
                    {
                        "id": 211,
                        "name": "alpha",
                        "full_name": "Alice/alpha",
                        "clone_url": "https://github.com/Alice/alpha.git",
                    }
                ]
            },
        )
        stream_override = io.StringIO()
        res_override = onboard_auto_discoveries(
            raw_override, store_override, lambda _: src, dst, owner="dest", account_type="personal", stream=stream_override
        )
        self.assertEqual(store_override.publish_calls, 0)
        self.assertEqual(dst.calls, [])
        self.assertEqual(res_override.adopted_total, 0)
        self.assertEqual(len(res_override.conflicts), 1)
        self.assertIn(
            "cannot adopt a repository with a configured destination name; entry kept, not mirrored",
            stream_override.getvalue(),
        )

        # Case 2: mirrors.<profile>.name differs from derived <prefix>-<repo> -> refuses adoption.
        raw_rule = {"url": "https://github.com/Alice", "id": 100}
        rule = normalize_config(_base_doc([raw_rule]))["auto_discoveries"][0]
        custom_mirror_child = {
            "source": {"url": "https://github.com/Alice/alpha", "id": 11},
            "mirrors": {"main": {"name": "custom-mirrors-alpha", "id": 5001, "visibility": "private"}},
            "discovered_by": "https://github.com/Alice",
        }
        plan = _plan_rule(1, 1, rule, raw_rule, "main", [custom_mirror_child], src, runner="github", raw_index=0)
        self.assertEqual(plan.adoptions, ())
        self.assertEqual(len(plan.conflicts), 1)
        self.assertEqual(plan.conflicts[0].reason, "cannot adopt a repository with a configured destination name")

    def test_adoption_retry_when_canonical_name_already_404_skips_rename(self):
        doc = _base_doc(
            [{"url": "https://github.com/Alice", "id": 100}],
            [
                {
                    "source": {"url": "https://github.com/Alice/alpha", "id": 11},
                    "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 5001, "visibility": "private"}},
                    "discovered_by": "https://github.com/Alice",
                }
            ],
        )
        raw = _encode(doc)
        src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 100, "login": "Alice", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Alice/repos?type=owner": [
                    {
                        "id": 211,
                        "name": "alpha",
                        "full_name": "Alice/alpha",
                        "clone_url": "https://github.com/Alice/alpha.git",
                    }
                ]
            },
        )
        # First simulate CAS failure after rename + create, leaving archive in place and removing canonical name for retry.
        dst_partial = FakeDestinationAPI(
            existing={
                "mirror-alice-alpha": {
                    "id": 5001,
                    "name": "mirror-Alice-alpha",
                    "full_name": "dest/mirror-Alice-alpha",
                }
            }
        )
        failing_store = FakeStore(raw)
        failing_store.publish = Mock(side_effect=ValueError("conflict"))
        stamp = "20261126T090000Z"
        with self.assertRaisesRegex(
            PartialOnboarding, "adoption publication failed; archived destinations left in place; reconcile manually"
        ):
            onboard_auto_discoveries(
                raw,
                failing_store,
                lambda _: src,
                dst_partial,
                owner="dest",
                account_type="personal",
                archive_timestamp=stamp,
            )
        expected_archive = f"mirror-Alice-alpha-wipe-{stamp}".lower()
        self.assertIn(expected_archive, dst_partial.repos)

        # Direct retry with canonical name already 404 (no existing repo at mirror-Alice-alpha) skips PATCH rename.
        dst_retry = FakeDestinationAPI(
            existing={expected_archive: dst_partial.repos[expected_archive]}
        )
        retry_store = FakeStore(raw)
        stream = io.StringIO()
        res = onboard_auto_discoveries(
            raw,
            retry_store,
            lambda _: src,
            dst_retry,
            owner="dest",
            account_type="personal",
            stream=stream,
            archive_timestamp=stamp,
        )
        self.assertEqual(retry_store.publish_calls, 1)
        self.assertEqual(res.adopted_total, 1)
        self.assertFalse(any(method == "PATCH" for method, _, _ in dst_retry.calls))
        self.assertIn("archived previously; adopted", stream.getvalue())
        self.assertIn("conflicts=0 adopted=1", stream.getvalue())
        published = json.loads(res.config_bytes)
        self.assertEqual(published["repositories"][0]["source"]["id"], 211)
        self.assertEqual(published["repositories"][0]["mirrors"]["main"]["id"], dst_retry.repos["mirror-alice-alpha"]["id"])

    def test_adoption_rename_failure_prevents_creation_and_publication_for_that_repo_only(self):
        alpha_child = {
            "source": {"url": "https://github.com/Alice/alpha", "id": 11},
            "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 5001, "visibility": "private"}},
            "discovered_by": "https://github.com/Alice",
        }
        doc_only = _base_doc([{"url": "https://github.com/Alice", "id": 100}], [alpha_child])
        raw_only = _encode(doc_only)
        src_only = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 100, "login": "Alice", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Alice/repos?type=owner": [
                    {
                        "id": 211,
                        "name": "alpha",
                        "full_name": "Alice/alpha",
                        "clone_url": "https://github.com/Alice/alpha.git",
                    }
                ]
            },
        )

        # Case 1: PATCH raises HTTP 500 -> no creation, no publish, conflict reported.
        store_http = FakeStore(raw_only)
        dst_http = FakeDestinationAPI(
            existing={
                "mirror-alice-alpha": {
                    "id": 5001,
                    "name": "mirror-Alice-alpha",
                    "full_name": "dest/mirror-Alice-alpha",
                }
            }
        )
        dst_http.fail_on_patch = "mirror-Alice-alpha"
        stream_http = io.StringIO()
        res_http = onboard_auto_discoveries(
            raw_only, store_http, lambda _: src_only, dst_http, owner="dest", account_type="personal", stream=stream_http
        )
        self.assertEqual(store_http.publish_calls, 0)
        self.assertFalse(any(method == "POST" for method, _, _ in dst_http.calls))
        self.assertEqual(res_http.adopted_total, 0)
        self.assertEqual(len(res_http.conflicts), 1)
        self.assertIn("failed to archive existing destination; entry kept, not mirrored", stream_http.getvalue())
        self.assertIn("conflicts=1 adopted=0", stream_http.getvalue())

        # Case 2: PATCH returns mismatched repository id -> verification fails -> no creation, no publish; other repos continue.
        doc_multi = _base_doc([{"url": "https://github.com/Alice", "id": 100}], [alpha_child])
        raw_multi = _encode(doc_multi)
        store_multi = FakeStore(raw_multi)
        dst_multi = FakeDestinationAPI(
            existing={
                "mirror-alice-alpha": {
                    "id": 5001,
                    "name": "mirror-Alice-alpha",
                    "full_name": "dest/mirror-Alice-alpha",
                }
            }
        )
        dst_multi.patch_response_override = lambda old_name, new_name, repo: {
            "id": 9999,
            "name": new_name,
            "full_name": f"dest/{new_name}",
        }
        src_multi = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 100, "login": "Alice", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Alice/repos?type=owner": [
                    {
                        "id": 211,
                        "name": "alpha",
                        "full_name": "Alice/alpha",
                        "clone_url": "https://github.com/Alice/alpha.git",
                    },
                    {
                        "id": 12,
                        "name": "beta",
                        "full_name": "Alice/beta",
                        "clone_url": "https://github.com/Alice/beta.git",
                    },
                ]
            },
        )
        stream_multi = io.StringIO()
        res_multi = onboard_auto_discoveries(
            raw_multi,
            store_multi,
            lambda _: src_multi,
            dst_multi,
            owner="dest",
            account_type="personal",
            stream=stream_multi,
        )
        self.assertEqual(store_multi.publish_calls, 1)
        self.assertEqual(res_multi.inducted_total, 1)
        self.assertEqual(res_multi.adopted_total, 0)
        self.assertEqual(len(res_multi.conflicts), 1)
        post_names = [data["name"] for method, _, data in dst_multi.calls if method == "POST"]
        self.assertEqual(post_names, ["mirror-Alice-beta"])
        published = json.loads(res_multi.config_bytes)
        by_url = {r["source"]["url"]: r for r in published["repositories"]}
        self.assertEqual(by_url["https://github.com/Alice/alpha"]["source"]["id"], 11)
        self.assertEqual(by_url["https://github.com/Alice/beta"]["source"]["id"], 12)
        self.assertIn("inducted=1 excluded=0 tracked=0 conflicts=1 adopted=0", stream_multi.getvalue())

    def test_adoption_long_name_uses_100_char_hash_fallback(self):
        repo_name = "r" * 83
        dest_name = f"mirror-Alice-{repo_name}"  # 13 + 83 = 96 chars (> 100 when -wipe-<ts> added)
        self.assertEqual(len(dest_name), 96)
        stamp = "20261126T090000Z"
        expected_archive = archive_name(dest_name, stamp)
        self.assertEqual(len(expected_archive), 100)

        doc = _base_doc(
            [{"url": "https://github.com/Alice", "id": 100}],
            [
                {
                    "source": {"url": f"https://github.com/Alice/{repo_name}", "id": 11},
                    "mirrors": {"main": {"name": dest_name, "id": 5001, "visibility": "private"}},
                    "discovered_by": "https://github.com/Alice",
                }
            ],
        )
        raw = _encode(doc)
        store = FakeStore(raw)
        dst = FakeDestinationAPI(
            existing={dest_name.lower(): {"id": 5001, "name": dest_name, "full_name": f"dest/{dest_name}"}}
        )
        src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 100, "login": "Alice", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Alice/repos?type=owner": [
                    {
                        "id": 211,
                        "name": repo_name,
                        "full_name": f"Alice/{repo_name}",
                        "clone_url": f"https://github.com/Alice/{repo_name}.git",
                    }
                ]
            },
        )
        stream = io.StringIO()
        res = onboard_auto_discoveries(
            raw,
            store,
            lambda _: src,
            dst,
            owner="dest",
            account_type="personal",
            stream=stream,
            archive_timestamp=stamp,
        )
        self.assertEqual(res.adopted_total, 1)
        self.assertEqual(store.publish_calls, 1)
        patch_calls = [c for c in dst.calls if c[0] == "PATCH"]
        self.assertEqual(
            patch_calls,
            [("PATCH", f"https://api.github.com/repos/dest/{dest_name}", {"name": expected_archive})],
        )
        self.assertIn(expected_archive.lower(), dst.repos)
        self.assertEqual(dst.repos[expected_archive.lower()]["id"], 5001)
        self.assertIn(dest_name.lower(), dst.repos)
        self.assertNotEqual(dst.repos[dest_name.lower()]["id"], 5001)
        self.assertIn("adopted=1", stream.getvalue())

    def test_plain_pinned_entry_without_discovered_by_never_adopts(self):
        doc = _base_doc(
            [{"url": "https://github.com/Alice", "id": 100}],
            [
                {
                    "source": {"url": "https://github.com/Alice/alpha", "id": 11},
                    "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 5001}},
                }
            ],
        )
        raw = _encode(doc)
        store = FakeStore(raw)
        dst = FakeDestinationAPI(
            existing={
                "mirror-alice-alpha": {
                    "id": 5001,
                    "name": "mirror-Alice-alpha",
                    "full_name": "dest/mirror-Alice-alpha",
                }
            }
        )
        src = FakeSourceAPI(
            routes={"https://api.github.com/users/Alice": {"id": 100, "login": "Alice", "type": "User"}},
            pages_routes={
                "https://api.github.com/users/Alice/repos?type=owner": [
                    {
                        "id": 211,
                        "name": "alpha",
                        "full_name": "Alice/alpha",
                        "clone_url": "https://github.com/Alice/alpha.git",
                    }
                ]
            },
        )
        stream = io.StringIO()
        res = onboard_auto_discoveries(
            raw, store, lambda _: src, dst, owner="dest", account_type="personal", stream=stream
        )
        self.assertEqual(store.publish_calls, 0)
        self.assertEqual(dst.calls, [])
        self.assertEqual(res.adopted_total, 0)
        self.assertEqual(len(res.conflicts), 1)
        self.assertFalse(any("/repositories/" in url for _, url in src.requests))
        self.assertIn("conflicts=1 adopted=0", stream.getvalue())

    def test_gitlab_rule_child_keeps_conflict_behaviour(self):
        doc = _base_doc(
            [{"url": "https://gitlab.com/team/sub", "type": "gitlab", "id": 7}],
            [
                {
                    "source": {"url": "https://gitlab.com/team/sub/proj", "id": 701},
                    "mirrors": {"main": {"name": "mirror-team-sub-proj", "id": 5001, "visibility": "private"}},
                    "discovered_by": "https://gitlab.com/team/sub",
                }
            ],
        )
        raw = _encode(doc)
        store = FakeStore(raw)
        dst = FakeDestinationAPI(
            existing={
                "mirror-team-sub-proj": {
                    "id": 5001,
                    "name": "mirror-team-sub-proj",
                    "full_name": "dest/mirror-team-sub-proj",
                }
            }
        )
        src = FakeSourceAPI(
            routes={"https://gitlab.com/api/v4/groups/team%2Fsub": {"id": 7, "visibility": "public", "full_path": "team/sub"}},
            pages_routes={
                "https://gitlab.com/api/v4/groups/team%2Fsub/projects?include_subgroups=false&with_shared=false": [
                    {
                        "id": 799,
                        "path": "proj",
                        "visibility": "public",
                        "path_with_namespace": "team/sub/proj",
                        "http_url_to_repo": "https://gitlab.com/team/sub/proj.git",
                    }
                ]
            },
        )
        stream = io.StringIO()
        res = onboard_auto_discoveries(
            raw, store, lambda _: src, dst, owner="dest", account_type="personal", stream=stream
        )
        self.assertEqual(store.publish_calls, 0)
        self.assertEqual(dst.calls, [])
        self.assertEqual(res.adopted_total, 0)
        self.assertEqual(len(res.conflicts), 1)
        self.assertIn("conflicts=1 adopted=0", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
