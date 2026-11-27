"""Unit tests for RFC 8288 Link-header pagination in API.pages()."""

import http.client
import json
import unittest
from unittest.mock import patch

from repo_mirror.github_api import API, SourceAPI
from repo_mirror.logging_utils import error_detail


class _FakeResponse:
    def __init__(self, payload, headers=None):
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = headers if headers is not None else {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _make_opener(pages_by_call, recorded_urls):
    def fake_open(request, timeout=60):
        recorded_urls.append(request.full_url)
        idx = len(recorded_urls) - 1
        payload, headers = pages_by_call[idx]
        return _FakeResponse(payload, headers)

    return fake_open


class LinkHeaderPaginationTests(unittest.TestCase):
    BASE_URL = "https://api.github.com/repos/octocat/demo/releases"

    def test_stops_after_last_page_with_link_headers(self):
        # 3 pages of 2 items each = 6 items total.
        # GitHub-style Link headers on pages 1..2 with rel="next", and rel="prev", rel="first" on page 3.
        pages_with_prev = [
            ([{"id": 1}, {"id": 2}], {"Link": f'<{self.BASE_URL}?page=2>; rel="next", <{self.BASE_URL}?page=3>; rel="last"'}),
            ([{"id": 3}, {"id": 4}], {"Link": f'<{self.BASE_URL}?page=3>; rel="next", <{self.BASE_URL}?page=1>; rel="prev"'}),
            ([{"id": 5}, {"id": 6}], {"Link": f'<{self.BASE_URL}?page=2>; rel="prev", <{self.BASE_URL}?page=1>; rel="first"'}),
        ]
        # Same fixture with Link omitted on the final page.
        pages_omitted_on_last = [
            ([{"id": 1}, {"id": 2}], {"Link": f'<{self.BASE_URL}?page=2>; rel="next"'}),
            ([{"id": 3}, {"id": 4}], {"Link": f'<{self.BASE_URL}?page=3>; rel="next"'}),
            ([{"id": 5}, {"id": 6}], {}),
        ]
        for label, fixture in (("github_prev_first", pages_with_prev), ("omitted_on_last", pages_omitted_on_last)):
            with self.subTest(label=label):
                recorded = []
                api = API()
                with patch.object(api.opener, "open", side_effect=_make_opener(fixture, recorded)):
                    items = api.pages(self.BASE_URL)
                self.assertEqual(items, [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}, {"id": 6}])
                self.assertEqual(len(recorded), 3)

    def test_single_page_with_rel_last_stops_in_one_request(self):
        recorded = []
        api = API()
        fixture = [([{"id": 1}], {"Link": f'<{self.BASE_URL}?page=1>; rel="last"'})]
        with patch.object(api.opener, "open", side_effect=_make_opener(fixture, recorded)):
            items = api.pages(self.BASE_URL)
        self.assertEqual(items, [{"id": 1}])
        self.assertEqual(len(recorded), 1)

    def test_no_link_header_falls_back_to_empty_trailing_page(self):
        # 3 non-empty short pages + 1 empty trailing page = 4 requests (N+1).
        recorded = []
        api = API()
        fixture = [
            ([{"id": 1}, {"id": 2}], {}),
            ([{"id": 3}, {"id": 4}], {}),
            ([{"id": 5}, {"id": 6}], {}),
            ([], {}),
        ]
        with patch.object(api.opener, "open", side_effect=_make_opener(fixture, recorded)):
            items = api.pages(self.BASE_URL)
        self.assertEqual(items, [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}, {"id": 6}])
        self.assertEqual(
            recorded,
            [
                f"{self.BASE_URL}?per_page=100&limit=100&page=1",
                f"{self.BASE_URL}?per_page=100&limit=100&page=2",
                f"{self.BASE_URL}?per_page=100&limit=100&page=3",
                f"{self.BASE_URL}?per_page=100&limit=100&page=4",
            ],
        )

    def test_empty_first_page_costs_one_request(self):
        for headers in ({}, {"Link": f'<{self.BASE_URL}?page=2>; rel="next"'}):
            with self.subTest(headers=headers):
                recorded = []
                api = API()
                with patch.object(api.opener, "open", side_effect=_make_opener([([], headers)], recorded)):
                    items = api.pages(self.BASE_URL)
                self.assertEqual(items, [])
                self.assertEqual(len(recorded), 1)

    def test_uses_rel_next_url_verbatim(self):
        base_url = f"{self.BASE_URL}?type=all"
        next_url = f"{self.BASE_URL}?page=2&per_page=100&type=all"
        recorded = []
        api = API()
        fixture = [
            ([{"id": 1}], {"Link": f'<{next_url}>; rel="next"'}),
            ([{"id": 2}], {"Link": f'<{base_url}&page=1>; rel="prev"'}),
        ]
        with patch.object(api.opener, "open", side_effect=_make_opener(fixture, recorded)):
            items = api.pages(base_url)
        self.assertEqual(items, [{"id": 1}, {"id": 2}])
        self.assertEqual(recorded, [f"{base_url}&per_page=100&limit=100&page=1", next_url])

    def test_parses_commas_in_urls_quoted_params_and_multiple_link_headers(self):
        base_url = f"{self.BASE_URL}?filter=a,b"
        url_with_comma = f"{self.BASE_URL}?filter=a,b&page=2"
        msg1 = http.client.HTTPMessage()
        msg1.add_header("Link", f'<{self.BASE_URL}?a=1>; title="first, item; x"; rel="prev"')
        msg1.add_header("Link", f'<{url_with_comma}>; title="next, page; y"; rel="next"')
        msg2 = http.client.HTTPMessage()
        msg2.add_header("Link", f'<{base_url}&page=1>; rel="prev"')

        recorded = []
        api = API()
        fixture = [
            ([{"id": 1}], msg1),
            ([{"id": 2}], msg2),
        ]
        with patch.object(api.opener, "open", side_effect=_make_opener(fixture, recorded)):
            items = api.pages(base_url)
        self.assertEqual(items, [{"id": 1}, {"id": 2}])
        self.assertEqual(recorded, [f"{base_url}&per_page=100&limit=100&page=1", url_with_comma])

    def test_malformed_or_unrelated_link_headers_do_not_loop_or_truncate(self):
        cases = [
            ("missing_rel", {"Link": f"<{self.BASE_URL}?page=99>"}),
            ("garbage", {"Link": "not-a-valid-link-header"}),
            ("unrelated_rel", {"Link": f'<{self.BASE_URL}/schema>; rel="describedby"'}),
            ("prev_only_on_first_page", {"Link": f'<{self.BASE_URL}?page=0>; rel="prev"'}),
        ]
        for label, bad_header in cases:
            with self.subTest(label=label):
                recorded = []
                api = API()
                fixture = [
                    ([{"id": 1}], bad_header),
                    ([{"id": 2}], {}),
                    ([], {}),
                ]
                with patch.object(api.opener, "open", side_effect=_make_opener(fixture, recorded)):
                    items = api.pages(self.BASE_URL)
                self.assertEqual(items, [{"id": 1}, {"id": 2}])
                self.assertEqual(len(recorded), 3)

    def test_prev_only_on_last_page_after_following_next_stops_without_looping(self):
        recorded = []
        api = API()
        fixture = [
            ([{"id": 1}], {"Link": f'<{self.BASE_URL}?page=2>; rel="next"'}),
            ([{"id": 2}], {"Link": f'<{self.BASE_URL}?page=1>; rel="prev"'}),
        ]
        with patch.object(api.opener, "open", side_effect=_make_opener(fixture, recorded)):
            items = api.pages(self.BASE_URL)
        self.assertEqual(items, [{"id": 1}, {"id": 2}])
        self.assertEqual(len(recorded), 2)

    def test_cycle_and_cross_origin_guards(self):
        api = API()
        cycle_url = f"{self.BASE_URL}?per_page=100&limit=100&page=1"
        with (
            patch.object(
                api.opener,
                "open",
                side_effect=_make_opener([([{"id": 1}], {"Link": f'<{cycle_url}>; rel="next"'})], []),
            ),
            self.assertRaisesRegex(RuntimeError, "cycle"),
        ):
            api.pages(self.BASE_URL)

        with (
            patch.object(
                api.opener,
                "open",
                side_effect=_make_opener(
                    [([{"id": 1}], {"Link": '<https://evil.example/repos/a/b/releases?page=2>; rel="next"'})],
                    [],
                ),
            ),
            self.assertRaisesRegex(ValueError, "outside configured origin"),
        ):
            api.pages(self.BASE_URL)

    def test_source_api_inherits_link_and_fallback_paths(self):
        forge_url = "https://forge.example/api/v1/repos/octocat/demo/releases"
        for label, api_factory, base_url, use_link in (
            ("github_link", lambda: SourceAPI("token"), self.BASE_URL, True),
            ("forgejo_link", lambda: SourceAPI(None), forge_url, True),
            ("forgejo_fallback", lambda: SourceAPI(None), forge_url, False),
        ):
            with self.subTest(label=label):
                recorded = []
                api = api_factory()
                if use_link:
                    fixture = [
                        ([{"id": 1}], {"Link": f'<{base_url}?page=2>; rel="next"'}),
                        ([{"id": 2}], {"Link": f'<{base_url}?page=1>; rel="prev"'}),
                    ]
                    expected_requests = 2
                else:
                    fixture = [
                        ([{"id": 1}], {}),
                        ([{"id": 2}], {}),
                        ([], {}),
                    ]
                    expected_requests = 3
                with patch.object(api.opener, "open", side_effect=_make_opener(fixture, recorded)):
                    items = api.pages(base_url)
                self.assertEqual(items, [{"id": 1}, {"id": 2}])
                self.assertEqual(len(recorded), expected_requests)

    def test_rejects_unsafe_or_off_path_next_links(self):
        vectors = (
            ("relative_dotdot_user_repos", "../../../../user/repos", lambda: API("secret-dest-tok")),
            ("relative_dotdot_user_emails", "../../../../user/emails", lambda: SourceAPI("secret-src-tok")),
            ("root_relative_other_repo", "/repos/victim/private-repo/releases", lambda: API("secret-dest-tok")),
            ("same_origin_other_endpoint", "https://api.github.com/user/installations", lambda: API("secret-dest-tok")),
            ("path_params_smuggling", "/repos/octocat/demo/releases;x=1?page=2", lambda: API("secret-dest-tok")),
            ("trailing_slash_drift", f"{self.BASE_URL}/?page=2", lambda: API("secret-dest-tok")),
            ("cross_origin", "https://evil.example/repos/octocat/demo/releases?page=2", lambda: API("secret-dest-tok")),
            ("protocol_relative", "//evil.example/repos/octocat/demo/releases?page=2", lambda: API("secret-dest-tok")),
            ("file_scheme", "file:///etc/passwd", lambda: API("secret-dest-tok")),
            ("http_scheme", "http://api.github.com/repos/octocat/demo/releases?page=2", lambda: API("secret-dest-tok")),
            ("userinfo", "https://user:pass@api.github.com/repos/octocat/demo/releases?page=2", lambda: API("secret-dest-tok")),
            ("port_change", "https://api.github.com:8443/repos/octocat/demo/releases?page=2", lambda: API("secret-dest-tok")),
            ("fragment", f"{self.BASE_URL}?page=2#frag", lambda: API("secret-dest-tok")),
        )
        for label, next_link, api_factory in vectors:
            with self.subTest(label=label):
                recorded = []
                api = api_factory()
                fixture = [
                    ([{"id": 1}], {"Link": f'<{next_link}>; rel="next"'}),
                    ([{"id": 99}], {}),
                ]
                with (
                    patch.object(api.opener, "open", side_effect=_make_opener(fixture, recorded)),
                    self.assertRaises(ValueError),
                ):
                    api.pages(self.BASE_URL)
                self.assertEqual(len(recorded), 1)

    def test_rejects_query_parameter_drift_in_next_links(self):
        cases = (
            ("per_page_drift", self.BASE_URL, f"{self.BASE_URL}?page=2&per_page=500"),
            ("limit_drift", self.BASE_URL, f"{self.BASE_URL}?page=2&limit=500"),
            ("added_sort_param", self.BASE_URL, f"{self.BASE_URL}?page=2&sort=updated"),
            ("added_cursor_param", self.BASE_URL, f"{self.BASE_URL}?page=2&after=opaque"),
            ("dropped_caller_param", f"{self.BASE_URL}?type=all", f"{self.BASE_URL}?page=2"),
            ("mutated_caller_param", f"{self.BASE_URL}?type=all", f"{self.BASE_URL}?type=private&page=2"),
            ("missing_page_param", self.BASE_URL, f"{self.BASE_URL}?per_page=100"),
            ("page_zero", self.BASE_URL, f"{self.BASE_URL}?page=0"),
            ("page_non_integer", self.BASE_URL, f"{self.BASE_URL}?page=abc"),
        )
        for label, base_url, next_link in cases:
            with self.subTest(label=label):
                recorded = []
                api = API("secret-dest-tok")
                fixture = [
                    ([{"id": 1}], {"Link": f'<{next_link}>; rel="next"'}),
                    ([{"id": 99}], {}),
                ]
                with (
                    patch.object(api.opener, "open", side_effect=_make_opener(fixture, recorded)),
                    self.assertRaises(ValueError),
                ):
                    api.pages(base_url)
                self.assertEqual(len(recorded), 1)

    def test_allows_same_path_with_valid_pagination_and_caller_params(self):
        cases = (
            ("page_only", self.BASE_URL, f"{self.BASE_URL}?page=2"),
            ("page_and_per_page", self.BASE_URL, f"{self.BASE_URL}?per_page=100&page=2"),
            ("page_and_limit", self.BASE_URL, f"{self.BASE_URL}?limit=100&page=2"),
            ("reordered_caller_param", f"{self.BASE_URL}?type=all", f"{self.BASE_URL}?page=2&per_page=100&type=all"),
        )
        for label, base_url, next_link in cases:
            with self.subTest(label=label):
                recorded = []
                api = API("secret-dest-tok")
                fixture = [
                    ([{"id": 1}], {"Link": f'<{next_link}>; rel="next"'}),
                    ([{"id": 2}], {"Link": f'<{base_url}>; rel="prev"'}),
                ]
                with patch.object(api.opener, "open", side_effect=_make_opener(fixture, recorded)):
                    items = api.pages(base_url)
                self.assertEqual(items, [{"id": 1}, {"id": 2}])
                self.assertEqual(len(recorded), 2)

    def test_allows_github_canonical_numeric_id_next_links(self):
        cases = (
            (
                "repo_releases_api",
                lambda: API("secret-dest-tok"),
                "https://api.github.com/repos/alice/demo/releases",
                [
                    (
                        [{"id": 1}],
                        {
                            "Link": (
                                '<https://api.github.com/repositories/12345/releases?per_page=100&limit=100&page=2>; rel="next", '
                                '<https://api.github.com/repositories/12345/releases?per_page=100&limit=100&page=3>; rel="last"'
                            )
                        },
                    ),
                    (
                        [{"id": 2}],
                        {
                            "Link": '<https://api.github.com/repositories/12345/releases?per_page=100&limit=100&page=3>; rel="next"'
                        },
                    ),
                    ([{"id": 3}], {"Link": '<https://api.github.com/repositories/12345/releases?page=2>; rel="prev"'}),
                ],
                3,
            ),
            (
                "repo_releases_source_api",
                lambda: SourceAPI("secret-src-tok"),
                "https://api.github.com/repos/alice/demo/releases",
                [
                    (
                        [{"id": 1}],
                        {
                            "Link": '<https://api.github.com/repositories/12345/releases?per_page=100&limit=100&page=2>; rel="next"'
                        },
                    ),
                    ([{"id": 2}], {"Link": '<https://api.github.com/repositories/12345/releases?page=1>; rel="prev"'}),
                ],
                2,
            ),
            (
                "release_assets",
                lambda: API("secret-dest-tok"),
                "https://api.github.com/repos/alice/demo/releases/7/assets",
                [
                    (
                        [{"id": 1}],
                        {
                            "Link": '<https://api.github.com/repositories/12345/releases/7/assets?per_page=100&limit=100&page=2>; rel="next"'
                        },
                    ),
                    ([{"id": 2}], {}),
                ],
                2,
            ),
            (
                "org_repos",
                lambda: SourceAPI("secret-src-tok"),
                "https://api.github.com/orgs/acme/repos",
                [
                    (
                        [{"id": 1}],
                        {
                            "Link": '<https://api.github.com/organizations/99/repos?per_page=100&limit=100&page=2>; rel="next"'
                        },
                    ),
                    ([{"id": 2}], {}),
                ],
                2,
            ),
            (
                "user_repos",
                lambda: SourceAPI("secret-src-tok"),
                "https://api.github.com/users/alice/repos",
                [
                    (
                        [{"id": 1}],
                        {"Link": '<https://api.github.com/user/42/repos?per_page=100&limit=100&page=2>; rel="next"'},
                    ),
                    ([{"id": 2}], {}),
                ],
                2,
            ),
        )
        for label, api_factory, base_url, fixture, expected_count in cases:
            with self.subTest(label=label):
                recorded = []
                api = api_factory()
                with patch.object(api.opener, "open", side_effect=_make_opener(fixture, recorded)):
                    items = api.pages(base_url)
                self.assertEqual(items, [{"id": i} for i in range(1, expected_count + 1)])
                self.assertEqual(len(recorded), expected_count)

    def test_rejects_unpinned_or_mismatched_canonical_pagination_links(self):
        cases = (
            (
                "switched_numeric_id_on_page_2",
                lambda: API("secret-dest-tok"),
                "https://api.github.com/repos/alice/demo/releases",
                [
                    (
                        [{"id": 1}],
                        {"Link": '<https://api.github.com/repositories/12345/releases?page=2>; rel="next"'},
                    ),
                    (
                        [{"id": 2}],
                        {"Link": '<https://api.github.com/repositories/67890/releases?page=3>; rel="next"'},
                    ),
                ],
                2,
            ),
            (
                "subpath_mismatch",
                lambda: API("secret-dest-tok"),
                "https://api.github.com/repos/alice/demo/releases",
                [
                    (
                        [{"id": 1}],
                        {"Link": '<https://api.github.com/repositories/12345/issues?page=2>; rel="next"'},
                    ),
                ],
                1,
            ),
            (
                "non_github_origin",
                lambda: SourceAPI(None),
                "https://forge.example/api/v1/repos/alice/demo/releases",
                [
                    (
                        [{"id": 1}],
                        {"Link": '<https://forge.example/repositories/12345/releases?page=2>; rel="next"'},
                    ),
                ],
                1,
            ),
            (
                "zero_numeric_id",
                lambda: API("secret-dest-tok"),
                "https://api.github.com/repos/alice/demo/releases",
                [
                    (
                        [{"id": 1}],
                        {"Link": '<https://api.github.com/repositories/0/releases?page=2>; rel="next"'},
                    ),
                ],
                1,
            ),
            (
                "non_numeric_id",
                lambda: API("secret-dest-tok"),
                "https://api.github.com/repos/alice/demo/releases",
                [
                    (
                        [{"id": 1}],
                        {"Link": '<https://api.github.com/repositories/abc/releases?page=2>; rel="next"'},
                    ),
                ],
                1,
            ),
        )
        for label, api_factory, base_url, fixture, expected_requests in cases:
            with self.subTest(label=label):
                recorded = []
                api = api_factory()
                with (
                    patch.object(api.opener, "open", side_effect=_make_opener(fixture, recorded)),
                    self.assertRaisesRegex(ValueError, "pagination link outside configured origin"),
                ):
                    api.pages(base_url)
                self.assertEqual(len(recorded), expected_requests)

    def test_pagination_errors_are_not_stripped_to_bare_class_names(self):
        self.assertEqual(
            error_detail(ValueError("pagination link outside configured origin")),
            "pagination link outside configured origin",
        )
        self.assertEqual(
            error_detail(RuntimeError("pagination cycle detected")),
            "pagination cycle detected",
        )


if __name__ == "__main__":
    unittest.main()
