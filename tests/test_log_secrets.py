import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch
from urllib.error import HTTPError

from mirror_test_support import patch_everywhere
from test_reconcile_assets import SOURCE, Destination, Source

import mirror
from repo_mirror.git_sync import archive_name


class LogSecretsTests(unittest.TestCase):
    def setUp(self):
        secret = patch.dict("os.environ", {"MIRROR_TIMESTAMP_SECRET": "synthetic-test-only-secret"})
        secret.start()
        self.addCleanup(secret.stop)

    def test_untrusted_identifiers_are_not_logged(self):
        secret = "ghp_" + "A" * 36
        config = {
            "destinations": {"main": {"account_type": "personal"}},
            "repositories": [{"source": {"url": f"https://github.com/{secret}/{secret}"}, "mirrors": {"main": {}}}],
        }
        output = io.StringIO()
        with tempfile.NamedTemporaryFile(mode="w") as f:
            json.dump(config, f)
            f.flush()
            with (
                patch.dict(os.environ, {"GH_TOKEN": "token", "DESTINATION_OWNER": "dest"}),
                patch_everywhere("get_existing", return_value=None),
                patch_everywhere("mirror_repository"),
                redirect_stderr(output),
            ):
                mirror.main(["--config", f.name])
        self.assertNotIn(secret, output.getvalue())
        self.assertIn("source 1/1", output.getvalue())
        self.assertIn("repository 1/1", output.getvalue())

    def test_release_and_asset_names_are_not_logged(self):
        secret = "github_pat_" + "A" * 50

        class SecretSource(Source):
            def pages(self, url):
                values = super().pages(url)
                if url.endswith("/releases"):
                    values[0]["tag_name"] = secret
                else:
                    values[0]["name"] = secret
                return values

        output = io.StringIO()
        with patch_everywhere("mirror_git"), redirect_stderr(output), self.assertRaises(RuntimeError):
            mirror.mirror_repository(SOURCE, "demo", "dest", SecretSource(), Destination(), "token")
        self.assertNotIn(secret, output.getvalue())
        self.assertIn("release 1/1", output.getvalue())
        self.assertIn("asset 1/2", output.getvalue())

    def test_journal_upload_http_failure_reports_safe_status(self):
        class FailedDestination(Destination):
            def upload(self, url, data, content_type):
                raise HTTPError("https://example.test/?token=secret", 403, "secret", {}, io.BytesIO())

        output = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as directory,
            patch_everywhere("mirror_git"),
            redirect_stderr(output),
        ):
            journal = mirror.FailureJournal(os.path.join(directory, "journal"))
            with self.assertRaises(RuntimeError) as raised:
                mirror.mirror_repository(
                    SOURCE, "demo", "dest", Source(), FailedDestination(), "token", journal=journal
                )
        self.assertIn("HTTP 403", mirror.error_detail(raised.exception))
        self.assertNotIn("secret", mirror.error_detail(raised.exception))
        self.assertIn("HTTP 403", output.getvalue())
        self.assertNotIn("secret", output.getvalue())

    def test_safe_label_allows_plain_and_hashed_archive_names(self):
        plain = "mirror-BlackBearRevived-ps5-native-app-boilerplate-wipe-20261126T072618Z"
        hashed_explicit = "mirror-BlackBearRevived-ps5-native-app-boilerplate-0123456789abcdef-wipe-20261126T072618Z"
        hashed_generated = archive_name(
            "mirror-BlackBearRevived-ps5-native-app-boilerplate-extended-edition-for-testing-long",
            "20261126T072618Z",
        )
        self.assertEqual(len(hashed_generated), 100)
        self.assertRegex(hashed_generated, r"-[0-9a-f]{16}-wipe-20261126T072618Z\Z")
        for value in (plain, hashed_explicit, hashed_generated):
            with self.subTest(value=value):
                self.assertEqual(mirror.safe_label(value), value)

    def test_safe_label_redacts_high_entropy_with_non_archive_suffix_or_secret_prefix(self):
        for value in (
            "mX9qR-2pL7v-K4nT8-wZ3bJ-0cY5-snap-987654",
            "mirror-BlackBearRevived-ps5-app-bak-20261126T072618Z",
            "aB7cD-9eF2g-H4iJ6-kL8mN-0pQ3r-S5tU7-vWxYz-wipe-20261126T072618Z",
        ):
            with self.subTest(value=value):
                self.assertEqual(mirror.safe_label(value), "[redacted]")

    def test_safe_label_preserves_existing_redaction_rules(self):
        for value in (
            "ghp_" + "A" * 36,
            "github_pat_" + "A" * 40,
            "aB7cD9eF2gH4iJ6kL8mN0pQ3rS5tU7vW",
            "a" * 32 + "-wipe-20261126T072618Z",
            "mirror-secret-repo-wipe-20261126T072618Z",
        ):
            with self.subTest(value=value):
                self.assertEqual(mirror.safe_label(value), "[redacted]")

        archive_like_secret = "mirror-BlackBearRevived-ps5-native-app-boilerplate-wipe-20261126T072618Z"
        with patch.dict(
            os.environ,
            {
                "GH_TOKEN": "gh-env-secret-value-123456",
                "MIRROR_TIMESTAMP_SECRET": archive_like_secret,
            },
        ):
            self.assertEqual(mirror.safe_label("gh-env-secret-value-123456"), "[redacted]")
            self.assertEqual(mirror.safe_label(archive_like_secret), "[redacted]")

    def test_safe_label_redacts_archive_suffix_only(self):
        for value in (
            "-wipe-20261126T072618Z",
            "-0123456789abcdef-wipe-20261126T072618Z",
        ):
            with self.subTest(value=value):
                self.assertEqual(mirror.safe_label(value), "[redacted]")
