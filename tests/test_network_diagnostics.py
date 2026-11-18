"""Public diagnostics for transport (URLError) failures.

A source host that is unreachable previously surfaced only the class name
``URLError``, which is indistinguishable from any other repository failure.
"""

import socket
import ssl
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from repo_mirror.github_api import API
from repo_mirror.logging_utils import error_detail


class NetworkDiagnosticTests(unittest.TestCase):
    def test_timeout_reports_fixed_reason(self):
        self.assertEqual(error_detail(URLError(TimeoutError("timed out"))), "URLError (timeout)")
        self.assertEqual(error_detail(URLError(socket.timeout("timed out"))), "URLError (timeout)")

    def test_reason_labels(self):
        for reason, label in (
            (socket.gaierror(-2, "Name or service not known"), "dns failure"),
            (ConnectionRefusedError(111, "Connection refused"), "connection refused"),
            (ConnectionResetError(104, "Connection reset by peer"), "connection reset"),
            (ssl.SSLError(1, "wrong version number"), "tls failure"),
            (OSError(101, "Network is unreachable"), "connection failure"),
            ("offline", "transport failure"),
        ):
            with self.subTest(reason=reason):
                self.assertEqual(error_detail(URLError(reason)), f"URLError ({label})")

    def test_operation_label_is_appended_when_annotated(self):
        exc = URLError(TimeoutError("timed out"))
        setattr(exc, "mirror_operation", ("source", "API", "GET"))
        self.assertEqual(error_detail(exc), "URLError (timeout) (source API GET)")

    def test_unexpected_operation_is_not_printed(self):
        exc = URLError(TimeoutError("timed out"))
        setattr(exc, "mirror_operation", ("source", "API", "https://secret.example/token"))
        self.assertEqual(error_detail(exc), "URLError (timeout)")

    def test_raw_reason_text_never_leaks(self):
        exc = URLError(OSError("connect to api.example.invalid:443 failed for token s3cret"))
        detail = error_detail(exc)
        for fragment in ("example", "443", "s3cret", "token"):
            self.assertNotIn(fragment, detail)


class ApiTransportAnnotationTests(unittest.TestCase):
    @patch("time.sleep")
    def test_escaping_transport_error_keeps_operation(self, sleep):
        api = API(None)
        api.trusted_api_origin = ("https", "theghostonline.dev", 443)
        api.opener.open = Mock(side_effect=URLError(TimeoutError("timed out")))
        with self.assertRaises(URLError) as caught:
            api.request("GET", "https://theghostonline.dev/api/v1/repos/o/r")
        exc = caught.exception
        self.assertEqual(getattr(exc, "mirror_operation", None), ("source", "API", "GET"))
        self.assertEqual(error_detail(exc), "URLError (timeout) (source API GET)")
        self.assertEqual(api.opener.open.call_count, 4)


if __name__ == "__main__":
    unittest.main()
