import hashlib
import hmac
import json
import unittest

from repo_mirror.manifest_migration import migrate_release


class API:
    def __init__(self, assets, content=b''):
        self.assets = assets
        self.content = content
        self.writes = []

    def pages(self, url):
        return [dict(a) for a in self.assets]

    def request(self, method, url, data=None):
        if method == 'PATCH':
            self.writes.append((url, data))
            self.assets[0]['name'] = data['name']
        return {}

    def download(self, url, f):
        f.write(self.content)


class MigrationTests(unittest.TestCase):
    def test_oversize_metadata_refused_before_download(self):
        class NoDownload(API):
            def download(self, url, f):
                self.fail_download = True
        dst = NoDownload([{'id': 42, 'name': 'TIMESTAMPS.txt', 'state': 'uploaded', 'url': 'asset', 'size': 1048577}])
        with self.assertRaisesRegex(ValueError, 'large'):
            migrate_release(API([]), dst, 'source-assets', 'destination', 5, 'source', 'v1', 'secret')
        self.assertFalse(hasattr(dst, 'fail_download'))

    def test_download_is_bounded_during_write(self):
        class StreamingAPI(API):
            def download(self, url, f):
                f.write(b'x' * 1048576)
                f.write(b'x')
                self.exceeded = True
        dst = StreamingAPI([{'id': 42, 'name': 'TIMESTAMPS.txt', 'state': 'uploaded', 'url': 'asset'}])
        with self.assertRaisesRegex(ValueError, 'large'):
            migrate_release(API([]), dst, 'source-assets', 'destination', 5, 'source', 'v1', 'secret')
        self.assertFalse(dst.writes)
        self.assertFalse(hasattr(dst, 'exceeded'))

    def test_signed_pending_manifest_is_rejected(self):
        payload = {'owner': 'repo-mirror:timestamps:v1', 'source': 'source', 'tag': 'v1', 'assets': {}, 'pending': True}
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()
        payload['signature'] = hmac.new(b'secret', raw, hashlib.sha256).hexdigest()
        dst = API([{'id': 42, 'name': 'REPO-MIRROR-MANIFEST.json', 'state': 'uploaded', 'url': 'asset'}], json.dumps(payload).encode())
        with self.assertRaisesRegex(ValueError, 'pending'):
            migrate_release(API([]), dst, 'source-assets', 'destination', 5, 'source', 'v1', 'secret')

    def test_final_asset_content_change_is_rejected(self):
        payload = {'owner': 'repo-mirror:timestamps:v1', 'source': 'source', 'tag': 'v1', 'assets': {}}
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()
        payload['signature'] = hmac.new(b'secret', raw, hashlib.sha256).hexdigest()
        class ChangedAPI(API):
            def request(self, method, url, data=None):
                super().request(method, url, data)
                self.content = b'changed'
                return {}
        dst = ChangedAPI([{'id': 42, 'name': 'TIMESTAMPS.txt', 'state': 'uploaded', 'url': 'asset'}], json.dumps(payload).encode())
        with self.assertRaises(ValueError):
            migrate_release(API([]), dst, 'source-assets', 'destination', 5, 'source', 'v1', 'secret')

    def test_renames_signed_asset_preserving_id(self):
        payload = {'owner': 'repo-mirror:timestamps:v1', 'source': 'source', 'tag': 'v1', 'assets': {}}
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()
        payload['signature'] = hmac.new(b'secret', encoded, hashlib.sha256).hexdigest()
        dst = API([{'id': 42, 'name': 'TIMESTAMPS.txt', 'state': 'uploaded', 'url': 'asset'}], json.dumps(payload).encode())
        src = API([])
        result = migrate_release(src, dst, 'source-assets', 'destination', 5, 'source', 'v1', 'secret')
        self.assertEqual(result, 'renamed')
        self.assertEqual(dst.writes, [('destination/releases/assets/42', {'name': 'REPO-MIRROR-MANIFEST.json'})])

    def test_rename_rejects_duplicate_new_name_after_patch(self):
        payload = {'owner': 'repo-mirror:timestamps:v1', 'source': 'source', 'tag': 'v1', 'assets': {}}
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()
        payload['signature'] = hmac.new(b'secret', encoded, hashlib.sha256).hexdigest()
        class RacingAPI(API):
            def request(self, method, url, data=None):
                super().request(method, url, data)
                self.assets.append({'id': 43, 'name': 'REPO-MIRROR-MANIFEST.json', 'state': 'uploaded', 'url': 'other'})
        dst = RacingAPI([{'id': 42, 'name': 'TIMESTAMPS.txt', 'state': 'uploaded', 'url': 'asset'}], json.dumps(payload).encode())
        with self.assertRaises(ValueError):
            migrate_release(API([]), dst, 'source-assets', 'destination', 5, 'source', 'v1', 'secret')

    def test_source_reserved_name_blocks_write(self):
        src = API([{'name': 'TIMESTAMPS.txt'}])
        dst = API([{'id': 42, 'name': 'TIMESTAMPS.txt', 'state': 'uploaded', 'url': 'asset'}])
        with self.assertRaises(ValueError):
            migrate_release(src, dst, 'source-assets', 'destination', 5, 'source', 'v1', 'secret')
        self.assertEqual(dst.writes, [])


class ScanTests(unittest.TestCase):
    def test_scans_old_releases_and_reports_destination_only(self):
        from repo_mirror.manifest_migration import scan_repository
        source = {"type": "github", "base_url": "https://github.com", "owner": "alice", "repos": ["proj"]}
        from repo_mirror.sources import source_marker
        marker = source_marker(source, "proj")
        payload = {"owner": "repo-mirror:timestamps:v1", "source": marker, "tag": "old", "assets": {}}
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
        payload["signature"] = hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
        class Routes(API):
            def __init__(self, routes, content=b""):
                super().__init__([], content); self.routes = routes
            def pages(self, url):
                return [dict(x) for x in self.routes[url]]
            def request(self, method, url, data=None):
                if method == "GET": return {"description": marker}
                self.writes.append((url, data))
                self.routes["target/releases/2/assets"][0]["name"] = data["name"]
        src = Routes({"https://api.github.com/repos/alice/proj/releases": [{"id": 1, "tag_name": "new", "draft": False}, {"id": 2, "tag_name": "old", "draft": False}], "https://api.github.com/repos/alice/proj/releases/2/assets": []})
        dst = Routes({"target/releases": [{"id": 2, "tag_name": "old", "draft": False}, {"id": 3, "tag_name": "orphan", "draft": False}], "target/releases/2/assets": [{"id": 42, "name": "TIMESTAMPS.txt", "state": "uploaded", "url": "asset"}]}, json.dumps(payload).encode())
        result = scan_repository(source, "proj", "target", src, dst, "secret")
        self.assertEqual(result["renamed"], 1)
        self.assertEqual(result["destination_only"], 1)
        self.assertEqual(result["source_only"], 1)

    def test_cli_reports_failure_exit_without_writes(self):
        import tempfile
        from unittest.mock import patch
        from repo_mirror.manifest_migration import main
        config = {"destination_account_type": "personal", "sources": [{"type": "github", "base_url": "https://github.com", "owner": "alice", "repos": ["proj"]}]}
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".json") as f:
            json.dump(config, f); f.flush()
            with patch.dict("os.environ", {"GH_TOKEN": "test", "DESTINATION_OWNER": "dest", "MIRROR_TIMESTAMP_SECRET": "secret"}, clear=True), patch("repo_mirror.manifest_migration.scan_repository", side_effect=ValueError("conflict")):
                self.assertEqual(main(["--config", f.name]), 1)

    def test_untagged_destination_draft_blocks_repository(self):
        from repo_mirror.manifest_migration import scan_repository
        from repo_mirror.sources import source_marker
        source = {"type": "github", "base_url": "https://github.com", "owner": "alice", "repos": ["proj"]}
        class Routes(API):
            def pages(self, url):
                return [{"id": 1, "tag_name": "v1", "draft": False}] if url.startswith("https://api.github.com") else [{"id": 2, "tag_name": "untagged-abc", "draft": True}]
            def request(self, method, url, data=None):
                return {"description": source_marker(source, "proj")}
        with self.assertRaisesRegex(ValueError, "draft"):
            scan_repository(source, "proj", "target", Routes([]), Routes([]), "secret")


class HardeningTests(unittest.TestCase):
    source = {"type": "github", "base_url": "https://github.com", "owner": "alice", "repos": ["proj"]}

    def scan(self, source_assets, dest_assets, content=b"", body=None):
        from repo_mirror.manifest_migration import scan_repository
        from repo_mirror.sources import source_marker
        marker = source_marker(self.source, "proj")
        class Routes(API):
            def __init__(self, upstream):
                super().__init__([], content); self.upstream = upstream
            def pages(self, url):
                if url.endswith("/releases"):
                    return [{"id": 1 if self.upstream else 2, "tag_name": "v1", "draft": False, "body": None if self.upstream else body}]
                return [dict(x) for x in (source_assets if self.upstream else dest_assets)]
            def request(self, method, url, data=None):
                if method == "GET": return {"description": marker}
                self.writes.append((url, data))
                dest_assets[0]["name"] = data["name"]
        dst = Routes(False)
        result = scan_repository(self.source, "proj", "target", Routes(True), dst, "secret")
        return result, dst.writes

    def test_no_destination_manifest_ignores_source_reserved_name(self):
        result, writes = self.scan([{"name": "TIMESTAMPS.txt"}], [])
        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertFalse(writes)

    def test_foreign_new_manifest_is_not_success(self):
        result, writes = self.scan([], [{"id": 9, "name": "REPO-MIRROR-MANIFEST.json", "state": "uploaded", "url": "asset"}], b"{}")
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["unchanged"], 0)
        self.assertFalse(writes)

    def test_failure_reports_safe_actionable_category(self):
        import contextlib
        import io
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.scan([], [{'id': 9, 'name': 'REPO-MIRROR-MANIFEST.json', 'state': 'uploaded', 'url': 'asset'}], b'{}')
        self.assertIn('manifest identity mismatch', output.getvalue())
        self.assertNotIn('url=', output.getvalue())

    def test_signed_new_manifest_is_idempotent(self):
        from repo_mirror.sources import source_marker
        payload = {"owner": "repo-mirror:timestamps:v1", "source": source_marker(self.source, "proj"), "tag": "v1", "assets": {}}
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
        payload["signature"] = hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
        result, writes = self.scan([], [{"id": 9, "name": "REPO-MIRROR-MANIFEST.json", "state": "uploaded", "url": "asset"}], json.dumps(payload).encode())
        self.assertEqual(result["unchanged"], 1)
        self.assertFalse(writes)

    def test_cli_incomplete_counts_return_failure(self):
        import tempfile
        from unittest.mock import patch
        from repo_mirror.manifest_migration import main
        config = {"destination_account_type": "personal", "sources": [self.source]}
        with tempfile.NamedTemporaryFile(mode="w+", suffix=".json") as f:
            json.dump(config, f); f.flush()
            for field in ("source_only", "destination_only", "missing_repository"):
                counts = dict.fromkeys(("renamed", "unchanged", "source_only", "destination_only", "failed", "missing_repository"), 0)
                counts[field] = 1
                with self.subTest(field=field), patch.dict("os.environ", {"GH_TOKEN": "test", "DESTINATION_OWNER": "dest", "MIRROR_TIMESTAMP_SECRET": "secret"}, clear=True), patch("repo_mirror.manifest_migration.scan_repository", return_value=counts):
                    self.assertEqual(main(["--config", f.name]), 1)

    def test_staging_marker_is_not_success(self):
        result, writes = self.scan([], [{"id": 9, "name": "TIMESTAMPS.txt.repo-mirror-staging", "state": "uploaded", "url": "asset"}])
        self.assertEqual(result["failed"], 1)
        self.assertFalse(writes)

    def test_asset_download_count_changes_do_not_block_rename(self):
        payload = {"owner": "repo-mirror:timestamps:v1", "source": "source", "tag": "v1", "assets": {}}
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
        payload["signature"] = hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
        class VolatileAPI(API):
            def pages(self, url):
                self.assets[0]["download_count"] = self.assets[0].get("download_count", 0) + 1
                return super().pages(url)
        dst = VolatileAPI([{"id": 42, "name": "TIMESTAMPS.txt", "state": "uploaded", "url": "asset"}], json.dumps(payload).encode())
        self.assertEqual(migrate_release(API([]), dst, "source-assets", "destination", 5, "source", "v1", "secret"), "renamed")

    def test_pending_release_marker_blocks_no_manifest_success(self):
        result, writes = self.scan([], [], body="<!-- repo-mirror:pending:v3 -->")
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["unchanged"], 0)
        self.assertFalse(writes)

    def test_pending_release_name_blocks_no_manifest_success(self):
        from repo_mirror.manifest_migration import scan_repository
        from repo_mirror.sources import source_marker
        source = self.source
        class Routes(API):
            def pages(self, url):
                if url.endswith('/releases'):
                    return [{'id': 1, 'tag_name': 'v1', 'draft': False, 'name': '[repo-mirror:pending:v2]encoded'}]
                return []
            def request(self, method, url, data=None):
                return {'description': source_marker(source, 'proj')}
        result = scan_repository(source, 'proj', 'target', Routes([]), Routes([]), 'secret')
        self.assertEqual(result['failed'], 1)

    def test_new_manifest_appearing_during_empty_scan_is_not_success(self):
        from repo_mirror.manifest_migration import scan_repository
        from repo_mirror.sources import source_marker
        source = self.source
        class Routes(API):
            def __init__(self, upstream):
                super().__init__([])
                self.upstream, self.calls = upstream, 0
            def pages(self, url):
                if url.endswith('/releases'):
                    return [{'id': 1, 'tag_name': 'v1', 'draft': False}]
                self.calls += 1
                if not self.upstream and self.calls > 1:
                    return [{'id': 9, 'name': 'TIMESTAMPS.txt', 'state': 'uploaded', 'url': 'asset'}]
                return []
            def request(self, method, url, data=None):
                return {'description': source_marker(source, 'proj')}
        result = scan_repository(source, 'proj', 'target', Routes(True), Routes(False), 'secret')
        self.assertEqual(result['failed'], 1)
