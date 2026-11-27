import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from email.message import Message
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from repo_mirror import cli, github_api, repository
from repo_mirror.config_named import normalize_config
from repo_mirror.github_api import (
    API,
    SourceAPI,
    api_call_metrics_snapshot,
    format_api_call_summary,
    graphql_points_total,
    reset_api_call_metrics,
)
from repo_mirror.graphql_api import GRAPHQL_URL, GraphQLClient, GraphQLError, normalize_repository_node
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
            self.assertIn("failed: ValueError", stderr.getvalue())
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
            self.assertIn("failed: ValueError", stderr.getvalue())
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


if __name__ == "__main__":
    unittest.main()
