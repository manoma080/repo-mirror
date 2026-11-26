import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class Response:
    def __init__(self, body=b"", status=200, headers=None):
        import io
        self.stream = io.BytesIO(body)
        self.status = status
        self.headers = headers or {}
    def getheader(self, key, default=None):
        return self.headers.get(key, default)
    def read(self, size):
        return self.stream.read(size)
    def close(self):
        self.stream.close()


class GitLabAssetsTests(unittest.TestCase):
    def setattr(self, target, name, value):
        patcher = patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ipv6_transition_addresses_rejected_for_literals_and_dns(self):
        import socket
        from repo_mirror import gitlab_assets as a
        # Reject entire transition prefixes, even when the embedded IPv4 is public.
        addresses = (
            '64:ff9b::a00:1', '64:ff9b::808:808', '64:ff9b:1::a00:1',
            '::ffff:10.0.0.1', '::ffff:8.8.8.8', '::10.0.0.1',
            '2002:0a00:0001::', '2002:0808:0808::',
            '2001:0000:0808:0808::', '2001:0000:0808:0808::f5ff:fffe',
            '100::a00:1', '2001:20::1',
        )
        with patch.object(socket, 'socket', side_effect=AssertionError('unsafe connection')):
            for address in addresses:
                with self.subTest(address=address, source='literal'):
                    with self.assertRaises(a.UnsafeAsset):
                        a.validate_url(f'https://[{address}]/file')
                with self.subTest(address=address, source='dns'):
                    answer = (socket.AF_INET6, socket.SOCK_STREAM, 6, '', (address, 443, 0, 0))
                    with patch.object(socket, 'getaddrinfo', return_value=[answer]):
                        with self.assertRaises(a.UnsafeAsset):
                            a._open('https://example.com/file', 5)

    def test_ordinary_global_ipv6_allowed_for_literals_and_dns(self):
        import socket
        from repo_mirror import gitlab_assets as a
        address = '2606:4700:4700::1111'
        a.validate_url(f'https://[{address}]/file')
        calls = []
        class Sock:
            def settimeout(self, value): pass
            def connect(self, target): calls.append(target)
            def close(self): pass
        class Conn:
            def __init__(self, *args, **kwargs): pass
            def request(self, *args, **kwargs): pass
            def getresponse(self): return Response()
            def close(self): pass
        answer = (socket.AF_INET6, socket.SOCK_STREAM, 6, '', (address, 80, 0, 0))
        with patch.object(socket, 'getaddrinfo', return_value=[answer]), \
             patch.object(socket, 'socket', return_value=Sock()), \
             patch.object(a.http.client, 'HTTPConnection', Conn):
            response, close = a._open('http://example.com/file', 5)
            close()
        self.assertEqual(calls, [(address, 80, 0, 0)])

    def test_http_and_ftp_urls_are_supported(self):
        from repo_mirror.gitlab_assets import validate_url
        for scheme in ('http', 'ftp'):
            with self.subTest(scheme=scheme):
                validate_url(f'{scheme}://example.com/file')

    def test_http_socket_is_pinned_without_tls(self):
        import socket
        from repo_mirror import gitlab_assets as a
        calls = []
        self.setattr(socket, 'getaddrinfo', lambda *args, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 80))])
        class Sock:
            def settimeout(self, value): pass
            def connect(self, address): calls.append(address)
            def close(self): pass
        class Conn:
            def __init__(self, *args, **kwargs): pass
            def request(self, *args, **kwargs): calls.append((args, kwargs))
            def getresponse(self): return Response(b'data')
            def close(self): pass
        self.setattr(socket, 'socket', lambda *args: Sock())
        self.setattr(a.http.client, 'HTTPConnection', Conn)
        with a.download_asset('http://example.com/file', 4) as result:
            self.assertEqual(result.file.read(), b'data')
        self.assertEqual(calls[0], ('8.8.8.8', 80))
        self.assertEqual(calls[1][0], ('GET', '/file'))

    def test_ftp_pins_control_and_epsv_data_connections(self):
        import socket
        from repo_mirror import gitlab_assets as a
        calls = []
        self.setattr(socket, 'getaddrinfo', lambda *args, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 21))])
        class Sock:
            def __init__(self): self.data = b'data'
            def settimeout(self, value): pass
            def makefile(self, *args, **kwargs): return None
            def connect(self, address): calls.append(address)
            def recv(self, size):
                chunk, self.data = self.data[:size], self.data[size:]
                return chunk
            def close(self): pass
        class FTP:
            encoding = 'utf-8'
            def __init__(self): pass
            def getresp(self): return '220 ready'
            def login(self): calls.append('anonymous')
            def voidcmd(self, command): calls.append(command)
            def sendcmd(self, command):
                calls.append(command)
                if command == 'EPSV': return '229 Entering Extended Passive Mode (|||40000|)'
                return '150 opening'
            def voidresp(self): calls.append('complete')
            def close(self): pass
        self.setattr(socket, 'socket', lambda *args: Sock())
        self.setattr(a.ftplib, 'FTP', FTP)
        with a.download_asset('ftp://example.com/file', 4) as result:
            self.assertEqual(result.file.read(), b'data')
        self.assertEqual(calls, [('8.8.8.8', 21), 'anonymous', 'TYPE I', 'EPSV', ('8.8.8.8', 40000), 'RETR /file', 'complete'])
        calls.clear()
        link = {'url': 'ftp://example.com/file', 'name': 'archive', 'link_type': 'package'}
        with a.download_asset(link['url'], 3) as result:
            self.assertTrue(result.oversize)
            self.assertIsNone(result.file)
        self.assertEqual(a.render_preserved_link(link), '- [archive](ftp://example.com/file) (package)')
        self.assertNotIn('complete', calls)

    def test_ftp_rejects_untrusted_passive_endpoint(self):
        import socket
        from repo_mirror import gitlab_assets as a
        self.setattr(socket, 'getaddrinfo', lambda *args, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 21))])
        calls = []
        class Sock:
            def settimeout(self, value): pass
            def connect(self, address): calls.append(address)
            def makefile(self, *args, **kwargs): return None
            def close(self): pass
        class FTP:
            encoding = 'utf-8'
            def getresp(self): pass
            def login(self): pass
            def voidcmd(self, cmd): pass
            def sendcmd(self, cmd): return '227 Entering Passive Mode (127,0,0,1,8,1)'
            def close(self): pass
        self.setattr(socket, 'socket', lambda *args: Sock())
        self.setattr(a.ftplib, 'FTP', FTP)
        with self.assertRaises(a.UnsafeAsset):
            with a.download_asset('ftp://example.com/file', 4): pass
        self.assertEqual(calls, [('8.8.8.8', 21)])

    def test_https_redirect_cannot_downgrade_or_leak_signed_query(self):
        from repo_mirror import gitlab_assets as a
        calls = []
        def opening(url, timeout):
            calls.append(url)
            return Response(status=302, headers={'Location': 'http://example.com/file?secret=token'}), lambda: None
        self.setattr(a, '_open', opening)
        with self.assertRaises(a.UnsafeAsset):
            with a.download_asset('https://example.com/file', 4): pass
        self.assertEqual(len(calls), 1)

    def test_signed_redirect_downloads_without_exposing_secret(self):
        from repo_mirror import gitlab_assets as a
        calls = []
        signed = "https://cdn.example.com/file?X-Amz-Signature=secret%2Fvalue"
        def opening(url, timeout):
            calls.append(url)
            if len(calls) == 1:
                return Response(status=302, headers={"Location": signed}), lambda: None
            return Response(b"data"), lambda: None
        self.setattr(a, "_open", opening)
        with a.download_asset("https://example.com/file", 10) as result:
            self.assertEqual(result.file.read(), b"data")
        self.assertEqual(calls, ["https://example.com/file", signed])
        self.assertNotIn("secret", a.render_preserved_link({"name": "file", "url": calls[0], "link_type": "other"}))

    def test_default_deadline_allows_large_downloads(self):
        from repo_mirror import gitlab_assets as a
        budgets = []
        def opening(url, timeout):
            budgets.append(timeout)
            return Response(b"data"), lambda: None
        self.setattr(a, "_open", opening)
        with a.download_asset("https://example.com/file", 10):
            pass
        self.assertGreater(budgets[0], 1800)

    def test_environment_configures_deadline(self):
        from repo_mirror import gitlab_assets as a
        budgets = []
        self.setattr(a, "_open", lambda url, timeout: (budgets.append(timeout) or Response(), lambda: None))
        with patch.dict("os.environ", {"REPO_MIRROR_ASSET_TIMEOUT_SECONDS": "90"}):
            with a.download_asset("https://example.com/file", 10):
                pass
        self.assertGreater(budgets[0], 80)
        self.assertLessEqual(budgets[0], 90)

    def test_original_query_is_neither_downloaded_nor_rendered(self):
        from repo_mirror import gitlab_assets as a
        self.setattr(a, "_open", lambda *args: self.fail("unsafe original fetched"))
        url = "https://example.com/file?token=secret"
        with self.assertRaises(a.UnsafeAsset) as error:
            with a.download_asset(url, 10):
                pass
        self.assertNotIn("secret", str(error.exception))
        with self.assertRaises(a.UnsafeAsset):
            a.render_preserved_link({"name": "file", "url": url, "link_type": "other"})

    def test_redirect_query_failure_does_not_leak(self):
        from repo_mirror import gitlab_assets as a
        def opening(url, timeout):
            if "?" in url:
                raise ValueError("failed " + url)
            return Response(status=302, headers={"Location": "https://cdn.example.com/?signature=secret"}), lambda: None
        self.setattr(a, "_open", opening)
        with self.assertRaises(a.AssetError) as error:
            with a.download_asset("https://example.com/file", 10):
                pass
        self.assertNotIn("secret", str(error.exception))

    def test_signed_redirect_to_private_target_is_rejected(self):
        from repo_mirror import gitlab_assets as a
        calls = []
        def opening(url, timeout):
            calls.append(url)
            return Response(status=302, headers={"Location": "https://127.0.0.1/file?signature=secret"}), lambda: None
        self.setattr(a, "_open", opening)
        with self.assertRaises(a.UnsafeAsset):
            with a.download_asset("https://example.com/file", 10):
                pass
        self.assertEqual(len(calls), 1)

    def test_render_preserved_link_is_stable_and_escapes_markdown(self):
        from repo_mirror.gitlab_assets import render_preserved_link
        link = {"name": "a[b]", "url": "https://example.com/file", "link_type": "other"}
        assert render_preserved_link(link) == '- [a\\[b\\]](https://example.com/file) (other)'




    def test_unknown_size_streams_to_bounded_temporary_file(self):
        monkeypatch = self
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        tmp_path = Path(tempdir.name)
        from repo_mirror import gitlab_assets as a
        monkeypatch.setattr(a, "_open", lambda url, timeout: (Response(b"1234"), lambda: None))
        with a.download_asset("https://example.com/file", 4, directory=tmp_path) as result:
            assert not result.oversize
            assert result.file.read() == b"1234"
        assert list(tmp_path.iterdir()) == []


    def test_unknown_size_oversize_never_writes_over_limit(self):
        monkeypatch = self
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        tmp_path = Path(tempdir.name)
        from repo_mirror import gitlab_assets as a
        monkeypatch.setattr(a, "_open", lambda url, timeout: (Response(b"12345"), lambda: None))
        with a.download_asset("https://example.com/file", 4, directory=tmp_path) as result:
            assert result.oversize
            assert result.file is None
            assert all(p.stat().st_size <= 4 for p in tmp_path.iterdir())
        assert list(tmp_path.iterdir()) == []


    def test_redirect_revalidates_before_connect(self):
        monkeypatch = self
        from repo_mirror import gitlab_assets as a
        calls = []
        def open_url(url, timeout):
            calls.append(url)
            return Response(status=302, headers={"Location": "https://127.0.0.1/x"}), lambda: None
        monkeypatch.setattr(a, "_open", open_url)
        with self.assertRaises(a.UnsafeAsset):
            with a.download_asset("https://example.com/file", 4):
                pass
        assert len(calls) == 1


    def test_render_rejects_secret_url_and_markdown_injection(self):
        from repo_mirror.gitlab_assets import render_preserved_link, UnsafeAsset
        with self.assertRaises(UnsafeAsset):
            render_preserved_link({"name": "asset", "url": "https://example.com/?token=abc", "link_type": "other"})
        with self.assertRaises(UnsafeAsset):
            render_preserved_link({"name": "asset\nmalicious", "url": "https://example.com/a", "link_type": "other"})


    def test_nonpublic_dns_answer_is_rejected_before_socket_connect(self):
        monkeypatch = self
        import socket
        from repo_mirror import gitlab_assets as a
        monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ])
        monkeypatch.setattr(socket, "socket", lambda *args: self.fail("should not connect"))
        with self.assertRaises(a.UnsafeAsset):
            a._open("https://example.com/file", 5)


    def test_caller_exception_is_not_rewritten(self):
        monkeypatch = self
        from repo_mirror import gitlab_assets as a
        monkeypatch.setattr(a, "_open", lambda url, timeout: (Response(b"x"), lambda: None))
        with self.assertRaisesRegex(RuntimeError, "upload failed"):
            with a.download_asset("https://example.com/file", 4):
                raise RuntimeError("upload failed")


    def test_doubly_encoded_generated_archive_is_rejected(self):
        from repo_mirror.gitlab_assets import validate_url, UnsafeAsset
        with self.assertRaises(UnsafeAsset):
            validate_url("https://example.com/p/%252d/archive/main.zip")


    def test_lying_oversize_length_is_not_verified_oversize(self):
        from repo_mirror import gitlab_assets as a
        self.setattr(a, "_open", lambda url, timeout: (Response(b"bad", headers={"Content-Length": "100"}), lambda: None))
        with self.assertRaisesRegex(a.AssetError, "incomplete response"):
            with a.download_asset("https://example.com/f", 5):
                self.fail("unverified oversize")

    def test_advertised_oversize_requires_cap_plus_one_bytes(self):
        from repo_mirror import gitlab_assets as a
        self.setattr(a, "_open", lambda url, timeout: (Response(b"123456", headers={"Content-Length": "100"}), lambda: None))
        with a.download_asset("https://example.com/f", 5) as result:
            self.assertTrue(result.oversize)


    def test_body_exceeding_advertised_length_is_rejected(self):
        from repo_mirror import gitlab_assets as a
        self.setattr(a, "_open", lambda url, timeout: (Response(b"123456", headers={"Content-Length": "2"}), lambda: None))
        with self.assertRaises(a.AssetError):
            with a.download_asset("https://example.com/f", 5):
                self.fail("mismatched length accepted")

    def test_total_deadline_includes_redirects(self):
        import time
        from repo_mirror import gitlab_assets as a
        calls = []
        def opening(url, timeout):
            calls.append(timeout)
            time.sleep(0.04)
            return Response(status=302, headers={"Location": "/next"}), lambda: None
        self.setattr(a, "_open", opening)
        with self.assertRaises(a.AssetError):
            with a.download_asset("https://example.com/f", 5, timeout=0.07, max_redirects=5):
                self.fail("deadline ignored")
        self.assertLess(len(calls), 5)

    def test_total_deadline_includes_slow_reads(self):
        import time
        from repo_mirror import gitlab_assets as a
        class Slow(Response):
            def read(self, size):
                time.sleep(0.04)
                return super().read(min(size, 1))
        self.setattr(a, "_open", lambda url, timeout: (Slow(b"12345"), lambda: None))
        with self.assertRaises(a.AssetError):
            with a.download_asset("https://example.com/f", 5, timeout=0.07):
                self.fail("deadline ignored")

    def test_slow_open_is_bounded_and_late_response_closed(self):
        import threading
        import time
        from repo_mirror import gitlab_assets as a
        closed = threading.Event()
        def opening(url, timeout):
            time.sleep(0.12)
            return Response(), closed.set
        self.setattr(a, "_open", opening)
        started = time.monotonic()
        with self.assertRaises(a.AssetError):
            with a.download_asset("https://example.com/f", 5, timeout=0.04):
                self.fail("deadline ignored")
        self.assertLess(time.monotonic() - started, 0.11)
        self.assertTrue(closed.wait(1))

    def test_exact_limit_with_advertised_length(self):
        from repo_mirror import gitlab_assets as a
        self.setattr(a, "_open", lambda url, timeout: (Response(b"1234", headers={"Content-Length": "4"}), lambda: None))
        with a.download_asset("https://example.com/f", 4) as result:
            self.assertEqual(result.size, 4)
            self.assertEqual(result.file.read(), b"1234")

    def test_short_response_is_deterministic_failure(self):
        monkeypatch = self
        from repo_mirror import gitlab_assets as a
        monkeypatch.setattr(a, "_open", lambda url, timeout: (Response(b"abc", headers={"Content-Length": "4"}), lambda: None))
        with self.assertRaisesRegex(a.AssetError, "incomplete response"):
            with a.download_asset("https://example.com/f", 5):
                pass


    def test_socket_connects_to_validated_dns_address_not_hostname(self):
        monkeypatch = self
        import socket
        from repo_mirror import gitlab_assets as a
        calls = []
        monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
        class Sock:
            def settimeout(self, timeout): pass
            def connect(self, address): calls.append(address)
            def do_handshake(self): calls.append("handshake")
            def close(self): pass
        class Context:
            def wrap_socket(self, sock, server_hostname, do_handshake_on_connect):
                assert server_hostname == "example.com"
                assert not do_handshake_on_connect
                return sock
        requests = []
        class Connection:
            def __init__(self, *args, **kwargs): pass
            def request(self, *args, **kwargs): requests.append((args, kwargs))
            def getresponse(self): return Response()
            def close(self): pass
        monkeypatch.setattr(socket, "socket", lambda *args: Sock())
        monkeypatch.setattr(a.ssl, "create_default_context", lambda: Context())
        monkeypatch.setattr(a.http.client, "HTTPSConnection", Connection)
        response, close = a._open("https://example.com/f?signature=secret%2Fvalue", 5)
        close()
        assert calls == [("8.8.8.8", 443), "handshake"]
        self.assertEqual(requests[0][0], ("GET", "/f?signature=secret%2Fvalue"))
        self.assertEqual(requests[0][1]["headers"], {"Accept-Encoding": "identity", "User-Agent": "repo-mirror"})


    def test_archive_path_segments_rejected_without_trailing_slash(self):
        from repo_mirror.gitlab_assets import validate_url, UnsafeAsset
        for path in ("/p/-/archive", "/p/%2d/archive", "/p/repository/archive"):
            with self.subTest(path=path), self.assertRaises(UnsafeAsset):
                validate_url("https://example.com" + path)
        validate_url("https://example.com/p/-/archived")
        validate_url("https://example.com/p/repository/archive-notes")

    def test_reject_unsafe_or_generated_urls(self):
        from repo_mirror.gitlab_assets import validate_url, UnsafeAsset
        urls = [
            "https://user:pass@example.com/file",
            "https://example.com/file?token=secret", "https://example.com/#secret",
            "https://example.com:8443/file", "https://127.0.0.1/file",
            "https://example.com/a\\b", "https://example.com/%0afile",
            "https://example.com/group/project/-/archive/main/file.zip",
        ]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(UnsafeAsset):
                validate_url(url)


class GitLabAssetTokenTests(unittest.TestCase):
    def setUp(self):
        from repo_mirror import gitlab_assets as module
        self.module = module

    def test_same_origin_token_used_and_dropped_on_redirect(self):
        calls = []

        def opening(url, timeout, headers=None):
            calls.append((url, dict(headers or {})))
            if len(calls) == 1:
                return Response(status=302, headers={"Location": "https://cdn.example.com/file"}), lambda: None
            return Response(b"data"), lambda: None

        original = self.module._open
        self.module._open = opening
        try:
            with self.module.download_asset("https://gitlab.com/uploads/file", 10,
                                            headers={"PRIVATE-TOKEN": "glpat-x"}) as result:
                self.assertEqual(result.file.read(), b"data")
        finally:
            self.module._open = original
        self.assertEqual(calls[0][1].get("PRIVATE-TOKEN"), "glpat-x")
        self.assertNotIn("PRIVATE-TOKEN", calls[1][1])

    def test_gitlab_link_headers_restricted_to_exact_gitlab_com_origin(self):
        import os
        from unittest.mock import patch
        from repo_mirror.repository import gitlab_link_headers

        gitlab_com = {"type": "gitlab", "base_url": "https://gitlab.com"}
        self_hosted = {"type": "gitlab", "base_url": "https://gitlab.example"}
        with patch.dict(os.environ, {"MIRROR_GITLAB_TOKEN": "glpat-secret"}, clear=False):
            self.assertEqual(
                gitlab_link_headers(gitlab_com, "https://gitlab.com/g/p/uploads/file.tar.gz"),
                {"PRIVATE-TOKEN": "glpat-secret"},
            )
            self.assertEqual(
                gitlab_link_headers(gitlab_com, "https://GITLAB.COM/g/p/uploads/file.tar.gz"),
                {"PRIVATE-TOKEN": "glpat-secret"},
            )
            self.assertEqual(
                gitlab_link_headers(gitlab_com, "https://gitlab.com:443/g/p/uploads/file.tar.gz"),
                {"PRIVATE-TOKEN": "glpat-secret"},
            )
            for rejected_url in (
                "http://gitlab.com/g/p/uploads/file.tar.gz",
                "https://gitlab.com:0/g/p/uploads/file.tar.gz",
                "https://gitlab.com:8443/g/p/uploads/file.tar.gz",
                "https://gitlab.com:notaport/g/p/uploads/file.tar.gz",
                "https://gitlab.com:99999/g/p/uploads/file.tar.gz",
                "https://user:pass@gitlab.com/g/p/uploads/file.tar.gz",
                "https://@gitlab.com/g/p/uploads/file.tar.gz",
                "https://cdn.gitlab.com/g/p/uploads/file.tar.gz",
                "https://gitlab.example/g/p/uploads/file.tar.gz",
                None,
                123,
            ):
                with self.subTest(url=rejected_url):
                    self.assertIsNone(gitlab_link_headers(gitlab_com, rejected_url))
            for rejected_base_url in (
                "https://gitlab.com:0",
                "https://gitlab.com:notaport",
                "https://gitlab.com:99999",
                "https://user:pass@gitlab.com",
                "https://@gitlab.com",
                None,
                123,
            ):
                with self.subTest(base_url=rejected_base_url):
                    self.assertIsNone(
                        gitlab_link_headers(
                            {"type": "gitlab", "base_url": rejected_base_url},
                            "https://gitlab.com/g/p/uploads/file.tar.gz",
                        )
                    )
            for non_dict_source in (None, "https://gitlab.com", ["gitlab"]):
                with self.subTest(source=non_dict_source):
                    self.assertIsNone(
                        gitlab_link_headers(non_dict_source, "https://gitlab.com/g/p/uploads/file.tar.gz")
                    )
            self.assertIsNone(
                gitlab_link_headers(self_hosted, "https://gitlab.example/g/p/uploads/file.tar.gz")
            )
            self.assertIsNone(
                gitlab_link_headers(self_hosted, "https://gitlab.com/g/p/uploads/file.tar.gz")
            )

    def test_download_asset_token_scope_for_gitlab_com_and_self_hosted(self):
        import os
        from unittest.mock import patch
        from repo_mirror.repository import gitlab_link_headers

        gitlab_com = {"type": "gitlab", "base_url": "https://gitlab.com"}
        self_hosted = {"type": "gitlab", "base_url": "https://gitlab.example"}
        calls = []

        def opening(url, timeout, headers=None):
            calls.append((url, dict(headers or {})))
            if url == "https://gitlab.com/uploads/file":
                return Response(status=302, headers={"Location": "https://cdn.gitlab.com/file"}), lambda: None
            return Response(b"data"), lambda: None

        original = self.module._open
        self.module._open = opening
        try:
            with patch.dict(os.environ, {"MIRROR_GITLAB_TOKEN": "glpat-secret"}, clear=False):
                com_headers = gitlab_link_headers(gitlab_com, "https://gitlab.com/uploads/file")
                with self.module.download_asset(
                    "https://gitlab.com/uploads/file", 10, headers=com_headers
                ) as result:
                    self.assertEqual(result.file.read(), b"data")

                sh_headers = gitlab_link_headers(self_hosted, "https://gitlab.example/uploads/file")
                with self.module.download_asset(
                    "https://gitlab.example/uploads/file", 10, headers=sh_headers
                ) as result:
                    self.assertEqual(result.file.read(), b"data")
        finally:
            self.module._open = original

        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], ("https://gitlab.com/uploads/file", {"PRIVATE-TOKEN": "glpat-secret"}))
        self.assertEqual(calls[1], ("https://cdn.gitlab.com/file", {}))
        self.assertEqual(calls[2], ("https://gitlab.example/uploads/file", {}))


class GitLabDownloadThrottleTests(unittest.TestCase):
    def test_short_rate_limit_is_retried(self):
        from repo_mirror import gitlab_assets as a
        calls = []

        def opening(url, timeout):
            calls.append(url)
            if len(calls) == 1:
                return Response(status=429, headers={"Retry-After": "0"}), lambda: None
            return Response(b"data"), lambda: None

        original = a._open
        a._open = opening
        try:
            with a.download_asset("https://example.com/file", 10) as result:
                self.assertEqual(result.file.read(), b"data")
        finally:
            a._open = original
        self.assertEqual(len(calls), 2)

    def test_long_rate_limit_fails_visibly(self):
        from repo_mirror import gitlab_assets as a

        def opening(url, timeout):
            return Response(status=429, headers={"Retry-After": "900"}), lambda: None

        original = a._open
        a._open = opening
        try:
            with self.assertRaisesRegex(a.AssetError, "rate limited"):
                with a.download_asset("https://example.com/file", 10):
                    pass
        finally:
            a._open = original
