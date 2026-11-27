import io
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request

from mirror_test_support import patch_everywhere

import mirror


class SourceAuthTests(unittest.TestCase):
    def setUp(self):
        secret = patch.dict("os.environ", {"MIRROR_TIMESTAMP_SECRET": "synthetic-test-only-secret"})
        secret.start()
        self.addCleanup(secret.stop)

    def test_github_source_api_authenticates_only_exact_api_origin(self):
        api = mirror.SourceAPI("secret")
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"[]"
        api.opener.open = Mock(return_value=response)
        for url, expected in [
            ("https://api.github.com/repos/a/b/releases/1/assets", "Bearer secret"),
            ("https://api.github.com:443/repos/a/b", "Bearer secret"),
            ("https://api.github.com:444/repos/a/b", None),
            ("https://api.github.com.evil.example/repos/a/b", None),
            ("https://uploads.github.com/repos/a/b", None),
        ]:
            api.request("GET", url)
            self.assertEqual(api.opener.open.call_args.args[0].get_header("Authorization"), expected)

    def test_source_download_does_not_use_api_credential(self):
        api = mirror.SourceAPI("secret")
        with patch("mirror.socket.getaddrinfo", side_effect=OSError("stopped")) as lookup:
            with self.assertRaises(OSError):
                api.download("https://api.github.com/asset", io.BytesIO())
            lookup.assert_called_once()

    def test_redirect_never_forwards_credential_to_other_origin(self):
        handler = mirror.SafeRedirect()
        req = Request("https://api.github.com/a", headers={"Authorization": "Bearer secret"})
        redirected = handler.redirect_request(req, None, 302, "", {}, "https://evil.example/a")
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_403_diagnostic_uses_only_validated_rate_header_and_role(self):
        api = mirror.SourceAPI("secret")
        error = HTTPError("https://evil.example/secret", 403, "secret", {"X-RateLimit-Remaining": "0"}, None)
        self.addCleanup(error.close)
        api.opener.open = Mock(side_effect=error)
        from repo_mirror.github_api import _cooldowns
        self.addCleanup(_cooldowns.clear)
        with patch("repo_mirror.github_api._MAX_RATE_WAIT", 0), self.assertRaises(HTTPError) as caught:
            api.request("GET", "https://api.github.com/repos/a/b")
        self.assertEqual(
            mirror.error_detail(caught.exception), "HTTP 403 (source API GET; rate limit remaining=0)"
        )

    def test_malformed_rate_header_is_never_logged(self):
        api = mirror.API("secret")
        error = HTTPError(
            "https://evil.example/secret", 403, "secret", {"X-RateLimit-Remaining": "0\nsecret"}, None
        )
        self.addCleanup(error.close)
        api.opener.open = Mock(side_effect=error)
        with self.assertRaises(HTTPError) as caught:
            api.request("POST", "https://api.github.com/repos/a/b")
        detail = mirror.error_detail(caught.exception)
        self.assertEqual(detail, "HTTP 403 (destination API POST)")

    def test_destination_asset_403_is_identified_without_url(self):
        api = mirror.API("secret")
        error = HTTPError("https://evil.example/secret", 403, "secret", {"X-RateLimit-Remaining": "0"}, None)
        self.addCleanup(error.close)
        api.opener.open = Mock(side_effect=error)
        with self.assertRaises(HTTPError) as caught:
            api.download("https://api.github.com/asset", io.BytesIO())
        self.assertEqual(
            mirror.error_detail(caught.exception), "HTTP 403 (destination asset GET; rate limit remaining=0)"
        )

    def test_main_uses_separate_clients_for_github_and_forgejo(self):
        config_doc = {
            "graphql": False,
            "destinations": {"main": {"account_type": "personal"}},
            "repositories": [
                {"source": {"url": "https://github.com/alice/one"}, "mirrors": {"main": {}}},
                {"source": {"type": "forgejo", "url": "https://api.github.com/bob/two"}, "mirrors": {"main": {}}},
            ],
        }
        gh_client, forge_client, dest = Mock(), Mock(), Mock()
        with tempfile.NamedTemporaryFile(mode="w") as config:
            json.dump(config_doc, config)
            config.flush()
            with (
                patch.dict(os.environ, {"GH_TOKEN": "secret", "DESTINATION_OWNER": "dest"}),
                patch_everywhere("SourceAPI", return_value=gh_client),
                patch_everywhere("API", side_effect=[forge_client, dest]),
                patch_everywhere("get_existing", return_value=None),
                patch_everywhere("mirror_repository") as run,
            ):
                mirror.main(["--config", config.name])
        calls = {call.args[1]: call for call in run.call_args_list}
        self.assertIs(calls["one"].args[3], gh_client)
        self.assertIs(calls["two"].args[3], forge_client)
