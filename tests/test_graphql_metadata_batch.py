import base64
import contextlib
import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError

from repo_mirror import (
    auto_discoveries_onboarding,
    cli,
    forgejo_dispatch,
    forgejo_schedule,
    forgejo_sync,
    github_api,
    repository,
)
from repo_mirror.config_named import normalize_config
from repo_mirror.forgejo_dispatch import DispatchError
from repo_mirror.github_api import (
    API,
    SourceAPI,
    api_call_metrics_snapshot,
    format_api_call_summary,
    graphql_points_total,
    reset_api_call_metrics,
)
from repo_mirror.graphql_api import GRAPHQL_URL, GraphQLClient, GraphQLError, normalize_repository_node
from repo_mirror.journal import FailureJournal
from repo_mirror.sources import source_marker


class _StubResponse:
    def __init__(self, payload, status=200, headers=None):
        body = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload
        self._buf = io.BytesIO(body)
        self.status = status
        self.code = status
        msg = Message()
        for k, v in (headers or {}).items():
            msg[k] = v
        self.headers = msg

    def read(self, size=-1):
        return self._buf.read(size)

    def close(self):
        self._buf.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _gql_repo_node(
    database_id,
    owner,
    name,
    *,
    description=None,
    default_branch="main",
    is_private=False,
    is_archived=False,
    is_disabled=False,
):
    return {
        "databaseId": database_id,
        "name": name,
        "nameWithOwner": f"{owner}/{name}",
        "description": description,
        "owner": {"login": owner},
        "url": f"https://github.com/{owner}/{name}",
        "isPrivate": is_private,
        "isArchived": is_archived,
        "isEmpty": False,
        "isDisabled": is_disabled,
        "defaultBranchRef": {"name": default_branch, "target": {"oid": "a" * 40}} if default_branch else None,
        "pushedAt": "2026-11-01T00:00:00Z",
        "updatedAt": "2026-11-01T00:00:00Z",
    }


def _rest_repo_dict(
    repo_id,
    owner,
    name,
    *,
    description=None,
    default_branch="main",
    private=False,
    archived=False,
    disabled=False,
):
    return {
        "id": repo_id,
        "name": name,
        "full_name": f"{owner}/{name}",
        "description": description,
        "owner": {"login": owner},
        "html_url": f"https://github.com/{owner}/{name}",
        "clone_url": f"https://github.com/{owner}/{name}.git",
        "default_branch": default_branch,
        "private": private,
        "archived": archived,
        "disabled": disabled,
        "pushed_at": "2026-11-01T00:00:00Z",
        "updated_at": "2026-11-01T00:00:00Z",
    }


def _bind_opener(api_obj, fn):
    """Attach a mock opener that survives restrict_origin() calls in cli.main."""
    mock_open = MagicMock(side_effect=fn)
    api_obj.opener.open = mock_open
    orig_restrict = getattr(api_obj, "restrict_origin", None)
    if callable(orig_restrict):
        def _wrapped_restrict(origin):
            orig_restrict(origin)
            api_obj.opener.open = mock_open
        api_obj.restrict_origin = _wrapped_restrict
    return mock_open


def _parse_batch_aliases(body):
    query = body["query"]
    variables = body["variables"]
    aliases = []
    idx = 0
    while f"owner{idx}" in variables:
        aliases.append((f"r{idx}", variables[f"owner{idx}"], variables[f"name{idx}"]))
        idx += 1
    assert "rateLimit" in query
    return aliases


class GraphQLMetadataBatchTests(unittest.TestCase):
    def setUp(self):
        reset_api_call_metrics()
        with github_api._cooldown_lock:
            github_api._cooldowns.clear()
        env_patch = patch.dict(
            os.environ,
            {
                "GH_TOKEN": "synthetic-test-token",
                "DESTINATION_OWNER": "dest",
                "MIRROR_TIMESTAMP_SECRET": "synthetic-test-only-secret",
            },
            clear=True,
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.addCleanup(reset_api_call_metrics)
        self.addCleanup(self._clear_cooldowns)

    def _clear_cooldowns(self):
        with github_api._cooldown_lock:
            github_api._cooldowns.clear()

    def test_config_validation_graphql_flag(self):
        base = {
            "destinations": {"main": {"account_type": "organization"}},
            "repositories": [{"source": {"url": "https://github.com/alice/demo"}, "mirrors": {"main": {}}}],
        }
        self.assertNotIn("graphql", normalize_config(base))
        self.assertIs(normalize_config({**base, "graphql": True})["graphql"], True)
        self.assertIs(normalize_config({**base, "graphql": False})["graphql"], False)

        for bad in ("true", "false", 1, 0, None, [], {}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "graphql must be a boolean"):
                normalize_config({**base, "graphql": bad})

    def test_normalize_repository_node_requires_positive_integer_database_id(self):
        self.assertIsNone(normalize_repository_node(None))
        for bad_id in (None, "R_kgDOAbCdEf", True, False, 0, -5, 12.5):
            node = _gql_repo_node(123, "alice", "demo")
            if bad_id is None:
                node.pop("databaseId")
            else:
                node["databaseId"] = bad_id
            with self.subTest(bad_id=bad_id), self.assertRaisesRegex(
                GraphQLError, "GraphQL repository response missing integer databaseId"
            ):
                normalize_repository_node(node)

        # String GraphQL `id` is ignored; numeric `databaseId` is used for `id`.
        node = _gql_repo_node(456, "alice", "demo", description="marker")
        node["id"] = "R_kgDO_NODE_ID"
        normalized = normalize_repository_node(node)
        self.assertEqual(normalized["id"], 456)
        self.assertEqual(normalized["clone_url"], "https://github.com/alice/demo.git")
        self.assertEqual(normalized["default_branch"], "main")
        self.assertEqual(normalized["description"], "marker")

    def test_flag_on_vs_off_parity_and_measured_request_reduction(self):
        # 10 unpinned GitHub repositories: 5 existing destinations, 5 new destinations.
        repos = [f"repo-{i}" for i in range(10)]
        base_config = {
            "destinations": {"main": {"account_type": "organization"}},
            "repositories": [
                {"source": {"url": f"https://github.com/alice/{name}"}, "mirrors": {"main": {"name": name}}}
                for name in repos
            ],
        }

        def run_with_flag(graphql_enabled):
            reset_api_call_metrics()
            cfg_data = dict(base_config)
            if graphql_enabled is not None:
                cfg_data["graphql"] = graphql_enabled

            lock = threading.Lock()
            src_rest_gets = []
            dst_rest_gets = []
            dst_creates = []
            gql_batches = []
            captured_metadata = {}

            src_api = SourceAPI("synthetic-source-token")
            dst_api = API("synthetic-dest-token")

            def fake_src_open(req, timeout=60):
                url = req.full_url
                if url == GRAPHQL_URL:
                    body = json.loads(req.data.decode("utf-8"))
                    aliases = _parse_batch_aliases(body)
                    with lock:
                        gql_batches.append(("source", len(aliases)))
                    data = {
                        alias: _gql_repo_node(1000 + int(name.split("-")[1]), owner, name)
                        for alias, owner, name in aliases
                    }
                    data["rateLimit"] = {"cost": 1, "remaining": 4999, "resetAt": "2099-01-01T00:00:00Z"}
                    return _StubResponse({"data": data})
                if req.get_method() == "GET" and "/repos/alice/" in url:
                    name = url.rsplit("/", 1)[-1]
                    with lock:
                        src_rest_gets.append(url)
                    return _StubResponse(_rest_repo_dict(1000 + int(name.split("-")[1]), "alice", name))
                raise AssertionError(f"unexpected source request: {req.get_method()} {url}")

            def fake_dst_open(req, timeout=60):
                url = req.full_url
                method = req.get_method()
                if url == GRAPHQL_URL:
                    body = json.loads(req.data.decode("utf-8"))
                    aliases = _parse_batch_aliases(body)
                    with lock:
                        gql_batches.append(("destination", len(aliases)))
                    data = {}
                    errors = []
                    for alias, owner, name in aliases:
                        idx = int(name.split("-")[1])
                        if idx < 5:
                            marker = source_marker(
                                {"type": "github", "base_url": "https://github.com", "owner": "alice"}, name
                            )
                            data[alias] = _gql_repo_node(2000 + idx, owner, name, description=marker)
                        else:
                            data[alias] = None
                            errors.append({"type": "NOT_FOUND", "path": [alias], "message": "not found"})
                    data["rateLimit"] = {"cost": 1, "remaining": 4999, "resetAt": "2099-01-01T00:00:00Z"}
                    payload = {"data": data}
                    if errors:
                        payload["errors"] = errors
                    return _StubResponse(payload)
                if method == "GET" and url.startswith("https://api.github.com/repos/dest/"):
                    name = url.rsplit("/", 1)[-1]
                    with lock:
                        dst_rest_gets.append(url)
                    idx = int(name.split("-")[1])
                    if idx < 5:
                        marker = source_marker(
                            {"type": "github", "base_url": "https://github.com", "owner": "alice"}, name
                        )
                        return _StubResponse(_rest_repo_dict(2000 + idx, "dest", name, description=marker))
                    raise HTTPError(url, 404, "Not Found", Message(), io.BytesIO(b"{}"))
                if method == "POST" and url == "https://api.github.com/orgs/dest/repos":
                    payload = json.loads(req.data.decode("utf-8"))
                    name = payload["name"]
                    idx = int(name.split("-")[1])
                    with lock:
                        dst_creates.append(name)
                    return _StubResponse(
                        _rest_repo_dict(2000 + idx, "dest", name, description=payload["description"], default_branch=None)
                    )
                if method == "PUT" and url.endswith("/actions/permissions"):
                    return _StubResponse({})
                if method == "PATCH" and url.startswith("https://api.github.com/repos/dest/"):
                    return _StubResponse({})
                raise AssertionError(f"unexpected destination request: {method} {url}")

            _bind_opener(src_api, fake_src_open)
            _bind_opener(dst_api, fake_dst_open)

            orig_mirror_repo = repository.mirror_repository

            def spy_mirror_repo(source, name, owner, src, dst, token, account_type="organization", **kwargs):
                with lock:
                    captured_metadata[name] = {
                        "existing_repo": kwargs.get("existing_repo"),
                        "source_repo": kwargs.get("source_repo"),
                    }
                return orig_mirror_repo(source, name, owner, src, dst, token, account_type, **kwargs)

            stderr = io.StringIO()
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as cfg:
                json.dump(cfg_data, cfg)
                cfg.flush()
                with (
                    patch.object(cli, "SourceAPI", return_value=src_api),
                    patch.object(cli, "API", return_value=dst_api),
                    patch.object(cli, "mirror_repository", side_effect=spy_mirror_repo),
                    patch.object(repository, "mirror_git"),
                    patch.object(repository, "mirror_releases"),
                    contextlib.redirect_stderr(stderr),
                ):
                    cli.main(["--config", cfg.name])

            return {
                "src_rest_gets": src_rest_gets,
                "dst_rest_gets": dst_rest_gets,
                "dst_creates": sorted(dst_creates),
                "gql_batches": gql_batches,
                "captured_metadata": captured_metadata,
                "metrics": api_call_metrics_snapshot(),
                "points": graphql_points_total(),
                "summary": format_api_call_summary(),
                "stderr": stderr.getvalue(),
            }

        off_result = run_with_flag(False)
        default_on_result = run_with_flag(None)
        explicit_on_result = run_with_flag(True)

        # Flag OFF: 10 destination preflight GETs + 10 source metadata GETs = 20 REST metadata GETs, 0 GraphQL.
        self.assertEqual(len(off_result["dst_rest_gets"]), 10)
        self.assertEqual(len(off_result["src_rest_gets"]), 10)
        self.assertEqual(off_result["gql_batches"], [])
        self.assertEqual(off_result["points"], 0)
        self.assertIn("graphql=0 graphql_points=0", off_result["summary"])

        # Flag ON (both default and explicit True): 0 per-repository metadata GETs, 2 GraphQL POSTs.
        for on_result in (default_on_result, explicit_on_result):
            self.assertEqual(on_result["dst_rest_gets"], [])
            self.assertEqual(on_result["src_rest_gets"], [])
            self.assertEqual(on_result["gql_batches"], [("destination", 10), ("source", 10)])
            self.assertEqual(on_result["dst_creates"], [f"repo-{i}" for i in range(5, 10)])
            self.assertEqual(on_result["metrics"][("github", "destination", "graphql", "POST")], 1)
            self.assertEqual(on_result["metrics"][("github", "source", "graphql", "POST")], 1)
            self.assertEqual(on_result["points"], 2)
            self.assertIn("graphql=2 graphql_points=2", on_result["summary"])

            # Verify normalized dicts match REST dicts for all existing repos.
            for idx in range(5):
                name = f"repo-{idx}"
                marker = source_marker({"type": "github", "base_url": "https://github.com", "owner": "alice"}, name)
                self.assertEqual(
                    on_result["captured_metadata"][name]["existing_repo"],
                    _rest_repo_dict(2000 + idx, "dest", name, description=marker),
                )
                self.assertEqual(
                    on_result["captured_metadata"][name]["source_repo"],
                    _rest_repo_dict(1000 + idx, "alice", name),
                )
            for idx in range(5, 10):
                name = f"repo-{idx}"
                self.assertIsNone(on_result["captured_metadata"][name]["existing_repo"])
                self.assertEqual(
                    on_result["captured_metadata"][name]["source_repo"],
                    _rest_repo_dict(1000 + idx, "alice", name),
                )

    def test_batches_more_than_100_repositories_into_chunks_of_100(self):
        api = SourceAPI("synthetic-token")
        batch_sizes = []

        def fake_open(req, timeout=60):
            body = json.loads(req.data.decode("utf-8"))
            aliases = _parse_batch_aliases(body)
            batch_sizes.append(len(aliases))
            data = {
                alias: _gql_repo_node(index + 1, owner, name)
                for index, (alias, owner, name) in enumerate(aliases)
            }
            data["rateLimit"] = {"cost": 1, "remaining": 4999, "resetAt": "2099-01-01T00:00:00Z"}
            return _StubResponse({"data": data})

        api.opener.open = MagicMock(side_effect=fake_open)
        repos = [("alice", f"repo-{i}") for i in range(105)]
        # Include a duplicate differing only by case to verify deduplication.
        repos.append(("Alice", "Repo-0"))

        result = GraphQLClient(api).fetch_repositories_metadata(repos)
        self.assertEqual(batch_sizes, [100, 5])
        self.assertEqual(len(result), 105)
        self.assertEqual(api_call_metrics_snapshot(), {("github", "source", "graphql", "POST"): 2})

    def test_pin_mismatch_in_batched_source_and_live_recheck_pins(self):
        config = {
            "destinations": {"main": {"account_type": "organization"}},
            "repositories": [
                {
                    "source": {"url": "https://github.com/alice/demo", "id": 11},
                    "mirrors": {"main": {"name": "demo", "id": 22}},
                }
            ],
        }
        marker = source_marker({"type": "github", "base_url": "https://github.com", "owner": "alice"}, "demo")

        # 1. Mutated source databaseId in GraphQL response raises ValueError before destination writes or git.
        src_api = SourceAPI("synthetic-src")
        dst_api = API("synthetic-dst")
        _bind_opener(
            src_api,
            lambda *a, **k: _StubResponse(
                {
                    "data": {
                        "r0": _gql_repo_node(999, "alice", "demo"),
                        "rateLimit": {"cost": 1, "remaining": 4999},
                    }
                }
            ),
        )
        _bind_opener(
            dst_api,
            lambda *a, **k: _StubResponse(
                {
                    "data": {
                        "r0": _gql_repo_node(22, "dest", "demo", description=marker),
                        "rateLimit": {"cost": 1, "remaining": 4999},
                    }
                }
            ),
        )
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as cfg:
            json.dump(config, cfg)
            cfg.flush()
            with (
                patch.object(cli, "SourceAPI", return_value=src_api),
                patch.object(cli, "API", return_value=dst_api),
                patch.object(repository, "mirror_git") as mock_git,
                patch.object(repository, "mirror_releases") as mock_releases,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                with self.assertRaisesRegex(RuntimeError, "1 repositories failed"):
                    cli.main(["--config", cfg.name])
            self.assertIn("failed: source repository id mismatch", stderr.getvalue())
            mock_git.assert_not_called()
            mock_releases.assert_not_called()
            with self.assertRaisesRegex(ValueError, "source repository id mismatch"):
                repository.mirror_repository(
                    {"type": "github", "base_url": "https://github.com", "owner": "alice", "name": "demo", "mirror": {"source_id": 11, "destination_id": 22}},
                    "demo",
                    "dest",
                    src_api,
                    dst_api,
                    "synthetic-token",
                    existing_repo=_rest_repo_dict(22, "dest", "demo", description=marker),
                    source_repo=_rest_repo_dict(999, "alice", "demo"),
                )

        # 2. Matching initial batch, then destination replaced mid-run: live recheck_pins() catches it via REST!
        reset_api_call_metrics()
        src_api2 = SourceAPI("synthetic-src")
        dst_api2 = API("synthetic-dst")
        src_rest_gets = []
        dst_rest_gets = []

        def src_open_2(req, timeout=60):
            if req.full_url == GRAPHQL_URL:
                return _StubResponse(
                    {
                        "data": {
                            "r0": _gql_repo_node(11, "alice", "demo"),
                            "rateLimit": {"cost": 1, "remaining": 4999},
                        }
                    }
                )
            src_rest_gets.append(req.full_url)
            return _StubResponse(_rest_repo_dict(11, "alice", "demo"))

        def dst_open_2(req, timeout=60):
            if req.full_url == GRAPHQL_URL:
                return _StubResponse(
                    {
                        "data": {
                            "r0": _gql_repo_node(22, "dest", "demo", description=marker),
                            "rateLimit": {"cost": 1, "remaining": 4999},
                        }
                    }
                )
            if req.get_method() == "GET":
                dst_rest_gets.append(req.full_url)
                # First recheck (before PUT /actions/permissions) returns id=22; second (before git) returns id=23!
                return _StubResponse(
                    _rest_repo_dict(22 if len(dst_rest_gets) == 1 else 23, "dest", "demo", description=marker)
                )
            return _StubResponse({})

        _bind_opener(src_api2, src_open_2)
        _bind_opener(dst_api2, dst_open_2)

        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as cfg:
            json.dump(config, cfg)
            cfg.flush()
            with (
                patch.object(cli, "SourceAPI", return_value=src_api2),
                patch.object(cli, "API", return_value=dst_api2),
                patch.object(repository, "mirror_git") as mock_git,
                patch.object(repository, "mirror_releases") as mock_releases,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                with self.assertRaisesRegex(RuntimeError, "1 repositories failed"):
                    cli.main(["--config", cfg.name])
            self.assertIn("failed: destination repository id mismatch or missing", stderr.getvalue())
            self.assertEqual(len(src_rest_gets), 2)
            self.assertEqual(len(dst_rest_gets), 2)
            mock_git.assert_not_called()
            mock_releases.assert_not_called()

    def test_graphql_query_failure_with_flag_on_fails_without_rest_fallback(self):
        config = {
            "destinations": {"main": {"account_type": "organization"}},
            "repositories": [
                {"source": {"url": "https://github.com/alice/demo"}, "mirrors": {"main": {"name": "demo"}}}
            ],
        }

        for case, response_or_exc in (
            (
                "graphql_forbidden",
                _StubResponse(
                    {
                        "data": {"rateLimit": {"cost": 1, "remaining": 4999}},
                        "errors": [{"type": "FORBIDDEN", "message": "forbidden"}],
                    }
                ),
            ),
            ("http_500", HTTPError(GRAPHQL_URL, 500, "Server Error", Message(), io.BytesIO(b"err"))),
            ("malformed_json", _StubResponse(b"not-json")),
        ):
            with self.subTest(case=case):
                reset_api_call_metrics()
                src_api = SourceAPI("synthetic-src")
                dst_api = API("synthetic-dst")
                seen_urls = []

                def dst_open(req, timeout=60, item=response_or_exc):
                    seen_urls.append(req.full_url)
                    if isinstance(item, Exception):
                        raise item
                    return item

                _bind_opener(dst_api, dst_open)
                src_mock_open = _bind_opener(
                    src_api,
                    lambda *a, **k: (_ for _ in ()).throw(AssertionError("source opener must not be called")),
                )

                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as cfg:
                    json.dump(config, cfg)
                    cfg.flush()
                    with (
                        patch.object(cli, "SourceAPI", return_value=src_api),
                        patch.object(cli, "API", return_value=dst_api),
                        patch.object(github_api.time, "sleep"),
                        contextlib.redirect_stderr(io.StringIO()),
                    ):
                        with self.assertRaises(Exception):
                            cli.main(["--config", cfg.name])

                self.assertTrue(all(u == GRAPHQL_URL for u in seen_urls))
                src_mock_open.assert_not_called()

    def test_missing_source_repo_in_graphql_batch_fails_without_rest_fallback(self):
        config = {
            "destinations": {"main": {"account_type": "organization"}},
            "repositories": [
                {"source": {"url": "https://github.com/alice/missing"}, "mirrors": {"main": {"name": "missing"}}}
            ],
        }
        src_api = SourceAPI("synthetic-src")
        dst_api = API("synthetic-dst")
        src_urls = []

        def src_open(req, timeout=60):
            src_urls.append(req.full_url)
            return _StubResponse(
                {
                    "data": {"r0": None, "rateLimit": {"cost": 1, "remaining": 4999}},
                    "errors": [{"type": "NOT_FOUND", "path": ["r0"], "message": "not found"}],
                }
            )

        def dst_open(req, timeout=60):
            if req.full_url == GRAPHQL_URL:
                return _StubResponse(
                    {
                        "data": {"r0": None, "rateLimit": {"cost": 1, "remaining": 4999}},
                        "errors": [{"type": "NOT_FOUND", "path": ["r0"], "message": "not found"}],
                    }
                )
            raise AssertionError(f"unexpected destination REST call: {req.get_method()} {req.full_url}")

        _bind_opener(src_api, src_open)
        src_api.download = lambda url, out: None
        _bind_opener(dst_api, dst_open)

        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as cfg:
            json.dump(config, cfg)
            cfg.flush()
            with (
                patch.object(cli, "SourceAPI", return_value=src_api),
                patch.object(cli, "API", return_value=dst_api),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                with self.assertRaisesRegex(RuntimeError, "1 repositories failed"):
                    cli.main(["--config", cfg.name])

        self.assertEqual(src_urls, [GRAPHQL_URL])
        self.assertIn("GraphQL resource not found", stderr.getvalue())

    def test_forgejo_source_with_graphql_on_batches_destination_only(self):
        config = {
            "destinations": {"main": {"account_type": "organization"}},
            "repositories": [
                {
                    "source": {"url": "https://forge.example.test/alice/demo"},
                    "mirrors": {"main": {"name": "demo"}},
                }
            ],
        }
        marker = source_marker({"type": "forgejo", "base_url": "https://forge.example.test", "owner": "alice"}, "demo")
        src_api = SourceAPI(None)
        dst_api = API("synthetic-dst")

        def src_open(req, timeout=60):
            self.assertEqual(req.full_url, "https://forge.example.test/api/v1/repos/alice/demo")
            return _StubResponse(
                {
                    "id": 77,
                    "clone_url": "https://forge.example.test/alice/demo.git",
                    "default_branch": "main",
                }
            )

        def dst_open(req, timeout=60):
            if req.full_url == GRAPHQL_URL:
                return _StubResponse(
                    {
                        "data": {
                            "r0": _gql_repo_node(200, "dest", "demo", description=marker),
                            "rateLimit": {"cost": 1, "remaining": 4999},
                        }
                    }
                )
            if req.get_method() == "PUT" and req.full_url.endswith("/actions/permissions"):
                return _StubResponse({})
            raise AssertionError(f"unexpected destination call: {req.get_method()} {req.full_url}")

        github_src_api = SourceAPI("synthetic-src")
        _bind_opener(
            github_src_api,
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("github source opener must not be called")),
        )
        _bind_opener(src_api, src_open)
        src_api.download = lambda url, out: None
        _bind_opener(dst_api, dst_open)

        other_src_api = API()
        _bind_opener(other_src_api, src_open)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as cfg:
            json.dump(config, cfg)
            cfg.flush()
            with (
                patch.object(cli, "SourceAPI", side_effect=[github_src_api, src_api]),
                patch.object(cli, "API", side_effect=[other_src_api, dst_api]),
                patch.object(repository, "mirror_git") as mock_git,
                patch.object(repository, "mirror_releases") as mock_releases,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                cli.main(["--config", cfg.name])

        mock_git.assert_called_once()
        mock_releases.assert_called_once()
        self.assertEqual(api_call_metrics_snapshot()[("github", "destination", "graphql", "POST")], 1)

    def test_pinned_github_relocation_works_from_batched_metadata(self):
        config = {
            "destinations": {"main": {"account_type": "personal"}},
            "repositories": [
                {
                    "source": {"url": "https://github.com/old/repo", "id": 1},
                    "mirrors": {"main": {"name": "copy", "id": 2}},
                }
            ],
        }
        marker = source_marker({"type": "github", "base_url": "https://github.com", "owner": "old"}, "repo")
        src_api = SourceAPI("synthetic-src")
        dst_api = API("synthetic-dst")

        def src_open(req, timeout=60):
            if req.full_url == GRAPHQL_URL:
                return _StubResponse(
                    {
                        "data": {
                            "r0": _gql_repo_node(1, "new", "renamed"),
                            "rateLimit": {"cost": 1, "remaining": 4999},
                        }
                    }
                )
            return _StubResponse(_rest_repo_dict(1, "new", "renamed"))

        def dst_open(req, timeout=60):
            if req.full_url == GRAPHQL_URL:
                return _StubResponse(
                    {
                        "data": {
                            "r0": _gql_repo_node(2, "dest", "copy", description=marker),
                            "rateLimit": {"cost": 1, "remaining": 4999},
                        }
                    }
                )
            if req.get_method() == "GET":
                return _StubResponse(_rest_repo_dict(2, "dest", "copy", description=marker))
            return _StubResponse({})

        _bind_opener(src_api, src_open)
        src_api.download = lambda url, out: None
        _bind_opener(dst_api, dst_open)

        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as cfg:
            json.dump(config, cfg)
            cfg.flush()
            with (
                patch.object(cli, "SourceAPI", return_value=src_api),
                patch.object(cli, "API", return_value=dst_api),
                patch.object(repository, "mirror_git") as mock_git,
                patch.object(repository, "mirror_releases"),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                relocations = cli.main(["--config", cfg.name])

        self.assertEqual(len(relocations), 1)
        self.assertEqual(relocations[0][1]["owner"], "new")
        self.assertEqual(relocations[0][1]["name"], "renamed")
        self.assertEqual(mock_git.call_args[0][0], "https://github.com/new/renamed.git")

    def test_secondary_entrypoints_accept_graphql_flag_and_reject_non_boolean(self):
        base_forgejo = {
            "destinations": {"main": {"account_type": "personal"}},
            "repositories": [
                {"source": {"url": "https://github.com/alice/demo"}, "mirrors": {"main": {"name": "demo"}}}
            ],
        }
        groups_forgejo = {
            "destinations": {"main": {"account_type": "personal"}},
            "groups": [{"type": "gitlab", "base_url": "https://gitlab.com", "owner": "team/sub", "id": 7, "repos": "all"}],
            "repositories": [],
        }

        # 1. forgejo_sync.parse_manifest / normalize_named
        for flag in (False, True):
            with self.subTest(entrypoint="forgejo_sync", graphql=flag):
                parsed = forgejo_sync.parse_manifest({**base_forgejo, "graphql": flag})
                self.assertEqual(len(parsed), 1)
                parsed_groups = forgejo_sync.parse_manifest({**groups_forgejo, "graphql": flag})
                self.assertEqual(parsed_groups, [])
        for bad in ("false", "true", 1, 0, None):
            with self.assertRaisesRegex(ValueError, "graphql must be a boolean"):
                forgejo_sync.parse_manifest({**base_forgejo, "graphql": bad})

        # 2. forgejo_dispatch.plan_dispatch
        blank_inputs = {
            "source_urls": "",
            "add_to_config": False,
            "destination_name": "",
            "visibility": "unchanged",
            "remove_from_config": False,
            "ignore_releases": "keep",
        }
        for flag in (False, True):
            with self.subTest(entrypoint="forgejo_dispatch", graphql=flag):
                pub, runtime, sel = forgejo_dispatch.plan_dispatch(
                    {**base_forgejo, "graphql": flag}, blank_inputs, owner="dest", port=3000
                )
                self.assertIsNone(pub)
                self.assertIsNone(sel)
                self.assertIs(runtime["graphql"], flag)
                pub_g, runtime_g, sel_g = forgejo_dispatch.plan_dispatch(
                    {**groups_forgejo, "graphql": flag}, blank_inputs, owner="dest", port=3000
                )
                self.assertIsNone(pub_g)
                self.assertIsNone(sel_g)
                self.assertIs(runtime_g["graphql"], flag)
        for bad in ("false", "true", 1, 0, None):
            with self.assertRaisesRegex(DispatchError, "graphql must be a boolean"):
                forgejo_dispatch.plan_dispatch(
                    {**base_forgejo, "graphql": bad}, blank_inputs, owner="dest", port=3000
                )

        # 3. auto_discoveries_onboarding._validate_document
        for flag in (False, True):
            with self.subTest(entrypoint="auto_discoveries_onboarding", graphql=flag):
                validated_gh = auto_discoveries_onboarding._validate_document(
                    {**base_forgejo, "graphql": flag}, "github"
                )
                self.assertIs(validated_gh["graphql"], flag)
                validated_fj = auto_discoveries_onboarding._validate_document(
                    {**groups_forgejo, "graphql": flag}, "forgejo"
                )
                self.assertNotIn("groups", validated_fj)
                self.assertIs(validated_fj["graphql"], flag)
        for bad in ("false", 1, None):
            with self.assertRaisesRegex(ValueError, "graphql must be a boolean"):
                auto_discoveries_onboarding._validate_document({**groups_forgejo, "graphql": bad}, "forgejo")

        # 4. forgejo_schedule.main with groups manifest and "graphql": False / True
        class _FakeControlAPI:
            def __init__(self, raw):
                self.files = {
                    "config.json": raw,
                    "migrations.json": b'{"version": 1, "migrations": {}, "signatures": {}}',
                    "failures.json": b'{"version": 1, "journals": {}}\n',
                }

            def request(self, method, path, data=None):
                if path in ("/api/v1/repositories/99", "/api/v1/repos/dest/control"):
                    return {
                        "id": 99,
                        "full_name": "dest/control",
                        "default_branch": "main",
                        "private": True,
                        "has_actions": False,
                    }
                from repo_mirror.migrations import blob_sha
                for name in ("config.json", "migrations.json", "failures.json"):
                    if path.startswith(f"/api/v1/repos/dest/control/contents/{name}"):
                        if method == "PUT":
                            self.files[name] = base64.b64decode(data["content"])
                            return {"content": {"sha": blob_sha(self.files[name])}}
                        content = self.files[name]
                        return {
                            "type": "file",
                            "encoding": "base64",
                            "sha": blob_sha(content),
                            "content": base64.b64encode(content).decode("ascii"),
                        }
                raise AssertionError(f"unexpected ForgejoControlAPI request: {method} {path}")

        class _FakeSrc:
            def request(self, method, url, data=None):
                if url == "https://gitlab.com/api/v4/groups/team%2Fsub":
                    return {"id": 7, "visibility": "public", "full_path": "team/sub"}
                raise AssertionError(f"unexpected source request: {url}")

            def pages(self, url):
                return []

        for flag in (False, True):
            with self.subTest(entrypoint="forgejo_schedule", graphql=flag), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                raw = json.dumps({**groups_forgejo, "graphql": flag}).encode("utf-8")
                (root / "manifest").write_bytes(raw)
                (root / "secret").write_text("x" * 32)
                (root / "secret").chmod(0o600)
                (root / "token").write_text("forgejo-token")
                (root / "token").chmod(0o600)
                (root / "journal").mkdir(mode=0o700)
                ctrl = _FakeControlAPI(raw)
                with (
                    patch("repo_mirror.forgejo_control_config.ForgejoControlAPI", return_value=ctrl),
                    patch("repo_mirror.forgejo_schedule._build_auto_discovery_source_client", return_value=lambda _: _FakeSrc()),
                    patch("repo_mirror.forgejo_schedule.subprocess.Popen"),
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()) as sched_err,
                ):
                    rc = forgejo_schedule.main(
                        [
                            "run",
                            "--manifest", str(root / "manifest"),
                            "--token-file", str(root / "token"),
                            "--timestamp-secret-file", str(root / "secret"),
                            "--journal-dir", str(root / "journal"),
                            "--owner", "dest",
                            "--lock", str(root / "lock"),
                            "--forgejo-config-repo", "dest/control",
                            "--forgejo-config-id", "99",
                            "--asset-max-bytes", "100",
                        ]
                    )
                self.assertEqual(rc, 0, sched_err.getvalue())

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "manifest").write_text(json.dumps({**groups_forgejo, "graphql": "false"}))
            (root / "secret").write_text("x" * 32)
            (root / "secret").chmod(0o600)
            (root / "token").write_text("forgejo-token")
            (root / "token").chmod(0o600)
            (root / "journal").mkdir(mode=0o700)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                rc = forgejo_schedule.main(
                    [
                        "run",
                        "--manifest", str(root / "manifest"),
                        "--token-file", str(root / "token"),
                        "--timestamp-secret-file", str(root / "secret"),
                        "--journal-dir", str(root / "journal"),
                        "--owner", "dest",
                        "--lock", str(root / "lock"),
                        "--forgejo-config-repo", "dest/control",
                        "--forgejo-config-id", "99",
                        "--asset-max-bytes", "100",
                    ]
                )
            self.assertEqual(rc, 2)
            self.assertIn("Batch configuration or lock error: ValueError", err.getvalue())

        # Forgejo entrypoints never issue GraphQL calls.
        self.assertEqual(api_call_metrics_snapshot(), {})

    def test_shared_cooldown_between_graphql_and_rest_workers_under_cli_main(self):
        config = {
            "destinations": {"main": {"account_type": "organization"}},
            "repositories": [
                {"source": {"url": "https://github.com/alice/demo"}, "mirrors": {"main": {"name": "demo"}}}
            ],
        }
        marker = source_marker({"type": "github", "base_url": "https://github.com", "owner": "alice"}, "demo")

        # 1. GraphQL exhausts bucket -> REST worker fails fast on shared cooldown without issuing REST requests.
        src_api = SourceAPI("synthetic-src")
        dst_api = API("synthetic-dst")
        src_calls = []
        dst_calls = []

        def src_open_1(req, timeout=60):
            src_calls.append(req.full_url)
            if req.full_url == GRAPHQL_URL:
                return _StubResponse(
                    {
                        "data": {
                            "r0": _gql_repo_node(11, "alice", "demo"),
                            "rateLimit": {"cost": 1, "remaining": 0, "resetAt": "2099-01-01T00:00:00Z"},
                        }
                    }
                )
            raise AssertionError(f"unexpected source REST call during cooldown: {req.full_url}")

        def dst_open_1(req, timeout=60):
            dst_calls.append((req.get_method(), req.full_url))
            if req.full_url == GRAPHQL_URL:
                return _StubResponse(
                    {
                        "data": {
                            "r0": _gql_repo_node(22, "dest", "demo", description=marker),
                            "rateLimit": {"cost": 1, "remaining": 4999, "resetAt": "2099-01-01T00:00:00Z"},
                        }
                    }
                )
            raise AssertionError(f"unexpected destination REST call during cooldown: {req.get_method()} {req.full_url}")

        _bind_opener(src_api, src_open_1)
        _bind_opener(dst_api, dst_open_1)

        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as cfg:
            json.dump(config, cfg)
            cfg.flush()
            with (
                patch.object(cli, "SourceAPI", return_value=src_api),
                patch.object(cli, "API", return_value=dst_api),
                patch.object(github_api.time, "sleep") as mock_sleep,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                with self.assertRaisesRegex(RuntimeError, "1 repositories failed"):
                    cli.main(["--config", cfg.name])
            self.assertIn("60 s wait limit is exhausted", stderr.getvalue())
            mock_sleep.assert_not_called()
        self.assertEqual(src_calls, [GRAPHQL_URL])
        self.assertEqual(dst_calls, [("POST", GRAPHQL_URL)])

        # 2. REST exhausts bucket -> GraphQL batch fails fast on shared cooldown with zero GraphQL/REST requests.
        self._clear_cooldowns()
        reset_api_call_metrics()
        rest_api = API("synthetic-rest")
        _bind_opener(
            rest_api,
            lambda req, timeout=60: _StubResponse(
                {"ok": True},
                headers={
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(github_api.time.time()) + 300),
                },
            ),
        )
        rest_api.request("GET", "https://api.github.com/repos/dest/demo")

        src_api2 = SourceAPI("synthetic-src")
        dst_api2 = API("synthetic-dst")
        src_mock2 = _bind_opener(
            src_api2,
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("source opener must not be called")),
        )
        dst_mock2 = _bind_opener(
            dst_api2,
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("destination opener must not be called")),
        )
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as cfg:
            json.dump(config, cfg)
            cfg.flush()
            with (
                patch.object(cli, "SourceAPI", return_value=src_api2),
                patch.object(cli, "API", return_value=dst_api2),
                patch.object(github_api.time, "sleep") as mock_sleep2,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaisesRegex(RuntimeError, "60 s wait limit is exhausted"):
                    cli.main(["--config", cfg.name])
            mock_sleep2.assert_not_called()
        src_mock2.assert_not_called()
        dst_mock2.assert_not_called()

    def test_retry_accounting_under_cli_main_on_batched_path(self):
        config = {
            "destinations": {"main": {"account_type": "organization"}},
            "repositories": [
                {"source": {"url": "https://github.com/alice/demo"}, "mirrors": {"main": {"name": "demo"}}}
            ],
        }
        marker = source_marker({"type": "github", "base_url": "https://github.com", "owner": "alice"}, "demo")

        # 1. Issued retry on destination GraphQL POST is counted once (2 destination + 1 source = 3 graphql).
        src_api = SourceAPI("synthetic-src")
        dst_api = API("synthetic-dst")
        dst_attempts = 0

        def dst_open_retry(req, timeout=60):
            nonlocal dst_attempts
            if req.full_url == GRAPHQL_URL:
                dst_attempts += 1
                if dst_attempts == 1:
                    raise HTTPError(GRAPHQL_URL, 500, "Server Error", Message(), io.BytesIO(b"err"))
                return _StubResponse(
                    {
                        "data": {
                            "r0": _gql_repo_node(22, "dest", "demo", description=marker),
                            "rateLimit": {"cost": 1, "remaining": 4999},
                        }
                    }
                )
            return _StubResponse({})

        _bind_opener(dst_api, dst_open_retry)
        _bind_opener(
            src_api,
            lambda req, timeout=60: _StubResponse(
                {
                    "data": {
                        "r0": _gql_repo_node(11, "alice", "demo"),
                        "rateLimit": {"cost": 1, "remaining": 4999},
                    }
                }
            ),
        )

        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as cfg:
            json.dump(config, cfg)
            cfg.flush()
            with (
                patch.object(cli, "SourceAPI", return_value=src_api),
                patch.object(cli, "API", return_value=dst_api),
                patch.object(github_api.time, "sleep"),
                patch("repo_mirror.graphql_api.time.sleep"),
                patch.object(repository, "mirror_git"),
                patch.object(repository, "mirror_releases"),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                cli.main(["--config", cfg.name])

        self.assertEqual(dst_attempts, 2)
        self.assertEqual(api_call_metrics_snapshot()[("github", "destination", "graphql", "POST")], 2)
        self.assertEqual(api_call_metrics_snapshot()[("github", "source", "graphql", "POST")], 1)
        self.assertEqual(graphql_points_total(), 2)
        self.assertIn("graphql=3 graphql_points=2", stderr.getvalue())
        self.assertIn("request retry 1/3", stderr.getvalue())

        # 2. Refused retry (Retry-After > 60s) is not retried and counted only once.
        self._clear_cooldowns()
        reset_api_call_metrics()
        src_api2 = SourceAPI("synthetic-src")
        dst_api2 = API("synthetic-dst")
        src_mock2 = _bind_opener(
            src_api2,
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("source opener must not be called")),
        )
        _bind_opener(
            dst_api2,
            lambda req, timeout=60: (_ for _ in ()).throw(
                HTTPError(GRAPHQL_URL, 429, "Too Many", {"Retry-After": "120"}, io.BytesIO(b"rate"))
            ),
        )
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as cfg:
            json.dump(config, cfg)
            cfg.flush()
            with (
                patch.object(cli, "SourceAPI", return_value=src_api2),
                patch.object(cli, "API", return_value=dst_api2),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(HTTPError):
                    cli.main(["--config", cfg.name])

        src_mock2.assert_not_called()
        self.assertEqual(api_call_metrics_snapshot(), {("github", "destination", "graphql", "POST"): 1})
        self.assertEqual(graphql_points_total(), 0)
        self.assertIn("graphql=1 graphql_points=0", format_api_call_summary())

    def test_starter_asset_refused_with_batched_metadata_under_cli_main(self):
        config = {
            "destinations": {"main": {"account_type": "personal"}},
            "repositories": [
                {"source": {"url": "https://github.com/alice/demo"}, "mirrors": {"main": {}}}
            ],
        }
        marker = source_marker({"type": "github", "base_url": "https://github.com", "owner": "alice"}, "demo")
        manifest = [["empty", 0, 10, None, None]]
        digest = hashlib.sha256(json.dumps(manifest, separators=(",", ":")).encode()).hexdigest()
        pending_name = "[repo-mirror:pending:v1]" + base64.urlsafe_b64encode(json.dumps([None, digest]).encode()).decode()

        src_api = SourceAPI("synthetic-src")
        dst_api = API("synthetic-dst")
        src_rest_repo_gets = []
        dst_calls = []

        def src_open(req, timeout=60):
            url = req.full_url
            if url == GRAPHQL_URL:
                return _StubResponse(
                    {
                        "data": {
                            "r0": _gql_repo_node(11, "alice", "demo"),
                            "rateLimit": {"cost": 1, "remaining": 4999},
                        }
                    }
                )
            if "/repos/alice/demo/releases/latest" in url:
                raise HTTPError(url, 404, "Not Found", Message(), io.BytesIO(b"{}"))
            if "/repos/alice/demo/releases/1/assets" in url:
                return _StubResponse(
                    []
                    if "page=2" in url
                    else [{"id": 10, "name": "empty", "size": 0, "browser_download_url": "https://github.com/alice/demo/releases/download/v1/empty"}]
                )
            if "/repos/alice/demo/releases/1" in url:
                return _StubResponse({"id": 1, "tag_name": "v1"})
            if "/repos/alice/demo/releases" in url:
                return _StubResponse([] if "page=2" in url else [{"id": 1, "tag_name": "v1"}])
            if url.endswith("/repos/alice/demo"):
                src_rest_repo_gets.append(url)
            raise AssertionError(f"unexpected source request: {url}")

        def dst_open(req, timeout=60):
            url = req.full_url
            method = req.get_method()
            if url == GRAPHQL_URL:
                return _StubResponse(
                    {
                        "data": {
                            "r0": _gql_repo_node(22, "dest", "mirror-alice-demo", description=marker),
                            "rateLimit": {"cost": 1, "remaining": 4999},
                        }
                    }
                )
            dst_calls.append((method, url))
            if method == "PUT" and url.endswith("/actions/permissions"):
                return _StubResponse({})
            if method == "GET" and "/releases/tags/" in url:
                return _StubResponse({"id": 2, "name": pending_name})
            if method == "GET" and "/releases/2/assets" in url:
                return _StubResponse(
                    []
                    if "page=2" in url
                    else [{"id": 20, "name": "empty", "size": 0, "state": "starter", "url": "https://api.github.com/a"}]
                )
            raise AssertionError(f"unexpected destination request: {method} {url}")

        _bind_opener(src_api, src_open)
        src_api.download = lambda url, out: None
        _bind_opener(dst_api, dst_open)

        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as cfg:
            json.dump(config, cfg)
            cfg.flush()
            with (
                patch.object(cli, "SourceAPI", return_value=src_api),
                patch.object(cli, "API", return_value=dst_api),
                patch.object(repository, "mirror_git"),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                with self.assertRaisesRegex(RuntimeError, "1 repositories failed"):
                    cli.main(["--config", cfg.name])

        self.assertRegex(stderr.getvalue().lower(), "starter.*manual")
        self.assertEqual(src_rest_repo_gets, [])
        self.assertFalse(any(m in ("DELETE", "POST", "PATCH") for m, _ in dst_calls))
        self.assertIn("graphql=2 graphql_points=2", stderr.getvalue())

    def test_failure_journal_written_and_cleared_on_batched_path_under_cli_main(self):
        config = {
            "destinations": {"main": {"account_type": "personal"}},
            "repositories": [
                {"source": {"url": "https://github.com/alice/demo"}, "mirrors": {"main": {}}}
            ],
        }
        marker = source_marker({"type": "github", "base_url": "https://github.com", "owner": "alice"}, "demo")

        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            journal_path = Path(d) / "failures.json"
            cfg.write_text(json.dumps(config))
            state = {"fail_upload": True, "release": None, "assets": []}

            def make_src_api(*_a, **_k):
                api = SourceAPI("synthetic-src")

                def src_open(req, timeout=60):
                    url = req.full_url
                    if url == GRAPHQL_URL:
                        return _StubResponse(
                            {
                                "data": {
                                    "r0": _gql_repo_node(11, "alice", "demo"),
                                    "rateLimit": {"cost": 1, "remaining": 4999},
                                }
                            }
                        )
                    if url.endswith("/releases/latest"):
                        raise HTTPError(url, 404, "Not Found", Message(), io.BytesIO(b"{}"))
                    if "/releases/1/assets" in url:
                        return _StubResponse(
                            []
                            if "page=2" in url
                            else [{"id": 10, "name": "file", "size": 3, "browser_download_url": "https://github.com/alice/demo/releases/download/v1/file"}]
                        )
                    if "/releases/1" in url:
                        return _StubResponse({"id": 1, "tag_name": "v1", "name": "one", "body": ""})
                    if "/releases" in url:
                        return _StubResponse(
                            [] if "page=2" in url else [{"id": 1, "tag_name": "v1", "name": "one", "body": ""}]
                        )
                    raise AssertionError(f"unexpected source request: {url}")

                _bind_opener(api, src_open)
                api.download = lambda url, out: out.write(b"abc")
                return api

            def make_dst_api(*_a, **_k):
                api = API("synthetic-dst")

                def dst_open(req, timeout=60):
                    url = req.full_url
                    method = req.get_method()
                    if url == GRAPHQL_URL:
                        return _StubResponse(
                            {
                                "data": {
                                    "r0": _gql_repo_node(22, "dest", "mirror-alice-demo", description=marker),
                                    "rateLimit": {"cost": 1, "remaining": 4999},
                                }
                            }
                        )
                    if method == "PUT" and url.endswith("/actions/permissions"):
                        return _StubResponse({})
                    if method == "GET" and "/releases/tags/" in url:
                        if state["release"] is None:
                            raise HTTPError(url, 404, "Not Found", Message(), io.BytesIO(b"{}"))
                        return _StubResponse(dict(state["release"]))
                    if method == "POST" and url.endswith("/releases"):
                        payload = json.loads(req.data.decode("utf-8"))
                        state["release"] = dict(payload, id=2, upload_url="https://uploads.github.com/a{?name}")
                        return _StubResponse(dict(state["release"]))
                    if method == "GET" and "/releases/2/assets" in url:
                        return _StubResponse([] if "page=2" in url else list(state["assets"]))
                    if method == "GET" and "/repos/dest/mirror-alice-demo/releases" in url:
                        return _StubResponse([])
                    if method == "POST" and url.startswith("https://uploads.github.com/a"):
                        if state["fail_upload"]:
                            raise RuntimeError("secret token must not be persisted")
                        state["assets"] = [
                            {"id": 20, "name": "file", "size": 3, "state": "uploaded", "url": "https://api.github.com/file"}
                        ]
                        return _StubResponse({"id": 9})
                    if method == "GET" and url == "https://api.github.com/file":
                        return _StubResponse(b"abc")
                    if method == "PATCH" and "/releases/" in url:
                        payload = json.loads(req.data.decode("utf-8"))
                        state["release"].update(payload)
                        return _StubResponse(dict(state["release"]))
                    raise AssertionError(f"unexpected destination request: {method} {url}")

                _bind_opener(api, dst_open)
                return api

            # Run 1: Upload fails after GraphQL batches succeed -> failure journal written.
            with (
                patch.object(cli, "SourceAPI", side_effect=make_src_api),
                patch.object(cli, "API", side_effect=[API(), make_dst_api()]),
                patch("repo_mirror.forgejo_source_adapter.ForgejoSourceAdapter.download", side_effect=lambda _u, out: out.write(b"abc")),
                patch.object(repository, "mirror_git"),
                contextlib.redirect_stderr(io.StringIO()) as err1,
            ):
                with self.assertRaisesRegex(RuntimeError, "1 repositories failed"):
                    cli.main(["--config", str(cfg), "--failures", str(journal_path)])
            self.assertTrue(journal_path.exists(), err1.getvalue())
            text = journal_path.read_text()
            self.assertNotIn("secret", text)
            self.assertEqual(len(json.loads(text)["failures"]), 1)
            self.assertIn("graphql=2 graphql_points=2", err1.getvalue())

            # Run 2: Retry succeeds on batched path -> failure journal cleared.
            reset_api_call_metrics()
            state["fail_upload"] = False
            with (
                patch.object(cli, "SourceAPI", side_effect=make_src_api),
                patch.object(cli, "API", side_effect=[API(), make_dst_api()]),
                patch("repo_mirror.forgejo_source_adapter.ForgejoSourceAdapter.download", side_effect=lambda _u, out: out.write(b"abc")),
                patch.object(repository, "mirror_git"),
                contextlib.redirect_stderr(io.StringIO()) as err2,
            ):
                cli.main(["--config", str(cfg), "--failures", str(journal_path)])
            self.assertEqual(json.loads(journal_path.read_text())["failures"], [])
            self.assertEqual(state["release"]["name"], "one")
            self.assertIn("graphql=2 graphql_points=2", err2.getvalue())

    def test_initial_destination_pin_mismatch_from_batch_under_cli_main(self):
        config = {
            "destinations": {"main": {"account_type": "organization"}},
            "repositories": [
                {
                    "source": {"url": "https://github.com/alice/demo", "id": 11},
                    "mirrors": {"main": {"name": "demo", "id": 22}},
                }
            ],
        }
        marker = source_marker({"type": "github", "base_url": "https://github.com", "owner": "alice"}, "demo")
        src_api = SourceAPI("synthetic-src")
        dst_api = API("synthetic-dst")
        src_calls = []
        dst_calls = []

        def src_open(req, timeout=60):
            src_calls.append(req.full_url)
            if req.full_url == GRAPHQL_URL:
                return _StubResponse(
                    {
                        "data": {
                            "r0": _gql_repo_node(11, "alice", "demo"),
                            "rateLimit": {"cost": 1, "remaining": 4999},
                        }
                    }
                )
            raise AssertionError(f"unexpected source REST call: {req.full_url}")

        def dst_open(req, timeout=60):
            dst_calls.append((req.get_method(), req.full_url))
            if req.full_url == GRAPHQL_URL:
                return _StubResponse(
                    {
                        "data": {
                            "r0": _gql_repo_node(888, "dest", "demo", description=marker),
                            "rateLimit": {"cost": 1, "remaining": 4999},
                        }
                    }
                )
            raise AssertionError(f"unexpected destination write/REST call: {req.get_method()} {req.full_url}")

        _bind_opener(src_api, src_open)
        src_api.download = lambda url, out: None
        _bind_opener(dst_api, dst_open)

        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as cfg:
            json.dump(config, cfg)
            cfg.flush()
            with (
                patch.object(cli, "SourceAPI", return_value=src_api),
                patch.object(cli, "API", return_value=dst_api),
                patch.object(repository, "mirror_git") as mock_git,
                patch.object(repository, "mirror_releases") as mock_releases,
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                with self.assertRaisesRegex(RuntimeError, "1 repositories failed"):
                    cli.main(["--config", cfg.name])

        self.assertIn("failed: destination repository id mismatch or missing", stderr.getvalue())
        self.assertEqual(src_calls, [GRAPHQL_URL])
        self.assertEqual(dst_calls, [("POST", GRAPHQL_URL)])
        mock_git.assert_not_called()
        mock_releases.assert_not_called()
        with self.assertRaisesRegex(ValueError, "destination repository id mismatch or missing"):
            repository.mirror_repository(
                {
                    "type": "github",
                    "base_url": "https://github.com",
                    "owner": "alice",
                    "name": "demo",
                    "mirror": {"source_id": 11, "destination_id": 22},
                },
                "demo",
                "dest",
                src_api,
                dst_api,
                "synthetic-token",
                existing_repo=_rest_repo_dict(888, "dest", "demo", description=marker),
                source_repo=_rest_repo_dict(11, "alice", "demo"),
            )

    def test_malformed_database_id_fails_hard_before_repository_work_under_cli_main(self):
        config = {
            "destinations": {"main": {"account_type": "organization"}},
            "repositories": [
                {"source": {"url": "https://github.com/alice/demo"}, "mirrors": {"main": {"name": "demo"}}}
            ],
        }
        bad_ids = ("missing", None, "R_kgDOAbCdEf", True, False, 0, -1)

        for target in ("destination", "source"):
            for bad_id in bad_ids:
                with self.subTest(target=target, bad_id=bad_id):
                    reset_api_call_metrics()
                    src_api = SourceAPI("synthetic-src")
                    dst_api = API("synthetic-dst")

                    def bad_node(owner, name, value=bad_id):
                        node = _gql_repo_node(123, owner, name)
                        if value == "missing":
                            node.pop("databaseId")
                        else:
                            node["databaseId"] = value
                        return node

                    if target == "destination":
                        _bind_opener(
                            dst_api,
                            lambda req, timeout=60: _StubResponse(
                                {"data": {"r0": bad_node("dest", "demo"), "rateLimit": {"cost": 1, "remaining": 4999}}}
                            ),
                        )
                        _bind_opener(
                            src_api,
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("source opener must not be called")),
                        )
                    else:
                        _bind_opener(
                            dst_api,
                            lambda req, timeout=60: _StubResponse(
                                {
                                    "data": {
                                        "r0": _gql_repo_node(22, "dest", "demo"),
                                        "rateLimit": {"cost": 1, "remaining": 4999},
                                    }
                                }
                            ),
                        )
                        _bind_opener(
                            src_api,
                            lambda req, timeout=60: _StubResponse(
                                {"data": {"r0": bad_node("alice", "demo"), "rateLimit": {"cost": 1, "remaining": 4999}}}
                            ),
                        )

                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as cfg:
                        json.dump(config, cfg)
                        cfg.flush()
                        with (
                            patch.object(cli, "SourceAPI", return_value=src_api),
                            patch.object(cli, "API", return_value=dst_api),
                            patch.object(cli, "mirror_repository") as mock_mirror,
                            patch.object(repository, "mirror_git") as mock_git,
                            patch.object(repository, "mirror_releases") as mock_releases,
                            contextlib.redirect_stderr(io.StringIO()),
                        ):
                            with self.assertRaisesRegex(
                                GraphQLError, "GraphQL repository response missing integer databaseId"
                            ):
                                cli.main(["--config", cfg.name])
                    mock_mirror.assert_not_called()
                    mock_git.assert_not_called()
                    mock_releases.assert_not_called()

    def test_auto_discovery_onboarding_through_cli_main_with_graphql_on_and_off(self):
        class _MemStore:
            def __init__(self, raw_bytes):
                self.data = raw_bytes
                self.sha = "a" * 40
                self.published = []

            def read(self):
                return self.data, self.sha

            def publish(self, expected_sha, replacement):
                self.published.append(replacement)
                self.data = replacement
                self.sha = "b" * 40

        for graphql_flag in (True, False):
            with self.subTest(graphql=graphql_flag):
                reset_api_call_metrics()
                doc = {
                    "graphql": graphql_flag,
                    "destinations": {"main": {"account_type": "personal"}},
                    "auto_discoveries": [{"url": "https://github.com/Alice"}],
                    "repositories": [],
                }
                raw = json.dumps(doc).encode("utf-8")
                store = _MemStore(raw)
                src_api = SourceAPI("synthetic-src")
                dst_api = API("synthetic-dst")
                created_desc = {}
                gql_calls = []
                rest_repo_gets = []

                def src_open(req, timeout=60):
                    url = req.full_url
                    if url == GRAPHQL_URL:
                        gql_calls.append("source")
                        return _StubResponse(
                            {
                                "data": {
                                    "r0": _gql_repo_node(201, "Alice", "alpha"),
                                    "rateLimit": {"cost": 1, "remaining": 4999},
                                }
                            }
                        )
                    if url == "https://api.github.com/users/Alice":
                        return _StubResponse({"id": 101, "login": "Alice", "type": "User"})
                    if url.startswith("https://api.github.com/users/Alice/repos"):
                        if "page=2" in url:
                            return _StubResponse([])
                        return _StubResponse(
                            [
                                {
                                    "id": 201,
                                    "name": "alpha",
                                    "full_name": "Alice/alpha",
                                    "clone_url": "https://github.com/Alice/alpha.git",
                                }
                            ]
                        )
                    if url == "https://api.github.com/repos/Alice/alpha":
                        rest_repo_gets.append(("source", url))
                        return _StubResponse(_rest_repo_dict(201, "Alice", "alpha"))
                    raise AssertionError(f"unexpected source request: {url}")

                def dst_open(req, timeout=60):
                    url = req.full_url
                    method = req.get_method()
                    if url == GRAPHQL_URL:
                        gql_calls.append("destination")
                        return _StubResponse(
                            {
                                "data": {
                                    "r0": _gql_repo_node(
                                        5001,
                                        "dest",
                                        "mirror-Alice-alpha",
                                        description=created_desc.get("desc"),
                                    ),
                                    "rateLimit": {"cost": 1, "remaining": 4999},
                                }
                            }
                        )
                    if method == "GET" and url == "https://api.github.com/user":
                        return _StubResponse({"login": "dest"})
                    if method == "GET" and url == "https://api.github.com/repos/dest/mirror-Alice-alpha":
                        if "desc" not in created_desc:
                            raise HTTPError(url, 404, "Not Found", Message(), io.BytesIO(b"{}"))
                        rest_repo_gets.append(("destination", url))
                        return _StubResponse(
                            _rest_repo_dict(
                                5001,
                                "dest",
                                "mirror-Alice-alpha",
                                description=created_desc["desc"],
                                private=created_desc["private"],
                            )
                        )
                    if method == "POST" and url == "https://api.github.com/user/repos":
                        payload = json.loads(req.data.decode("utf-8"))
                        created_desc["desc"] = payload["description"]
                        created_desc["private"] = payload["private"]
                        return _StubResponse(
                            _rest_repo_dict(
                                5001,
                                "dest",
                                "mirror-Alice-alpha",
                                description=payload["description"],
                                private=payload["private"],
                            )
                        )
                    if method == "PUT" and url == "https://api.github.com/repos/dest/mirror-Alice-alpha/actions/permissions":
                        return _StubResponse({})
                    if method == "GET" and url == "https://api.github.com/repos/dest/mirror-Alice-alpha/actions/permissions":
                        return _StubResponse({"enabled": False})
                    if method == "PATCH" and url == "https://api.github.com/repos/dest/mirror-Alice-alpha":
                        return _StubResponse({})
                    raise AssertionError(f"unexpected destination request: {method} {url}")

                _bind_opener(src_api, src_open)
                _bind_opener(dst_api, dst_open)

                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as cfg:
                    cfg.write(raw.decode("utf-8"))
                    cfg.flush()
                    with (
                        patch.object(cli, "SourceAPI", return_value=src_api),
                        patch.object(cli, "API", return_value=dst_api),
                        patch("repo_mirror.github_migration.transaction_from_environment", return_value=None),
                        patch("repo_mirror.github_migration.store_from_environment", return_value=store),
                        patch.object(repository, "mirror_git") as mock_git,
                        patch.object(repository, "mirror_releases") as mock_releases,
                        contextlib.redirect_stderr(io.StringIO()) as stderr,
                    ):
                        cli.main(["--config", cfg.name])

                mock_git.assert_called_once()
                mock_releases.assert_called_once()
                self.assertEqual(len(store.published), 1)
                published_doc = json.loads(store.published[0].decode("utf-8"))
                self.assertIs(published_doc["graphql"], graphql_flag)
                if graphql_flag:
                    self.assertEqual(gql_calls, ["destination", "source"])
                    self.assertIn("graphql=2 graphql_points=2", stderr.getvalue())
                else:
                    self.assertEqual(gql_calls, [])
                    self.assertIn("graphql=0 graphql_points=0", stderr.getvalue())
                    self.assertGreater(len(rest_repo_gets), 0)

    def test_default_omitted_graphql_key_uses_graphql(self):
        config = {
            "destinations": {"main": {"account_type": "organization"}},
            "repositories": [
                {"source": {"url": "https://github.com/alice/demo"}, "mirrors": {"main": {"name": "demo"}}}
            ],
        }
        self.assertNotIn("graphql", config)
        marker = source_marker({"type": "github", "base_url": "https://github.com", "owner": "alice"}, "demo")
        src_api = SourceAPI("synthetic-src")
        dst_api = API("synthetic-dst")

        def src_open(req, timeout=60):
            if req.full_url == GRAPHQL_URL:
                return _StubResponse(
                    {
                        "data": {
                            "r0": _gql_repo_node(11, "alice", "demo"),
                            "rateLimit": {"cost": 1, "remaining": 4999},
                        }
                    }
                )
            raise AssertionError(f"unexpected source REST call: {req.full_url}")

        def dst_open(req, timeout=60):
            if req.full_url == GRAPHQL_URL:
                return _StubResponse(
                    {
                        "data": {
                            "r0": _gql_repo_node(22, "dest", "demo", description=marker),
                            "rateLimit": {"cost": 1, "remaining": 4999},
                        }
                    }
                )
            if req.get_method() == "PUT" and req.full_url.endswith("/actions/permissions"):
                return _StubResponse({})
            raise AssertionError(f"unexpected destination call: {req.get_method()} {req.full_url}")

        _bind_opener(src_api, src_open)
        src_api.download = lambda url, out: None
        _bind_opener(dst_api, dst_open)

        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as cfg:
            json.dump(config, cfg)
            cfg.flush()
            with (
                patch.object(cli, "SourceAPI", return_value=src_api),
                patch.object(cli, "API", return_value=dst_api),
                patch.object(repository, "mirror_git"),
                patch.object(repository, "mirror_releases"),
                contextlib.redirect_stderr(io.StringIO()) as stderr,
            ):
                cli.main(["--config", cfg.name])

        metrics = api_call_metrics_snapshot()
        self.assertEqual(metrics[("github", "destination", "graphql", "POST")], 1)
        self.assertEqual(metrics[("github", "source", "graphql", "POST")], 1)
        self.assertEqual(graphql_points_total(), 2)
        self.assertIn("graphql=2 graphql_points=2", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
