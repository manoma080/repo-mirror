"""Tests verifying legacy configuration rejection and preserved named capabilities."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from repo_mirror import adhoc, cli, forgejo_dispatch, forgejo_schedule, forgejo_sync
from repo_mirror.config_named import normalize_config
from repo_mirror.logging_utils import SAFE_ERROR_MESSAGES, error_detail
from scheduler import auto_update

EXPECTED_ERROR = "legacy configuration format is no longer supported"


class LegacyFormatRejectionTests(unittest.TestCase):
    def _legacy_payloads(self):
        return [
            {"sources": []},
            {"destination_account_type": "personal", "sources": []},
            {"destination_account_type": "organization"},
            {
                "destinations": {"main": {"account_type": "personal"}},
                "repositories": [],
                "sources": [],
            },
        ]

    def test_error_message_is_safe_on_both_runners(self):
        self.assertIn(EXPECTED_ERROR, SAFE_ERROR_MESSAGES)
        self.assertIn(EXPECTED_ERROR, forgejo_sync.CONFIG_REASONS)
        self.assertEqual(error_detail(ValueError(EXPECTED_ERROR)), EXPECTED_ERROR)
        self.assertEqual(
            forgejo_sync.safe_reason(ValueError(EXPECTED_ERROR), configuration=True),
            EXPECTED_ERROR,
        )

    def test_github_entrypoints_reject_legacy_configs_with_fixed_error(self):
        for payload in self._legacy_payloads():
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError) as ctx:
                    normalize_config(payload)
                self.assertEqual(str(ctx.exception), EXPECTED_ERROR)

                with self.assertRaises(ValueError) as ctx:
                    adhoc.add_to_config(dict(payload), "https://github.com/a/b")
                self.assertEqual(str(ctx.exception), EXPECTED_ERROR)

                with self.assertRaises(ValueError) as ctx:
                    adhoc.remove_from_config(dict(payload), "https://github.com/a/b")
                self.assertEqual(str(ctx.exception), EXPECTED_ERROR)

                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "config.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    env = {
                        "GH_TOKEN": "token",
                        "DESTINATION_OWNER": "dest",
                        "MIRROR_TIMESTAMP_SECRET": "synthetic-secret",
                    }
                    with patch.dict(os.environ, env, clear=True):
                        with self.assertRaises(ValueError) as ctx:
                            cli.main(["--config", str(path)])
                        self.assertEqual(str(ctx.exception), EXPECTED_ERROR)
                        self.assertEqual(error_detail(ctx.exception), EXPECTED_ERROR)

    def test_forgejo_and_launcher_reject_legacy_configs_with_fixed_error(self):
        for payload in self._legacy_payloads():
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError) as ctx:
                    forgejo_sync.normalize_named(payload)
                self.assertEqual(str(ctx.exception), EXPECTED_ERROR)

                with self.assertRaises(forgejo_dispatch.DispatchError) as ctx:
                    forgejo_dispatch.plan_dispatch(
                        payload, {"source_urls": "https://github.com/a/b"}, owner="dest", port=3300
                    )
                self.assertEqual(str(ctx.exception), EXPECTED_ERROR)

                self.assertFalse(auto_update.valid_manifest(payload))

                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    manifest = root / "manifest.json"
                    manifest.write_text(json.dumps(payload), encoding="utf-8")
                    stderr = io.StringIO()
                    with redirect_stderr(stderr):
                        rc = forgejo_schedule.main(
                            [
                                "run",
                                "--manifest",
                                str(manifest),
                                "--token-file",
                                str(root / "token"),
                                "--timestamp-secret-file",
                                str(root / "secret"),
                                "--journal-dir",
                                str(root),
                                "--owner",
                                "dest",
                                "--lock",
                                str(root / "lock"),
                            ]
                        )
                    self.assertEqual(rc, 2)
                    self.assertIn(EXPECTED_ERROR, stderr.getvalue())


class PreservedCapabilitiesInNamedShapeTests(unittest.TestCase):
    def test_organization_custom_name_ignore_releases_overrides_and_pins(self):
        config = {
            "destinations": {"org": {"account_type": "organization"}},
            "repositories": [
                {
                    "source": {"url": "https://github.com/alice/pinned", "id": 10},
                    "mirrors": {"org": {"name": "custom-pinned", "id": 20, "visibility": "public", "ignore_releases": True}},
                },
                {
                    "source": {"url": "https://github.com/alice/unpinned"},
                    "mirrors": {"org": {}},
                },
            ],
            "auto_discoveries": [
                {
                    "url": "https://github.com/bob",
                    "visibility": "private",
                    "ignore_releases": False,
                    "destination_name_prefix": "custom-bob",
                    "overrides": {
                        "special": {"name": "bob-special", "visibility": "public", "ignore_releases": True}
                    },
                }
            ],
        }
        normalized = normalize_config(config)
        self.assertEqual(normalized["account_type"], "organization")
        self.assertEqual(
            normalized["repositories"][0]["mirror"],
            {"name": "custom-pinned", "source_id": 10, "destination_id": 20, "visibility": "public", "ignore_releases": True},
        )
        self.assertNotIn("mirror", normalized["repositories"][1])
        self.assertEqual(
            normalized["auto_discoveries"][0]["overrides"]["special"],
            {"name": "bob-special", "visibility": "public", "ignore_releases": True},
        )
