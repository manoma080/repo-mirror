"""Unit tests for RFC 8288 Link-header pagination in API.pages()."""

import http.client
import json
import unittest
from unittest.mock import patch

from repo_mirror.github_api import API, SourceAPI


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
        cursor_url = f"{self.BASE_URL}?after=opaque_cursor_99&per_page=100&custom=1"
        recorded = []
        api = API()
        fixture = [
            ([{"id": 1}], {"Link": f'<{cursor_url}>; rel="next"'}),
            ([{"id": 2}], {"Link": f'<{self.BASE_URL}?page=1>; rel="prev"'}),
        ]
        with patch.object(api.opener, "open", side_effect=_make_opener(fixture, recorded)):
            items = api.pages(self.BASE_URL)
        self.assertEqual(items, [{"id": 1}, {"id": 2}])
        self.assertEqual(recorded, [f"{self.BASE_URL}?per_page=100&limit=100&page=1", cursor_url])

    def test_parses_commas_in_urls_quoted_params_and_multiple_link_headers(self):
        url_with_comma = f"{self.BASE_URL}?filter=a,b&cursor=xyz"
        msg1 = http.client.HTTPMessage()
        msg1.add_header("Link", f'<{self.BASE_URL}?a=1>; title="first, item; x"; rel="prev"')
        msg1.add_header("Link", f'<{url_with_comma}>; title="next, page; y"; rel="next"')
        msg2 = http.client.HTTPMessage()
        msg2.add_header("Link", f'<{self.BASE_URL}?page=1>; rel="prev"')

        recorded = []
        api = API()
        fixture = [
            ([{"id": 1}], msg1),
            ([{"id": 2}], msg2),
        ]
        with patch.object(api.opener, "open", side_effect=_make_opener(fixture, recorded)):
            items = api.pages(self.BASE_URL)
        self.assertEqual(items, [{"id": 1}, {"id": 2}])
        self.assertEqual(recorded, [f"{self.BASE_URL}?per_page=100&limit=100&page=1", url_with_comma])

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


if __name__ == "__main__":
    unittest.main()
