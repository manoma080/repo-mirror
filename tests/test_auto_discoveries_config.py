"""Schema, validation, and round-trip tests for ``auto_discoveries``."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from repo_mirror import cli
from repo_mirror.adhoc import parse_owner_url
from repo_mirror.config_named import normalize_config
from repo_mirror.logging_utils import error_detail
from repo_mirror.migrations import blob_sha, plan_migration, rewrite_config


def _base_config(**extra):
    config = {
        "destinations": {"main": {"account_type": "personal"}},
        "repositories": [],
    }
    config.update(extra)
    return config


class ParseOwnerUrlTests(unittest.TestCase):
    def test_valid_owner_urls(self):
        self.assertEqual(
            parse_owner_url("https://GitHub.com/BlackBearRevived"),
            ("github", "https://github.com", ("BlackBearRevived",)),
        )
        self.assertEqual(
            parse_owner_url("https://forge.example/Alice"),
            ("forgejo", "https://forge.example", ("Alice",)),
        )
        self.assertEqual(
            parse_owner_url("https://gitlab.com/user1"),
            ("gitlab", "https://gitlab.com", ("user1",)),
        )
        self.assertEqual(
            parse_owner_url("https://gitlab.com/Team/SubGroup"),
            ("gitlab", "https://gitlab.com", ("Team", "SubGroup")),
        )
        self.assertEqual(
            parse_owner_url("https://code.example/Team/Sub", source_type="gitlab"),
            ("gitlab", "https://code.example", ("Team", "Sub")),
        )
        self.assertEqual(
            parse_owner_url("https://forge.example:8443/Alice", allow_forgejo_port=True),
            ("forgejo", "https://forge.example:8443", ("Alice",)),
        )

    def test_rejects_non_string_and_empty(self):
        for bad in (None, 123, "", "   "):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_owner_url(bad)

    def test_rejects_invalid_scheme_and_credentials(self):
        for bad in (
            "http://github.com/owner",
            "https://user:pass@github.com/owner",
            "https://user@github.com/owner",
            "https://@github.com/owner",
            "https://github.com:/owner",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_owner_url(bad)

    def test_rejects_query_fragment_git_trailing_slash_whitespace_control(self):
        for bad in (
            "https://github.com/owner?a=1",
            "https://github.com/owner?",
            "https://github.com/owner#frag",
            "https://github.com/owner#",
            "https://github.com/owner.git",
            "https://gitlab.com/team.git/sub",
            "https://github.com/owner/",
            "https://github.com",
            "https://github.com/",
            "https://github.com/owner ",
            " https://github.com/owner",
            "https://github.com/ow ner",
            "https://github.com/owner\t",
            "https://github.com/owner\n",
            "https://github.com/owner\x00",
            "https://github.com/owner\x7f",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_owner_url(bad)

    def test_rejects_dot_segments_bad_names_and_empty_segments(self):
        for bad in (
            "https://github.com/.",
            "https://github.com/..",
            "https://gitlab.com/team/./sub",
            "https://gitlab.com/team/../sub",
            "https://gitlab.com/team//sub",
            "https://github.com/owner/repo",
            "https://forge.example/owner/repo",
            "https://github.com/bad$owner",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_owner_url(bad)

    def test_rejects_port_by_default_and_bad_dns_labels(self):
        for bad in (
            "https://forge.example:8443/owner",
            "https://github.com.evil.com/owner",
            "https://-bad.example/owner",
            "https://bad..example/owner",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_owner_url(bad)

    def test_rejects_mismatched_or_invalid_source_type(self):
        cases = (
            ("https://github.com/owner", "forgejo"),
            ("https://github.com/owner", "gitlab"),
            ("https://gitlab.com/owner", "forgejo"),
            ("https://gitlab.com/owner", "github"),
            ("https://forge.example/owner", "github"),
            ("https://forge.example/owner", "bitbucket"),
            ("https://forge.example/owner", ""),
            ("https://forge.example/owner", 123),
        )
        for url, source_type in cases:
            with self.subTest(url=url, source_type=source_type), self.assertRaises(ValueError):
                parse_owner_url(url, source_type=source_type)


class AutoDiscoveriesValidConfigTests(unittest.TestCase):
    def test_valid_minimal_github_rule_defaults(self):
        cfg = _base_config(auto_discoveries=[{"url": "https://GitHub.com/BlackBearRevived"}])
        normalized = normalize_config(cfg)
        self.assertEqual(
            normalized["auto_discoveries"],
            [
                {
                    "url": "https://github.com/BlackBearRevived",
                    "type": "github",
                    "base_url": "https://github.com",
                    "owner": "BlackBearRevived",
                    "visibility": "private",
                    "ignore_releases": False,
                    "destination_name_prefix": "mirror-BlackBearRevived",
                    "exclude": [],
                    "overrides": {},
                }
            ],
        )

    def test_valid_minimal_forgejo_rule(self):
        cfg = _base_config(auto_discoveries=[{"url": "https://forge.example/Alice"}])
        normalized = normalize_config(cfg)
        self.assertEqual(normalized["auto_discoveries"][0]["type"], "forgejo")
        self.assertEqual(normalized["auto_discoveries"][0]["destination_name_prefix"], "mirror-Alice")
        self.assertNotIn("id", normalized["auto_discoveries"][0])

    def test_valid_empty_auto_discoveries_list(self):
        cfg = _base_config(auto_discoveries=[])
        normalized = normalize_config(cfg)
        self.assertEqual(normalized["auto_discoveries"], [])

    def test_valid_full_rule_with_exclude_overrides_and_id(self):
        cfg = _base_config(
            auto_discoveries=[
                {
                    "url": "https://github.com/BlackBearRevived",
                    "type": "github",
                    "id": 123456,
                    "visibility": "public",
                    "ignore_releases": True,
                    "destination_name_prefix": "custom-prefix",
                    "exclude": ["some-repo", "Archived"],
                    "overrides": {
                        "other-repo": {
                            "name": "renamed-other",
                            "visibility": "private",
                            "ignore_releases": False,
                        }
                    },
                }
            ]
        )
        normalized = normalize_config(cfg)
        self.assertEqual(
            normalized["auto_discoveries"][0],
            {
                "url": "https://github.com/BlackBearRevived",
                "type": "github",
                "base_url": "https://github.com",
                "owner": "BlackBearRevived",
                "id": 123456,
                "visibility": "public",
                "ignore_releases": True,
                "destination_name_prefix": "custom-prefix",
                "exclude": ["some-repo", "Archived"],
                "overrides": {
                    "other-repo": {
                        "name": "renamed-other",
                        "visibility": "private",
                        "ignore_releases": False,
                    }
                },
            },
        )

    def test_valid_gitlab_rules_and_nested_default_prefix(self):
        cfg = _base_config(
            auto_discoveries=[
                {"url": "https://gitlab.com/single-user", "id": 11},
                {"url": "https://gitlab.com/team/sub", "type": "gitlab", "id": 22},
                {"url": "https://code.example/org/dept", "type": "gitlab", "id": 33},
            ]
        )
        normalized = normalize_config(cfg)
        rules = normalized["auto_discoveries"]
        self.assertEqual(rules[0]["destination_name_prefix"], "mirror-single-user")
        self.assertEqual(rules[1]["destination_name_prefix"], "mirror-team-sub")
        self.assertEqual(rules[2]["destination_name_prefix"], "mirror-org-dept")
        self.assertEqual(rules[2]["type"], "gitlab")

    def test_same_id_allowed_on_different_origins(self):
        cfg = _base_config(
            auto_discoveries=[
                {"url": "https://github.com/org-a", "id": 42},
                {"url": "https://forge.example/org-b", "id": 42},
            ]
        )
        normalized = normalize_config(cfg)
        self.assertEqual(len(normalized["auto_discoveries"]), 2)


class AutoDiscoveriesRejectionTests(unittest.TestCase):
    def test_rejects_unknown_top_level_key(self):
        cfg = _base_config(auto_discoveries=[], extra_key=True)
        with self.assertRaises(ValueError):
            normalize_config(cfg)

    def test_rejects_auto_discoveries_in_legacy_config_without_named_keys(self):
        cfg = {"destination_account_type": "personal", "sources": [], "auto_discoveries": []}
        with self.assertRaisesRegex(ValueError, "legacy configuration format is no longer supported"):
            normalize_config(cfg)

    def test_rejects_non_list_auto_discoveries(self):
        for bad in ({}, "https://github.com/owner", None, 1):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_config(_base_config(auto_discoveries=bad))

    def test_rejects_non_dict_rule(self):
        with self.assertRaises(ValueError):
            normalize_config(_base_config(auto_discoveries=["https://github.com/owner"]))

    def test_rejects_missing_url_in_rule(self):
        with self.assertRaises(ValueError):
            normalize_config(_base_config(auto_discoveries=[{"type": "github"}]))

    def test_rejects_unknown_key_in_rule(self):
        with self.assertRaises(ValueError):
            normalize_config(
                _base_config(auto_discoveries=[{"url": "https://github.com/owner", "include_forks": True}])
            )

    def test_rejects_invalid_rule_urls(self):
        for bad_url in (
            "https://github.com/owner?tab=repositories",
            "https://github.com/owner#top",
            "https://github.com/owner.git",
            "https://github.com/owner/",
            "https://github.com/owner ",
            "https://github.com/.",
            "https://github.com/..",
            "https://user:pass@github.com/owner",
            "https://github.com/owner/repo",
            "https://forge.example/owner/repo",
        ):
            with self.subTest(url=bad_url), self.assertRaises(ValueError):
                normalize_config(_base_config(auto_discoveries=[{"url": bad_url}]))

    def test_rejects_invalid_rule_type(self):
        cases = (
            {"url": "https://github.com/owner", "type": "forgejo"},
            {"url": "https://github.com/owner", "type": "gitlab"},
            {"url": "https://gitlab.com/owner", "type": "forgejo", "id": 1},
            {"url": "https://gitlab.com/owner", "type": "github", "id": 1},
            {"url": "https://forge.example/owner", "type": "github"},
            {"url": "https://forge.example/owner", "type": "other"},
            {"url": "https://forge.example/owner", "type": ""},
            {"url": "https://forge.example/owner", "type": None},
        )
        for rule in cases:
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                normalize_config(_base_config(auto_discoveries=[rule]))

    def test_rejects_invalid_id(self):
        for bad_id in (True, False, 0, -1, "123", 1.5, None):
            with self.subTest(bad_id=bad_id), self.assertRaises(ValueError):
                normalize_config(
                    _base_config(auto_discoveries=[{"url": "https://github.com/owner", "id": bad_id}])
                )

    def test_rejects_missing_id_for_gitlab_rules(self):
        for rule in (
            {"url": "https://gitlab.com/team/sub"},
            {"url": "https://gitlab.com/user1"},
            {"url": "https://code.example/team/sub", "type": "gitlab"},
        ):
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                normalize_config(_base_config(auto_discoveries=[rule]))

    def test_rejects_invalid_visibility(self):
        for bad in ("internal", None, 123, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_config(
                    _base_config(auto_discoveries=[{"url": "https://github.com/owner", "visibility": bad}])
                )

    def test_rejects_invalid_ignore_releases(self):
        for bad in ("true", "false", 1, 0, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_config(
                    _base_config(auto_discoveries=[{"url": "https://github.com/owner", "ignore_releases": bad}])
                )

    def test_rejects_invalid_destination_name_prefix(self):
        for bad in ("", ".", "..", "bad/prefix", "bad name", 123, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_config(
                    _base_config(
                        auto_discoveries=[{"url": "https://github.com/owner", "destination_name_prefix": bad}]
                    )
                )

    def test_rejects_invalid_exclude(self):
        for bad in (
            "repo",
            {},
            [123],
            ["."],
            [".."],
            ["bad/name"],
            ["Alpha", "alpha"],
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_config(
                    _base_config(auto_discoveries=[{"url": "https://github.com/owner", "exclude": bad}])
                )

    def test_rejects_invalid_overrides_structure_and_keys(self):
        for bad in (
            [],
            "repo",
            {".": {"visibility": "public"}},
            {"..": {"visibility": "public"}},
            {"bad/repo": {"visibility": "public"}},
            {"Alpha": {"visibility": "public"}, "alpha": {"visibility": "private"}},
            {"repo": {}},
            {"repo": "public"},
            {"repo": {"visibility": "public", "unknown": 1}},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_config(
                    _base_config(auto_discoveries=[{"url": "https://github.com/owner", "overrides": bad}])
                )

    def test_rejects_overrides_overlapping_exclude_case_insensitively(self):
        cfg = _base_config(
            auto_discoveries=[
                {
                    "url": "https://github.com/owner",
                    "exclude": ["Some-Repo"],
                    "overrides": {"some-repo": {"visibility": "public"}},
                }
            ]
        )
        with self.assertRaises(ValueError):
            normalize_config(cfg)

    def test_rejects_invalid_override_values_and_name_limits(self):
        bad_overrides = (
            {"repo": {"name": ""}},
            {"repo": {"name": "."}},
            {"repo": {"name": ".."}},
            {"repo": {"name": "bad/name"}},
            {"repo": {"name": "a" * 101}},
            {"repo": {"name": "gitlab-" + ("a1" * 12)}},
            {"repo": {"visibility": "internal"}},
            {"repo": {"ignore_releases": "yes"}},
            {"repo": {"ignore_releases": 1}},
            {"r" * 90: {"visibility": "public"}},
        )
        for overrides in bad_overrides:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                normalize_config(
                    _base_config(
                        auto_discoveries=[
                            {
                                "url": "https://github.com/owner",
                                "destination_name_prefix": "mirror-owner",
                                "overrides": overrides,
                            }
                        ]
                    )
                )

    def test_rejects_duplicate_rule_by_origin_and_owner_case_insensitively(self):
        cfg = _base_config(
            auto_discoveries=[
                {"url": "https://github.com/Alice", "id": 1},
                {"url": "https://GitHub.com/alice", "id": 2},
            ]
        )
        with self.assertRaises(ValueError):
            normalize_config(cfg)

    def test_rejects_duplicate_rule_by_origin_and_id(self):
        cfg = _base_config(
            auto_discoveries=[
                {"url": "https://github.com/alice", "id": 99},
                {"url": "https://GitHub.com/bob", "id": 99},
            ]
        )
        with self.assertRaises(ValueError):
            normalize_config(cfg)


class DiscoveredByAndCollisionTests(unittest.TestCase):
    def test_discovered_by_accepted_and_preserved_on_normalized_source(self):
        cfg = _base_config(
            auto_discoveries=[{"url": "https://github.com/Alice"}],
            repositories=[
                {
                    "source": {"url": "https://github.com/Alice/alpha", "id": 10},
                    "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 20}},
                    "discovered_by": "https://github.com/Alice",
                }
            ],
        )
        normalized = normalize_config(cfg)
        self.assertEqual(normalized["repositories"][0]["discovered_by"], "https://github.com/Alice")

    def test_rejects_orphan_discovered_by(self):
        cases = (
            _base_config(
                repositories=[
                    {
                        "source": {"url": "https://github.com/Alice/alpha", "id": 10},
                        "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 20}},
                        "discovered_by": "https://github.com/Alice",
                    }
                ]
            ),
            _base_config(
                auto_discoveries=[{"url": "https://github.com/Bob"}],
                repositories=[
                    {
                        "source": {"url": "https://github.com/Alice/alpha", "id": 10},
                        "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 20}},
                        "discovered_by": "https://github.com/Alice",
                    }
                ],
            ),
            _base_config(
                auto_discoveries=[{"url": "https://github.com/Alice"}],
                repositories=[
                    {
                        "source": {"url": "https://github.com/Alice/alpha", "id": 10},
                        "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 20}},
                        "discovered_by": "https://github.com/Alice/",
                    }
                ],
            ),
            _base_config(
                auto_discoveries=[{"url": "https://github.com/Alice"}],
                repositories=[
                    {
                        "source": {"url": "https://github.com/Alice/alpha", "id": 10},
                        "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 20}},
                        "discovered_by": 123,
                    }
                ],
            ),
        )
        for cfg in cases:
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                normalize_config(cfg)

    def test_rejects_extra_key_on_repository_entry(self):
        cfg = _base_config(
            auto_discoveries=[{"url": "https://github.com/Alice"}],
            repositories=[
                {
                    "source": {"url": "https://github.com/Alice/alpha", "id": 10},
                    "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 20}},
                    "discovered_by": "https://github.com/Alice",
                    "unexpected": True,
                }
            ],
        )
        with self.assertRaises(ValueError):
            normalize_config(cfg)

    def test_rejects_discovered_by_source_owner_mismatch(self):
        cfg = _base_config(
            auto_discoveries=[{"url": "https://github.com/Alice"}],
            repositories=[
                {
                    "source": {"url": "https://github.com/Bob/alpha", "id": 10},
                    "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 20}},
                    "discovered_by": "https://github.com/Alice",
                }
            ],
        )
        with self.assertRaises(ValueError):
            normalize_config(cfg)

    def test_rejects_discovered_by_destination_name_mismatch_and_over_100_chars(self):
        mismatch_cfg = _base_config(
            auto_discoveries=[
                {"url": "https://github.com/Alice", "destination_name_prefix": "pref"}
            ],
            repositories=[
                {
                    "source": {"url": "https://github.com/Alice/alpha", "id": 10},
                    "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 20}},
                    "discovered_by": "https://github.com/Alice",
                }
            ],
        )
        with self.assertRaises(ValueError):
            normalize_config(mismatch_cfg)

        too_long_cfg = _base_config(
            auto_discoveries=[
                {"url": "https://github.com/Alice", "destination_name_prefix": "p" * 80}
            ],
            repositories=[
                {
                    "source": {"url": "https://github.com/Alice/" + ("r" * 25), "id": 10},
                    "mirrors": {"main": {"id": 20}},
                    "discovered_by": "https://github.com/Alice",
                }
            ],
        )
        with self.assertRaises(ValueError):
            normalize_config(too_long_cfg)

    def test_rejects_destination_name_collisions_case_insensitively(self):
        child_collision = _base_config(
            auto_discoveries=[{"url": "https://github.com/Alice"}],
            repositories=[
                {
                    "source": {"url": "https://github.com/Other/one", "id": 1},
                    "mirrors": {"main": {"name": "MIRROR-ALICE-ALPHA", "id": 2}},
                },
                {
                    "source": {"url": "https://github.com/Alice/alpha", "id": 3},
                    "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 4}},
                    "discovered_by": "https://github.com/Alice",
                },
            ],
        )
        with self.assertRaises(ValueError):
            normalize_config(child_collision)

        override_collision = _base_config(
            auto_discoveries=[
                {
                    "url": "https://github.com/Alice",
                    "overrides": {"alpha": {"name": "Shared-Dest"}},
                },
                {
                    "url": "https://github.com/Bob",
                    "overrides": {"beta": {"name": "shared-dest"}},
                },
            ],
        )
        with self.assertRaises(ValueError):
            normalize_config(override_collision)

        override_vs_repo_collision = _base_config(
            auto_discoveries=[
                {
                    "url": "https://github.com/Alice",
                    "overrides": {"alpha": {"name": "Shared-Dest"}},
                }
            ],
            repositories=[
                {
                    "source": {"url": "https://github.com/Other/one", "id": 1},
                    "mirrors": {"main": {"name": "shared-dest", "id": 2}},
                }
            ],
        )
        with self.assertRaises(ValueError):
            normalize_config(override_vs_repo_collision)

    def test_same_repo_in_override_and_discovered_by_does_not_self_collide(self):
        cfg = _base_config(
            auto_discoveries=[
                {
                    "url": "https://github.com/Alice",
                    "overrides": {"alpha": {"name": "custom-alpha"}},
                }
            ],
            repositories=[
                {
                    "source": {"url": "https://github.com/Alice/alpha", "id": 10},
                    "mirrors": {"main": {"name": "custom-alpha", "id": 20}},
                    "discovered_by": "https://github.com/Alice",
                }
            ],
        )
        normalized = normalize_config(cfg)
        self.assertEqual(normalized["repositories"][0]["discovered_by"], "https://github.com/Alice")

    def test_retained_legacy_gitlab_24hex_name_allowed_with_discovered_by(self):
        cfg = _base_config(
            auto_discoveries=[{"url": "https://gitlab.com/team/sub", "id": 7}],
            repositories=[
                {
                    "source": {"url": "https://gitlab.com/team/sub/alpha", "id": 10},
                    "mirrors": {"main": {"name": "gitlab-" + ("0123456789abcdef" * 1 + "01234567"), "id": 20}},
                    "discovered_by": "https://gitlab.com/team/sub",
                }
            ],
        )
        normalized = normalize_config(cfg)
        self.assertEqual(normalized["repositories"][0]["discovered_by"], "https://gitlab.com/team/sub")


class RoundTripAndRuntimeGuardTests(unittest.TestCase):
    def test_round_trip_through_normalize_and_rewrite_config(self):
        doc = {
            "destinations": {"main": {"account_type": "personal"}},
            "auto_discoveries": [
                {
                    "url": "https://github.com/Alice",
                    "id": 101,
                    "visibility": "private",
                    "ignore_releases": False,
                    "destination_name_prefix": "mirror-Alice",
                    "exclude": ["ignored"],
                    "overrides": {"beta": {"visibility": "public"}},
                }
            ],
            "repositories": [
                {
                    "source": {"url": "https://github.com/Other/explicit", "id": 1},
                    "mirrors": {"main": {"name": "mirror-Other-explicit", "id": 2}},
                },
                {
                    "source": {"url": "https://github.com/Alice/alpha", "id": 11},
                    "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 12}},
                    "discovered_by": "https://github.com/Alice",
                },
                {
                    "source": {"url": "https://github.com/Alice/beta", "id": 21},
                    "mirrors": {"main": {"name": "mirror-Alice-beta", "id": 22}},
                    "discovered_by": "https://github.com/Alice",
                },
            ],
        }
        normalized_before = normalize_config(doc)
        self.assertEqual(len(normalized_before["auto_discoveries"]), 1)
        self.assertEqual(normalized_before["repositories"][1]["discovered_by"], "https://github.com/Alice")

        raw = (json.dumps(doc, indent=2) + "\n").encode("utf-8")
        secret = "fixture-signing-secret-0123456789abcdef"

        # 1. Migrating an unrelated explicit entry leaves auto_discoveries and discovered_by entries intact.
        explicit_relocation = {
            "source_origin": "https://api.github.com",
            "source_id": 1,
            "original_owner": "Other",
            "original_name": "explicit",
            "owner": "OtherNew",
            "name": "explicit-renamed",
            "destination": {"profile": "main", "owner": "dest", "name": "mirror-Other-explicit", "id": 2},
        }
        entry1 = plan_migration(
            explicit_relocation,
            {"document": copy.deepcopy(doc), "path": "config.json", "base_sha": blob_sha(raw)},
            secret=secret,
        )
        rewritten1 = rewrite_config(raw, entry1)
        parsed1 = json.loads(rewritten1)
        self.assertEqual(parsed1["auto_discoveries"], doc["auto_discoveries"])
        self.assertEqual(parsed1["repositories"][1]["discovered_by"], "https://github.com/Alice")
        self.assertEqual(parsed1["repositories"][2]["discovered_by"], "https://github.com/Alice")
        self.assertEqual(normalize_config(parsed1)["auto_discoveries"], normalized_before["auto_discoveries"])

        # 2. Same-owner rename of a discovered_by entry preserves discovered_by.
        same_owner_relocation = {
            "source_origin": "https://api.github.com",
            "source_id": 11,
            "original_owner": "Alice",
            "original_name": "alpha",
            "owner": "Alice",
            "name": "alpha-renamed",
            "destination": {"profile": "main", "owner": "dest", "name": "mirror-Alice-alpha", "id": 12},
        }
        entry2 = plan_migration(
            same_owner_relocation,
            {"document": copy.deepcopy(parsed1), "path": "config.json", "base_sha": blob_sha(rewritten1)},
            secret=secret,
        )
        rewritten2 = rewrite_config(rewritten1, entry2)
        parsed2 = json.loads(rewritten2)
        self.assertEqual(parsed2["repositories"][1]["discovered_by"], "https://github.com/Alice")
        self.assertEqual(parsed2["auto_discoveries"], doc["auto_discoveries"])
        normalize_config(parsed2)

        # 3. Cross-owner transfer of a discovered_by entry drops discovered_by.
        cross_owner_relocation = {
            "source_origin": "https://api.github.com",
            "source_id": 21,
            "original_owner": "Alice",
            "original_name": "beta",
            "owner": "Bob",
            "name": "beta",
            "destination": {"profile": "main", "owner": "dest", "name": "mirror-Alice-beta", "id": 22},
        }
        entry3 = plan_migration(
            cross_owner_relocation,
            {"document": copy.deepcopy(parsed2), "path": "config.json", "base_sha": blob_sha(rewritten2)},
            secret=secret,
        )
        rewritten3 = rewrite_config(rewritten2, entry3)
        parsed3 = json.loads(rewritten3)
        self.assertNotIn("discovered_by", parsed3["repositories"][2])
        self.assertEqual(parsed3["auto_discoveries"], doc["auto_discoveries"])
        normalize_config(parsed3)

    def test_cli_requires_authoritative_store_for_non_empty_auto_discoveries(self):
        cfg = _base_config(auto_discoveries=[{"url": "https://github.com/BlackBearRevived"}])
        env = {
            "MIRROR_TIMESTAMP_SECRET": "test-secret",
            "GH_TOKEN": "token",
            "DESTINATION_OWNER": "dest",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(cli, "SourceAPI", return_value=Mock()),
                patch.object(cli, "API", return_value=Mock()),
            ):
                with self.assertRaises(ValueError) as ctx:
                    cli.main(["--config", str(path)])
        expected = "auto-discovery onboarding requires authoritative management configuration"
        self.assertEqual(str(ctx.exception), expected)
        self.assertEqual(error_detail(ctx.exception), expected)

    def test_cli_runtime_guard_does_not_fire_for_empty_auto_discoveries(self):
        cfg = _base_config(auto_discoveries=[])
        env = {
            "MIRROR_TIMESTAMP_SECRET": "test-secret",
            "GH_TOKEN": "token",
            "DESTINATION_OWNER": "dest",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(cfg), encoding="utf-8")
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(cli, "SourceAPI", return_value=Mock()),
                patch.object(cli, "API", return_value=Mock()),
            ):
                result = cli.main(["--config", str(path)])
        self.assertEqual(result, [])

    def test_relocated_rule_preserving_destination_name_prefix_round_trips(self):
        doc = {
            "destinations": {"main": {"account_type": "personal"}},
            "auto_discoveries": [
                {
                    "url": "https://github.com/AliceNew",
                    "id": 101,
                    "destination_name_prefix": "mirror-Alice",
                }
            ],
            "repositories": [
                {
                    "source": {"url": "https://github.com/Other/explicit", "id": 1},
                    "mirrors": {"main": {"name": "mirror-Other-explicit", "id": 2}},
                },
                {
                    "source": {"url": "https://github.com/AliceNew/alpha", "id": 11},
                    "mirrors": {"main": {"name": "mirror-Alice-alpha", "id": 12}},
                    "discovered_by": "https://github.com/AliceNew",
                },
            ],
        }
        normalized = normalize_config(doc)
        self.assertEqual(normalized["repositories"][1]["owner"], "AliceNew")
        self.assertEqual(normalized["repositories"][1]["discovered_by"], "https://github.com/AliceNew")

        raw = (json.dumps(doc, indent=2) + "\n").encode("utf-8")
        secret = "fixture-signing-secret-0123456789abcdef"
        explicit_relocation = {
            "source_origin": "https://api.github.com",
            "source_id": 1,
            "original_owner": "Other",
            "original_name": "explicit",
            "owner": "OtherNew",
            "name": "explicit-renamed",
            "destination": {"profile": "main", "owner": "dest", "name": "mirror-Other-explicit", "id": 2},
        }
        entry = plan_migration(
            explicit_relocation,
            {"document": copy.deepcopy(doc), "path": "config.json", "base_sha": blob_sha(raw)},
            secret=secret,
        )
        rewritten = rewrite_config(raw, entry)
        parsed = json.loads(rewritten)
        self.assertEqual(parsed["repositories"][1]["mirrors"]["main"]["name"], "mirror-Alice-alpha")
        self.assertEqual(parsed["repositories"][1]["discovered_by"], "https://github.com/AliceNew")
        normalize_config(parsed)


if __name__ == "__main__":
    unittest.main()
