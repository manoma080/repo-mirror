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
    def test_forgejo_assets_are_fetched_once_per_check_without_pagination(self):
        payload = {'owner': 'repo-mirror:timestamps:v1', 'source': 'source', 'tag': 'v1', 'assets': {}}
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()
        payload['signature'] = hmac.new(b'secret', raw, hashlib.sha256).hexdigest()
        class Forgejo(API):
            def pages(self, url):
                raise AssertionError('Forgejo repeats identical pages forever')
            def request(self, method, url, data=None):
                self.calls = getattr(self, 'calls', 0) + 1
                return [{'name': 'file.zip'}]
        src = Forgejo([])
        dst = API([{'id': 42, 'name': 'TIMESTAMPS.txt', 'state': 'uploaded', 'url': 'asset'}], json.dumps(payload).encode())
        self.assertEqual(migrate_release(src, dst, 'source-assets', 'destination', 5, 'source', 'v1', 'secret', source_type='forgejo'), 'renamed')
        self.assertGreaterEqual(src.calls, 3)
        self.assertEqual(len(dst.writes), 1)

    def test_github_collision_on_second_page_blocks_write(self):
        from repo_mirror.github_api import API as RealAPI
        class GitHub(RealAPI):
            def request(self, method, url, data=None, **kwargs):
                if url.endswith('&page=1'): return [{'name': 'file.zip'}]
                if url.endswith('&page=2'): return [{'name': 'TIMESTAMPS.txt'}]
                return []
        dst = API([{'id': 42, 'name': 'TIMESTAMPS.txt', 'state': 'uploaded', 'url': 'asset'}])
        with self.assertRaisesRegex(ValueError, 'source reserved name'):
            migrate_release(GitHub(), dst, 'source-assets', 'destination', 5, 'source', 'v1', 'secret')
        self.assertFalse(dst.writes)

    def test_forgejo_invalid_assets_and_late_collision_block_write(self):
        payload = {'owner': 'repo-mirror:timestamps:v1', 'source': 'source', 'tag': 'v1', 'assets': {}}
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()
        payload['signature'] = hmac.new(b'secret', raw, hashlib.sha256).hexdigest()
        for response in ({}, None, [None], [{'name': 'TIMESTAMPS.txt'}]):
            with self.subTest(response=response):
                class Forgejo:
                    def request(self, method, url): return response
                dst = API([{'id': 42, 'name': 'TIMESTAMPS.txt', 'state': 'uploaded', 'url': 'asset'}], json.dumps(payload).encode())
                with self.assertRaises(ValueError):
                    migrate_release(Forgejo(), dst, 'source-assets', 'destination', 5, 'source', 'v1', 'secret', source_type='forgejo')
                self.assertFalse(dst.writes)
        class ChangingForgejo:
            def __init__(self): self.calls = 0
            def request(self, method, url):
                self.calls += 1
                return [] if self.calls == 1 else [{'name': 'TIMESTAMPS.txt'}]
        dst = API([{'id': 42, 'name': 'TIMESTAMPS.txt', 'state': 'uploaded', 'url': 'asset'}], json.dumps(payload).encode())
        with self.assertRaisesRegex(ValueError, 'source reserved name'):
            migrate_release(ChangingForgejo(), dst, 'source-assets', 'destination', 5, 'source', 'v1', 'secret', source_type='forgejo')
        self.assertFalse(dst.writes)

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
    def test_cli_logs_repository_boundaries_and_redacts_labels(self):
        import contextlib
        import io
        import tempfile
        from unittest.mock import patch
        from repo_mirror.manifest_migration import main
        owner = 'ghp_' + 'a' * 36
        config = {'destination_account_type': 'personal', 'sources': [{'type': 'github', 'base_url': 'https://github.com', 'owner': owner, 'repos': ['proj']}]}
        counts = dict.fromkeys(('renamed', 'unchanged', 'source_only', 'destination_only', 'failed', 'missing_repository'), 0)
        output = io.StringIO()
        with tempfile.NamedTemporaryFile(mode='w+', suffix='.json') as f:
            json.dump(config, f); f.flush()
            with patch.dict('os.environ', {'GH_TOKEN': 'test', 'DESTINATION_OWNER': 'dest', 'MIRROR_TIMESTAMP_SECRET': 'secret'}, clear=True), patch('repo_mirror.manifest_migration.scan_repository', return_value=counts), contextlib.redirect_stdout(output):
                self.assertEqual(main(['--config', f.name]), 0)
        self.assertIn('repository 1/1 started', output.getvalue())
        self.assertIn('repository 1/1 finished', output.getvalue())
        self.assertNotIn(owner, output.getvalue())

    def test_scan_passes_forgejo_type_to_asset_check(self):
        from unittest.mock import patch
        from repo_mirror.manifest_migration import scan_repository
        from repo_mirror.sources import source_marker
        source = {'type': 'forgejo', 'base_url': 'https://code.example', 'owner': 'alice', 'repos': ['proj']}
        class Routes(API):
            def pages(self, url):
                if url.endswith('/releases'):
                    return [{'id': 1, 'tag_name': 'v1', 'draft': False}]
                return [{'id': 42, 'name': 'TIMESTAMPS.txt', 'state': 'uploaded', 'url': 'asset'}]
            def request(self, method, url, data=None):
                return {'description': source_marker(source, 'proj')}
        with patch('repo_mirror.manifest_migration.migrate_release', return_value='renamed') as migrate:
            result = scan_repository(source, 'proj', 'target', Routes([]), Routes([]), 'secret')
        self.assertEqual(result['renamed'], 1)
        self.assertEqual(migrate.call_args.kwargs['source_type'], 'forgejo')

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


class PreWriteGuardTests(unittest.TestCase):
    def test_late_changes_prevent_patch(self):
        from urllib.error import HTTPError
        from repo_mirror.manifest_migration import scan_repository
        from repo_mirror.sources import source_marker

        for scenario in ('upstream-resumes', 'upstream-403', 'upstream-redirect', 'release', 'marker', 'source-collision'):
            with self.subTest(scenario=scenario):
                unavailable = scenario.startswith('upstream-')
                name = 'ProsperoEden' if unavailable else 'proj'
                source = {'type': 'github', 'base_url': 'https://github.com', 'owner': 'Swordpdf' if unavailable else 'alice', 'repos': [name]}
                marker = source_marker(source, name)
                payload = {'owner': 'repo-mirror:timestamps:v1', 'source': marker, 'tag': 'v1', 'assets': {}}
                encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()
                payload['signature'] = hmac.new(b'secret', encoded, hashlib.sha256).hexdigest()
                state = {'downloaded': False}
                class Source(API):
                    def request(self, method, url, data=None):
                        if unavailable:
                            if state['downloaded'] and scenario == 'upstream-resumes': return {}
                            code = 403 if state['downloaded'] and scenario == 'upstream-403' else 404
                            error_url = 'https://other.example/' if state['downloaded'] and scenario == 'upstream-redirect' else url
                            raise HTTPError(error_url, code, 'error', {}, None)
                        return {}
                    def pages(self, url):
                        if url.endswith('/releases'): return [{'id': 1, 'tag_name': 'v1', 'draft': False}]
                        return [{'name': 'TIMESTAMPS.txt'}] if state['downloaded'] and scenario == 'source-collision' else []
                class Destination(API):
                    def pages(self, url):
                        if url == 'target/releases':
                            return [{'id': 3 if state['downloaded'] and scenario == 'release' else 2, 'tag_name': 'v1', 'draft': False}]
                        return super().pages(url)
                    def request(self, method, url, data=None):
                        if method == 'GET': return {'description': 'foreign' if state['downloaded'] and scenario == 'marker' else marker}
                        return super().request(method, url, data)
                    def download(self, url, f):
                        super().download(url, f)
                        state['downloaded'] = True
                dst = Destination([{'id': 42, 'name': 'TIMESTAMPS.txt', 'state': 'uploaded', 'url': 'asset'}], json.dumps(payload).encode())
                result = scan_repository(source, name, 'target', Source([]), dst, 'secret')
                self.assertEqual(result['failed'], 1)
                self.assertEqual(dst.writes, [])


class UnavailableSourceTests(unittest.TestCase):
    def test_authorized_404_signed_manifest_is_renamed(self):
        from urllib.error import HTTPError
        from repo_mirror.manifest_migration import scan_repository
        from repo_mirror.sources import source_marker
        source = {"type": "github", "base_url": "https://github.com", "owner": "Swordpdf", "repos": ["ProsperoEden"]}
        marker = source_marker(source, "ProsperoEden")
        payload = {"owner": "repo-mirror:timestamps:v1", "source": marker, "tag": "v1", "assets": {}}
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
        payload["signature"] = hmac.new(b"secret", raw, hashlib.sha256).hexdigest()
        class Source(API):
            def request(self, method, url, data=None):
                raise HTTPError(url, 404, "not found", {}, None)
            def pages(self, url):
                raise AssertionError("unavailable source must not be listed")
        class Destination(API):
            def pages(self, url):
                if url == "target/releases":
                    return [{"id": 2, "tag_name": "v1", "draft": False}]
                return super().pages(url)
            def request(self, method, url, data=None):
                if method == "GET": return {"description": marker}
                return super().request(method, url, data)
        dst = Destination([{"id": 42, "name": "TIMESTAMPS.txt", "state": "uploaded", "url": "asset"}], json.dumps(payload).encode())
        result = scan_repository(source, "ProsperoEden", "target", Source([]), dst, "secret")
        self.assertEqual(result["renamed"], 1)
        self.assertEqual(len(dst.writes), 1)

    def test_exception_boundaries_and_validation(self):
        from urllib.error import HTTPError, URLError
        from repo_mirror.manifest_migration import scan_repository
        from repo_mirror.sources import source_marker

        def run(owner, name, *, status=404, asset_names=("TIMESTAMPS.txt",), signed=True, payload_updates=None, release_updates=None, marker_ok=True, base="https://github.com"):
            source = {"type": "github", "base_url": base, "owner": owner, "repos": [name]}
            marker = source_marker(source, name)
            payload = {"owner": "repo-mirror:timestamps:v1", "source": marker, "tag": "v1", "assets": {}}
            payload.update(payload_updates or {})
            raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
            payload["signature"] = hmac.new(b"secret" if signed else b"wrong", raw, hashlib.sha256).hexdigest()
            release = {"id": 2, "tag_name": "v1", "draft": False}
            release.update(release_updates or {})
            class Source(API):
                def request(self, method, url, data=None):
                    if status == "network": raise URLError("network")
                    if status: raise HTTPError(url, status, "error", {}, None)
                    return {}
                def pages(self, url):
                    if status: raise HTTPError(url, 404, "error", {}, None)
                    if url.endswith("/releases"): return [{"id": 3, "tag_name": "v1", "draft": False}]
                    return [{"name": "TIMESTAMPS.txt"}]
            class Destination(API):
                def pages(self, url):
                    if url == "target/releases": return [dict(release)]
                    return super().pages(url)
                def request(self, method, url, data=None):
                    if method == "GET": return {"description": marker if marker_ok else "foreign"}
                    return super().request(method, url, data)
            assets = [{"id": i + 42, "name": n, "state": "uploaded", "url": "asset"} for i, n in enumerate(asset_names)]
            dst = Destination(assets, json.dumps(payload).encode())
            try:
                result = scan_repository(source, name, "target", Source([]), dst, "secret")
            except (HTTPError, URLError, RuntimeError) as exc:
                if isinstance(exc, HTTPError):
                    exc.close()
                result = None
            return result, dst.writes

        for owner, name in (("Swordpdf", "ProsperoEden"), ("mihawk-99", "PS5_Lichii"), ("mihawk-99", "PS5_Hookshot")):
            with self.subTest(owner=owner, name=name):
                result, writes = run(owner, name)
                self.assertEqual(result["renamed"], 1)
                self.assertEqual(len(writes), 1)
        for args in (
            {"status": 403}, {"status": 500}, {"status": "network"},
            {"status": 0}, {"base": "https://example.com"},
            {"signed": False}, {"payload_updates": {"tag": "other"}},
            {"payload_updates": {"source": "foreign"}}, {"payload_updates": {"pending": True}},
            {"asset_names": ("TIMESTAMPS.txt", "REPO-MIRROR-MANIFEST.json")},
            {"asset_names": ("TIMESTAMPS.txt.repo-mirror-staging",)},
            {"release_updates": {"draft": True}},
            {"release_updates": {"tag_name": "untagged-v1"}},
            {"release_updates": {"body": "repo-mirror pending"}}, {"marker_ok": False},
        ):
            with self.subTest(args=args):
                result, writes = run("Swordpdf", "ProsperoEden", **args)
                self.assertFalse(writes)
                self.assertTrue(result is None or result["failed"] == 1)
        result, writes = run("other", "unknown")
        self.assertIsNone(result)
        self.assertFalse(writes)

    def test_redirected_404_does_not_enable_exception(self):
        from urllib.error import HTTPError
        from repo_mirror.manifest_migration import scan_repository
        from repo_mirror.sources import source_marker
        source = {"type": "github", "base_url": "https://github.com", "owner": "Swordpdf", "repos": ["ProsperoEden"]}
        class Source(API):
            def request(self, method, url, data=None):
                raise HTTPError("https://other.example/redirect", 404, "error", {}, None)
        class Destination(API):
            def request(self, method, url, data=None):
                return {"description": source_marker(source, "ProsperoEden")}
        with self.assertRaises(HTTPError) as caught:
            scan_repository(source, "ProsperoEden", "target", Source([]), Destination([]), "secret")
        caught.exception.close()
