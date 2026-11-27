import contextlib
import io
import json
import time
import unittest
from email.message import Message
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from repo_mirror import github_api
from repo_mirror.github_api import (
    API,
    SourceAPI,
    api_call_metrics_snapshot,
    format_api_call_summary,
    graphql_points_total,
    reset_api_call_metrics,
)
from repo_mirror.graphql_api import (
    DEFAULT_ASSETS_PER_RELEASE,
    DEFAULT_MAX_POINTS,
    GRAPHQL_URL,
    HARD_MAX_POINTS,
    MAX_ASSETS_PER_RELEASE,
    MAX_RELEASES_PER_REPO,
    MAX_REPOS_PER_QUERY,
    GraphQLClient,
    GraphQLError,
    build_release_assets_query,
    build_releases_query,
    build_repositories_query,
    estimate_query_points,
)
from repo_mirror.logging_utils import error_detail


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


class GraphQLClientTests(unittest.TestCase):
    def setUp(self):
        reset_api_call_metrics()
        with github_api._cooldown_lock:
            github_api._cooldowns.clear()
        self.addCleanup(reset_api_call_metrics)
        self.addCleanup(self._clear_cooldowns)

    def _clear_cooldowns(self):
        with github_api._cooldown_lock:
            github_api._cooldowns.clear()

    def test_execute_success_parses_data_records_points_and_auth(self):
        src = SourceAPI(lambda: "synthetic-source-token")
        captured_requests = []

        def fake_open(req, timeout=60):
            captured_requests.append(req)
            return _StubResponse(
                {
                    "data": {
                        "r0": {"name": "demo", "nameWithOwner": "alice/demo"},
                        "rateLimit": {"cost": 1, "remaining": 4999, "resetAt": "2099-01-01T00:00:00Z"},
                    }
                }
            )

        src.opener.open = MagicMock(side_effect=fake_open)
        client = GraphQLClient(src)

        repo = client.fetch_repository("alice", "demo")
        self.assertEqual(repo, {"name": "demo", "nameWithOwner": "alice/demo"})
        self.assertEqual(len(captured_requests), 1)
        req = captured_requests[0]
        self.assertEqual(req.full_url, GRAPHQL_URL)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer synthetic-source-token")
        self.assertEqual(client.last_rate_limit, {"cost": 1, "remaining": 4999, "resetAt": "2099-01-01T00:00:00Z"})
        self.assertEqual(api_call_metrics_snapshot(), {("github", "source", "graphql", "POST"): 1})
        self.assertEqual(graphql_points_total(), 1)
        self.assertEqual(
            format_api_call_summary(),
            "api_calls total=1 github=1 uploads=0 other=0 graphql=1 graphql_points=1 "
            "by_role=source:1 by_operation=graphql_post:1",
        )

    def test_source_restrict_origin_blocks_github_graphql(self):
        src = SourceAPI("synthetic-token")
        src.restrict_origin("https://forge.example.test/api/v1")
        src.opener.open = MagicMock(side_effect=AssertionError("opener must not be called"))
        client = GraphQLClient(src)
        with self.assertRaisesRegex(ValueError, "API request outside configured origin"):
            client.fetch_repository("alice", "demo")
        self.assertEqual(api_call_metrics_snapshot(), {})

    def test_graphql_errors_map_to_safe_messages_without_leaking_provider_text(self):
        cases = [
            ("RATE_LIMITED", "GraphQL request rate limited"),
            ("FORBIDDEN", "GraphQL request forbidden"),
            ("INSUFFICIENT_SCOPES", "GraphQL request forbidden"),
            ("NOT_FOUND", "GraphQL resource not found"),
            ("MAX_NODE_LIMIT_EXCEEDED", "GraphQL query node limit exceeded"),
            ("UNPROCESSABLE", "GraphQL request returned errors"),
        ]
        secret_msg = "Could not resolve to a Repository with the name 'private-org/secret-repo' at /srv/internal"
        for err_type, expected_safe in cases:
            with self.subTest(err_type=err_type):
                api = API("synthetic-token")
                api.opener.open = MagicMock(
                    return_value=_StubResponse(
                        {
                            "data": {"rateLimit": {"cost": 1, "remaining": 4998}},
                            "errors": [{"type": err_type, "message": secret_msg}],
                        }
                    )
                )
                client = GraphQLClient(api)
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), self.assertRaises(GraphQLError) as caught:
                    client.fetch_repository("alice", "demo")

                exc = caught.exception
                self.assertEqual(str(exc), expected_safe)
                self.assertEqual(exc.safe_message, expected_safe)
                self.assertEqual(error_detail(exc), expected_safe)
                self.assertNotIn("private-org", str(exc))
                self.assertNotIn("secret-repo", repr(exc))
                self.assertNotIn("/srv/internal", error_detail(exc))
                self.assertNotIn("private-org", stderr.getvalue())
                self.assertEqual(exc.provider_messages, (secret_msg,))

    def test_http_429_and_403_publish_shared_cooldown_with_rest_and_respect_60s_budget(self):
        for status, headers, body in (
            (429, {"Retry-After": "120"}, b""),
            (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1120"}, b""),
            (403, {}, b'{"message":"You have exceeded a secondary rate limit"}'),
        ):
            with self.subTest(status=status, headers=headers):
                self._clear_cooldowns()
                reset_api_call_metrics()
                gql_api = API("synthetic-token")
                rest_api = API("synthetic-token")
                def make_err(*_args, status=status, headers=headers, body=body, **_kwargs):
                    err = HTTPError(GRAPHQL_URL, status, "Limited", headers, io.BytesIO(body))
                    self.addCleanup(err.close)
                    raise err

                gql_api.opener.open = MagicMock(side_effect=make_err)
                rest_api.opener.open = MagicMock(side_effect=AssertionError("REST opener must not be called"))

                client = GraphQLClient(gql_api)
                clock = [100.0]
                with (
                    patch("repo_mirror.github_api.time.time", return_value=1000.0),
                    patch("repo_mirror.github_api.time.monotonic", side_effect=lambda clock=clock: clock[0]),
                    patch(
                        "repo_mirror.github_api.time.sleep",
                        side_effect=lambda n, clock=clock: clock.__setitem__(0, clock[0] + n),
                    ) as sleep,
                ):
                    if status == 403 and not headers:
                        # Headerless secondary 403: attempt 0 sleeps 60s, attempt 1 publishes 120s cooldown and re-raises HTTPError.
                        with self.assertRaises(HTTPError):
                            client.fetch_repository("alice", "demo")
                        sleep.assert_called_once_with(60)
                    else:
                        with self.assertRaises(HTTPError) as caught:
                            client.fetch_repository("alice", "demo")
                        sleep.assert_not_called()
                        self.assertEqual(
                            error_detail(caught.exception),
                            f"HTTP {status} (destination graphql POST"
                            + ("; rate limit remaining=0)" if headers.get("X-RateLimit-Remaining") == "0" else ")"),
                        )

                    # REST call to api.github.com must now see the shared cooldown and fail fast without calling opener.
                    with self.assertRaisesRegex(RuntimeError, "already rate limited"):
                        rest_api.request("GET", "https://api.github.com/repos/alice/demo")
                    rest_api.opener.open.assert_not_called()

        # Conversely, a REST cooldown blocks GraphQL without hitting the network.
        self._clear_cooldowns()
        with github_api._cooldown_lock:
            github_api._cooldowns["api.github.com"] = time.monotonic() + 3600
        gql_api = API("synthetic-token")
        gql_api.opener.open = MagicMock(side_effect=AssertionError("GraphQL opener must not be called"))
        with self.assertRaisesRegex(RuntimeError, "already rate limited"):
            GraphQLClient(gql_api).fetch_repository("alice", "demo")

    def test_total_invariant_holds_with_rest_and_graphql_mixed(self):
        rest = API("synthetic-token")
        rest.opener.open = MagicMock(side_effect=lambda *a, **k: _StubResponse(b"{}"))
        gql_src = SourceAPI("synthetic-source-token")
        gql_src.opener.open = MagicMock(
            side_effect=[
                _StubResponse({"data": {"r0": {"name": "a"}, "rateLimit": {"cost": 1, "remaining": 4999}}}),
                _StubResponse({"data": {"r0": {"name": "b"}, "rateLimit": {"cost": 2, "remaining": 4997}}}),
            ]
        )

        rest.request("GET", "https://api.github.com/repos/dest/demo")
        rest.upload(
            "https://uploads.github.com/repos/dest/demo/releases/1/assets?name=a.bin",
            io.BytesIO(b"abc"),
            "application/octet-stream",
        )
        client = GraphQLClient(gql_src)
        client.fetch_repository("alice", "a")
        client.fetch_repository("alice", "b")

        summary = format_api_call_summary()
        self.assertEqual(
            summary,
            "api_calls total=4 github=3 uploads=1 other=0 graphql=2 graphql_points=3 "
            "by_role=destination:2,source:2 by_operation=api_get:1,api_post:1,graphql_post:2",
        )

    def test_cursor_pagination_follows_only_cursor_and_never_urls(self):
        api = SourceAPI("synthetic-token")
        seen_urls = []
        seen_cursors = []

        pages = [
            _StubResponse(
                {
                    "data": {
                        "r0": {
                            "releases": {
                                "pageInfo": {
                                    "hasNextPage": True,
                                    "endCursor": "cursor-1",
                                    "nextUrl": "https://evil.example.test/steal-token",
                                },
                                "nodes": [{"databaseId": 1, "tagName": "v1"}],
                            }
                        },
                        "rateLimit": {"cost": 1, "remaining": 4999},
                    }
                },
                headers={"Link": '<https://evil.example.test/link-steal>; rel="next"'},
            ),
            _StubResponse(
                {
                    "data": {
                        "r0": {
                            "releases": {
                                "pageInfo": {"hasNextPage": False, "endCursor": "cursor-2"},
                                "nodes": [{"databaseId": 2, "tagName": "v2"}],
                            }
                        },
                        "rateLimit": {"cost": 1, "remaining": 4998},
                    }
                }
            ),
        ]

        def fake_open(req, timeout=60):
            seen_urls.append(req.full_url)
            body = json.loads(req.data.decode("utf-8"))
            seen_cursors.append(body["variables"].get("after0"))
            return pages.pop(0)

        api.opener.open = MagicMock(side_effect=fake_open)
        client = GraphQLClient(api)
        releases = client.fetch_releases("alice", "demo", releases_per_page=10, assets_per_release=5)

        self.assertEqual([r["tagName"] for r in releases], ["v1", "v2"])
        self.assertEqual(seen_urls, [GRAPHQL_URL, GRAPHQL_URL])
        self.assertEqual(seen_cursors, [None, "cursor-1"])

    def test_cursor_pagination_detects_cycles_and_missing_cursors(self):
        api = SourceAPI("synthetic-token")
        api.opener.open = MagicMock(
            side_effect=[
                _StubResponse(
                    {
                        "data": {
                            "repository": {
                                "release": {
                                    "releaseAssets": {
                                        "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                                        "nodes": [{"name": "a.bin"}],
                                    }
                                }
                            },
                            "rateLimit": {"cost": 1, "remaining": 4999},
                        }
                    }
                ),
                _StubResponse(
                    {
                        "data": {
                            "repository": {
                                "release": {
                                    "releaseAssets": {
                                        "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                                        "nodes": [{"name": "b.bin"}],
                                    }
                                }
                            },
                            "rateLimit": {"cost": 1, "remaining": 4998},
                        }
                    }
                ),
            ]
        )
        client = GraphQLClient(api)
        with self.assertRaisesRegex(RuntimeError, "GraphQL pagination cycle detected"):
            client.fetch_release_assets("alice", "demo", "v1", assets_per_page=10)

    def test_bounded_query_shapes_and_fanout_caps(self):
        repos_100 = [("alice", f"repo-{i}") for i in range(MAX_REPOS_PER_QUERY)]
        query, variables, aliases = build_repositories_query(repos_100)
        self.assertEqual(len(aliases), 100)
        self.assertEqual(variables["name99"], "repo-99")
        self.assertIn("rateLimit { cost remaining resetAt }", query)

        # 2 repos * 100 releases * 100 assets == 20,202 nodes == 203 points > DEFAULT_MAX_POINTS -> refused
        with self.assertRaisesRegex(ValueError, "GraphQL query estimated point cost exceeds budget"):
            build_releases_query(
                [("alice", "one"), ("alice", "two")],
                releases_first=MAX_RELEASES_PER_REPO,
                assets_first=MAX_ASSETS_PER_RELEASE,
            )

        # 3 repos * 100 releases * 100 assets == 30,303 nodes == 304 points > DEFAULT_MAX_POINTS -> refused
        with self.assertRaisesRegex(ValueError, "GraphQL query estimated point cost exceeds budget"):
            build_releases_query(
                [("alice", "one"), ("alice", "two"), ("alice", "three")],
                releases_first=MAX_RELEASES_PER_REPO,
                assets_first=MAX_ASSETS_PER_RELEASE,
            )

        # Custom tighter point budget is enforced on client helpers too (111 nodes == 2 points > 1)
        client = GraphQLClient(SourceAPI("synthetic-token"), max_points=1)
        with self.assertRaisesRegex(ValueError, "GraphQL query estimated point cost exceeds budget"):
            client.fetch_releases("alice", "demo", releases_per_page=10, assets_per_release=10)

        # Per-dimension bounds and invalid parameters refused
        with self.assertRaisesRegex(ValueError, "invalid GraphQL query parameters"):
            build_repositories_query([("alice", f"r{i}") for i in range(101)])
        with self.assertRaisesRegex(ValueError, "invalid GraphQL query parameters"):
            build_releases_query([("alice", "demo")], releases_first=101, assets_first=1)
        with self.assertRaisesRegex(ValueError, "invalid GraphQL query parameters"):
            build_releases_query([("alice", "demo")], releases_first=1, assets_first=101)
        with self.assertRaisesRegex(ValueError, "invalid GraphQL query parameters"):
            build_release_assets_query("alice", "demo", "v1", assets_first=101)
        with self.assertRaisesRegex(ValueError, "invalid GraphQL query parameters"):
            build_repositories_query([("alice/evil", "demo")])
        with self.assertRaisesRegex(ValueError, "invalid GraphQL query parameters"):
            build_releases_query([("alice", "demo")], releases_first=True)

        self.assertEqual(DEFAULT_MAX_POINTS, 20)
        self.assertEqual(HARD_MAX_POINTS, 20)
        self.assertEqual(DEFAULT_ASSETS_PER_RELEASE, 0)

    def test_reviewer_a_reproductions_and_override_attempts_fail(self):
        # Reproduction 1: 2 repos x 100 releases x 100 assets (20,202 nodes -> 203 points)
        with self.assertRaisesRegex(ValueError, "GraphQL query estimated point cost exceeds budget"):
            build_releases_query(
                [("o", "r1"), ("o", "r2")],
                releases_first=100,
                assets_first=100,
            )

        # Reproduction 2: 100 repos x 100 releases x 2 assets (30,100 nodes -> 301 points)
        with self.assertRaisesRegex(ValueError, "GraphQL query estimated point cost exceeds budget"):
            build_releases_query(
                [("o", f"r{i}") for i in range(100)],
                releases_first=100,
                assets_first=2,
            )

        # Reproduction 3: override attempts via max_points=1_000_000 or max_fanout=1_000_000
        with self.assertRaisesRegex(ValueError, "GraphQL query estimated point cost exceeds budget"):
            build_releases_query(
                [("o", f"r{i}") for i in range(100)],
                releases_first=100,
                assets_first=100,
                max_points=1_000_000,
            )
        with self.assertRaises(TypeError):
            build_releases_query([("o", "r1")], max_fanout=1_000_000)

        api = SourceAPI("synthetic-token")
        api.opener.open = MagicMock(side_effect=AssertionError("opener must not be called"))
        client = GraphQLClient(api, max_points=1_000_000)
        self.assertEqual(client.max_points, HARD_MAX_POINTS)
        client.max_points = 1_000_000
        with self.assertRaisesRegex(ValueError, "GraphQL query estimated point cost exceeds budget"):
            client.fetch_releases("alice", "demo", releases_per_page=100, assets_per_release=100)
        api.opener.open.assert_not_called()

    def test_no_public_execute_bypass_and_private_gate_enforced(self):
        self.assertFalse(hasattr(GraphQLClient, "execute"))
        api = SourceAPI("synthetic-token")
        api.opener.open = MagicMock(side_effect=AssertionError("opener must not be called"))
        client = GraphQLClient(api)

        with self.assertRaisesRegex(ValueError, "GraphQL query point estimate is required"):
            client._execute("query { viewer { login } }")
        with self.assertRaisesRegex(ValueError, "GraphQL query estimated point cost exceeds budget"):
            client._execute("query { viewer { login } }", estimated_nodes=2001)
        api.opener.open.assert_not_called()
        self.assertEqual(api_call_metrics_snapshot(), {})

    def test_default_fetch_releases_fits_inside_budget(self):
        api = SourceAPI("synthetic-token")
        captured_bodies = []

        def fake_open(req, timeout=60):
            captured_bodies.append(json.loads(req.data.decode("utf-8")))
            return _StubResponse(
                {
                    "data": {
                        "r0": {
                            "releases": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [{"databaseId": 1, "tagName": "v1"}],
                            }
                        },
                        "rateLimit": {"cost": 1, "remaining": 4999},
                    }
                }
            )

        api.opener.open = MagicMock(side_effect=fake_open)
        client = GraphQLClient(api)
        releases = client.fetch_releases("alice", "demo")

        self.assertEqual([r["tagName"] for r in releases], ["v1"])
        self.assertEqual(len(captured_bodies), 1)
        self.assertEqual(captured_bodies[0]["variables"]["releasesFirst"], 100)
        self.assertNotIn("releaseAssets", captured_bodies[0]["query"])
        self.assertEqual(client.last_estimated_nodes, 101)
        self.assertEqual(client.last_estimated_points, 2)
        self.assertEqual(graphql_points_total(), 1)

    def test_over_budget_refuses_before_send_and_boundary_call_succeeds(self):
        self.assertEqual(estimate_query_points(1), 1)
        self.assertEqual(estimate_query_points(100), 1)
        self.assertEqual(estimate_query_points(101), 2)
        self.assertEqual(estimate_query_points(1901), 20)
        self.assertEqual(estimate_query_points(2000), 20)
        self.assertEqual(estimate_query_points(2001), 21)

        # Over-budget query refuses before opener is called
        blocked_api = SourceAPI("synthetic-token")
        blocked_api.opener.open = MagicMock(side_effect=AssertionError("opener must not be called"))
        blocked_client = GraphQLClient(blocked_api)
        with self.assertRaisesRegex(ValueError, "GraphQL query estimated point cost exceeds budget"):
            blocked_client.fetch_releases("alice", "demo", releases_per_page=100, assets_per_release=100)
        blocked_api.opener.open.assert_not_called()
        self.assertEqual(api_call_metrics_snapshot(), {})

        # 100 releases x 18 assets = 1 + 100 * 19 = 1,901 nodes -> 20 points (at budget) succeeds
        ok_api = SourceAPI("synthetic-token")
        ok_api.opener.open = MagicMock(
            return_value=_StubResponse(
                {
                    "data": {
                        "r0": {
                            "releases": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [{"databaseId": 1, "tagName": "v1"}],
                            }
                        },
                        "rateLimit": {"cost": 3, "remaining": 4997},
                    }
                }
            )
        )
        ok_client = GraphQLClient(ok_api)
        releases = ok_client.fetch_releases("alice", "demo", releases_per_page=100, assets_per_release=18)
        self.assertEqual(len(releases), 1)
        ok_api.opener.open.assert_called_once()
        self.assertEqual(ok_client.last_estimated_nodes, 1901)
        self.assertEqual(ok_client.last_estimated_points, 20)
        self.assertEqual(graphql_points_total(), 3)

        # 100 releases x 19 assets = 1 + 100 * 20 = 2,001 nodes -> 21 points (1 over budget) refuses before send
        ok_api.opener.open.reset_mock()
        with self.assertRaisesRegex(ValueError, "GraphQL query estimated point cost exceeds budget"):
            ok_client.fetch_releases("alice", "demo", releases_per_page=100, assets_per_release=19)
        ok_api.opener.open.assert_not_called()

    def test_no_token_query_text_or_response_body_reaches_log(self):
        token = "ghs_secretGraphQLTokenValue1234567890"
        private_owner = "private-example-owner"
        private_repo = "private-example-repo"
        secret_error_body = "secret-provider-diagnostic-payload"

        src = SourceAPI(token)
        src.opener.open = MagicMock(
            return_value=_StubResponse(
                {
                    "data": {"rateLimit": {"cost": 1, "remaining": 4999}},
                    "errors": [{"type": "NOT_FOUND", "message": secret_error_body}],
                }
            )
        )
        client = GraphQLClient(src)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(GraphQLError) as caught:
            client.fetch_repository(private_owner, private_repo)

        logged = stderr.getvalue()
        self.assertIn("network target=source operation=graphql method=POST elapsed=", logged)
        self.assertNotIn(token, logged)
        self.assertNotIn(private_owner, logged)
        self.assertNotIn(private_repo, logged)
        self.assertNotIn(secret_error_body, logged)
        self.assertNotIn("rateLimit", logged)
        self.assertEqual(error_detail(caught.exception), "GraphQL resource not found")


if __name__ == "__main__":
    unittest.main()
