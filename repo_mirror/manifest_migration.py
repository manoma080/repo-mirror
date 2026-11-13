"""Safety primitives for a future dedicated manifest migration command.

Not wired into the normal mirror: callers must establish release identity and
re-read release state immediately before invoking this function.
"""

import hashlib
import hmac
import json
import io

LEGACY = 'TIMESTAMPS.txt'
NEW = 'REPO-MIRROR-MANIFEST.json'
RESERVED = {LEGACY, NEW, LEGACY + '.repo-mirror-staging', NEW + '.repo-mirror-staging'}
MAX_MANIFEST_SIZE = 1024 * 1024
# Explicitly authorized exceptions, not a general unavailable-upstream policy.
UNAVAILABLE_SOURCES = frozenset({('Swordpdf', 'ProsperoEden'), ('mihawk-99', 'PS5_Lichii'), ('mihawk-99', 'PS5_Hookshot')})
SAFE_MIGRATION_ERRORS = frozenset({
    'missing secret', 'destination asset not uploaded', 'missing or conflicting manifest',
    'source reserved name', 'invalid manifest identity', 'manifest too large',
    'invalid manifest', 'manifest identity mismatch', 'manifest signature mismatch',
    'pending manifest requires manual review', 'manifest changed', 'rename verification failed',
    'ambiguous release identity', 'ambiguous untagged destination draft',
    'draft release requires manual review', 'invalid release body',
    'pending release requires manual review', 'destination repository disappeared', 'release changed',
    'source availability changed', 'invalid source assets',
})


def migration_error(exc):
    from repo_mirror.logging_utils import error_detail
    if type(exc) is ValueError and len(exc.args) == 1 and exc.args[0] in SAFE_MIGRATION_ERRORS:
        return exc.args[0] + '; inspect the release and rerun after reconciliation'
    return error_detail(exc)


class _BoundedBuffer(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_MANIFEST_SIZE:
            raise ValueError('manifest too large')
        return super().write(data)


def migrate_release(src, dst, source_assets_url, target, release_id, source, tag, secret, pre_write=None, source_type='github'):
    """Rename an authenticated legacy manifest, or refuse ambiguous state."""
    if not secret:
        raise ValueError('missing secret')
    url = target + '/releases/' + str(release_id) + '/assets'

    def read():
        dest_assets = list(dst.pages(url))
        if any(a.get('state') != 'uploaded' for a in dest_assets):
            raise ValueError('destination asset not uploaded')
        matches = [a for a in dest_assets if a.get('name') in RESERVED]
        if len(matches) != 1 or matches[0].get('name') not in (LEGACY, NEW):
            raise ValueError('missing or conflicting manifest')
        asset = matches[0]
        if source_assets_url is not None:
            source_assets = src.request('GET', source_assets_url) if source_type == 'forgejo' else src.pages(source_assets_url)
            if not isinstance(source_assets, list) or any(
                not isinstance(a, dict) or not isinstance(a.get('name'), str) or not a['name']
                for a in source_assets
            ):
                raise ValueError('invalid source assets')
            if any(a.get('name') in RESERVED for a in source_assets):
                raise ValueError('source reserved name')
        if type(asset.get('id')) is not int or asset['id'] <= 0 or not isinstance(asset.get('url'), str) or not asset['url']:
            raise ValueError('invalid manifest identity')
        # GitHub adds volatile counters and timestamps; they are not identity.
        return {k: asset.get(k) for k in ('id', 'name', 'state', 'url', 'size', 'digest', 'updated_at')}

    asset = read()
    def download(item):
        size = item.get('size')
        if size is not None and (type(size) is not int or size < 0 or size > MAX_MANIFEST_SIZE):
            raise ValueError('manifest too large')
        f = _BoundedBuffer()
        dst.download(item['url'], f)
        return f.getvalue()

    raw = download(asset)
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError('invalid manifest') from None
    if not isinstance(data, dict) or any(data.get(k) != v for k, v in {
        'owner': 'repo-mirror:timestamps:v1', 'source': source, 'tag': tag
    }.items()) or not isinstance(data.get('assets'), dict):
        raise ValueError('manifest identity mismatch')
    signature = data.pop('signature', None)
    payload = json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
        raise ValueError('manifest signature mismatch')
    if data.get('pending') is not None and data.get('pending') is not False:
        raise ValueError('pending manifest requires manual review')
    if read() != asset:
        raise ValueError('manifest changed')
    if download(asset) != raw or read() != asset:
        raise ValueError('manifest changed')
    if asset['name'] == NEW:
        return 'unchanged'
    if pre_write is not None:
        pre_write()
    dst.request('PATCH', target + '/releases/assets/' + str(asset['id']), {'name': NEW})
    assets = list(dst.pages(url))
    renamed = [a for a in assets if a.get('name') in RESERVED]
    if any(a.get('state') != 'uploaded' for a in assets) or len(renamed) != 1 or renamed[0].get('name') != NEW or renamed[0].get('id') != asset['id'] or renamed[0].get('state') != 'uploaded' or renamed[0].get('url') != asset['url']:
        raise ValueError('rename verification failed')
    final = {k: renamed[0].get(k) for k in ('id', 'name', 'state', 'url', 'size', 'digest', 'updated_at')}
    if download(renamed[0]) != raw or read() != final:
        raise ValueError('rename verification failed')
    return 'renamed'


def scan_repository(source, name, target, src, dst, secret):
    """Scan every release; never infer ownership from an asset filename alone."""
    from urllib.parse import quote
    from urllib.error import HTTPError
    from repo_mirror.sources import source_api_base, source_marker, get_existing, check_existing

    marker = source_marker(source, name)
    repo = get_existing(dst, target)
    if repo is None:
        return {"renamed": 0, "unchanged": 0, "source_only": 0, "destination_only": 0, "failed": 0, "missing_repository": 1}
    check_existing(repo, marker)
    upstream = source_api_base(source) + "/repos/" + quote(source["owner"], safe="") + "/" + quote(name, safe="")
    unavailable = False
    if (source.get('type') == 'github' and source.get('base_url') == 'https://github.com'
            and source.get('repos') != 'all' and name in source.get('repos', [])
            and (source.get('owner'), name) in UNAVAILABLE_SOURCES):
        try:
            src.request('GET', upstream)
        except HTTPError as exc:
            if exc.code != 404 or exc.url != upstream:
                raise
            exc.close()
            unavailable = True
            print(f"WARNING: {source['owner']}/{name}: source repository GET returned 404; source assets cannot be verified. User accepted this migration risk; availability may change.")
    source_releases = [] if unavailable else list(src.pages(upstream + "/releases"))
    dest_releases = list(dst.pages(target + "/releases"))

    def index(releases):
        result = {}
        ids = set()
        for release in releases:
            tag, rid, draft = release.get("tag_name"), release.get("id"), release.get("draft")
            if not isinstance(tag, str) or not tag or type(rid) is not int or rid <= 0 or type(draft) is not bool or tag in result or rid in ids:
                raise ValueError("ambiguous release identity")
            result[tag] = release
            ids.add(rid)
        return result

    upstream_by_tag, destination_by_tag = index(source_releases), index(dest_releases)
    if any(r['draft'] and r['tag_name'].startswith('untagged-') for r in dest_releases):
        raise ValueError('ambiguous untagged destination draft')
    result = {"renamed": 0, "unchanged": 0, "source_only": 0, "destination_only": 0, "failed": 0, "missing_repository": 0}
    for tag in (() if unavailable else destination_by_tag.keys() - upstream_by_tag.keys()):
        result["destination_only"] += 1
        print(f"destination-only release: {tag!r}")
    result["source_only"] = len(upstream_by_tag.keys() - destination_by_tag.keys())
    for tag in (destination_by_tag.keys() if unavailable else upstream_by_tag.keys() & destination_by_tag.keys()):
        a, b = upstream_by_tag.get(tag), destination_by_tag[tag]
        try:
            if (a is not None and a["draft"]) or b["draft"] or tag.startswith("untagged-"):
                raise ValueError("draft release requires manual review")
            body = b.get("body")
            release_name = b.get("name")
            if isinstance(release_name, str) and release_name.startswith(("[repo-mirror:pending:v1]", "[repo-mirror:pending:v2]")):
                raise ValueError("pending release requires manual review")
            if body is not None and not isinstance(body, str):
                raise ValueError("invalid release body")
            if body and "repo-mirror" in body.lower() and "pending" in body.lower():
                raise ValueError("pending release requires manual review")
            source_assets_url = None
            if a is not None:
                source_assets_url = upstream + "/releases/" + str(a["id"]) + "/assets"
            assets_url = target + "/releases/" + str(b["id"]) + "/assets"
            assets = list(dst.pages(assets_url))
            reserved = [x for x in assets if x.get("name") in RESERVED]
            if not reserved:
                if any(x.get("state") != "uploaded" for x in assets):
                    raise ValueError("destination asset not uploaded")
                latest = list(dst.pages(assets_url))
                if any(x.get('name') in RESERVED or x.get('state') != 'uploaded' for x in latest):
                    raise ValueError('manifest changed')
                result["unchanged"] += 1
                continue
            def snapshot(release):
                if release is None:
                    return None
                return tuple(release.get(k) for k in ("id", "tag_name", "draft", "name", "target_commitish", "published_at", "body"))

            def pre_write():
                # A 404 does not prove deletion; this is an explicitly authorized
                # exception only while the exact repository URL still returns 404.
                if unavailable:
                    try:
                        src.request('GET', upstream)
                    except HTTPError as exc:
                        matches = exc.code == 404 and exc.url == upstream
                        exc.close()
                        if not matches:
                            raise
                    else:
                        raise ValueError('source availability changed')
                elif snapshot(index(list(src.pages(upstream + "/releases"))).get(tag)) != snapshot(a):
                    raise ValueError("release changed")
                current_repo = get_existing(dst, target)
                if current_repo is None:
                    raise ValueError("destination repository disappeared")
                check_existing(current_repo, marker)
                if snapshot(index(list(dst.pages(target + "/releases"))).get(tag)) != snapshot(b):
                    raise ValueError("release changed")

            outcome = migrate_release(src, dst, source_assets_url, target, b["id"], marker, tag, secret, pre_write=pre_write, source_type=source['type'])
            result[outcome] += 1
        except Exception as exc:
            result["failed"] += 1
            print(f"release {tag!r} failed: {migration_error(exc)}")
    return result


def main(argv=None):
    """Run with python -m repo_mirror.manifest_migration --config FILE."""
    import argparse
    import os
    from urllib.parse import quote
    from repo_mirror.constants import GITHUB, NAME
    from repo_mirror.github_api import API, SourceAPI, AppTokenProvider
    from repo_mirror.logging_utils import error_detail, safe_label
    from repo_mirror.sources import validate_source, discover, destination_name
    from repo_mirror.source_filter import filter_plan

    parser = argparse.ArgumentParser(description="Migrate authenticated legacy release manifests")
    parser.add_argument("--config", required=True)
    parser.add_argument("--source-urls", default=os.environ.get("MIRROR_SOURCE_URLS"))
    args = parser.parse_args(argv)
    try:
        secret, owner, token = os.environ.get("MIRROR_TIMESTAMP_SECRET"), os.environ.get("DESTINATION_OWNER"), os.environ.get("GH_TOKEN")
        app_id, key = os.environ.get("MIRROR_APP_ID"), os.environ.pop("MIRROR_APP_PRIVATE_KEY", None)
        if not secret or not owner or not NAME.fullmatch(owner) or owner in (".", "..") or not (token or (app_id and key)):
            raise ValueError("valid destination credentials, owner, and timestamp secret required")
        with open(args.config, encoding="utf-8") as f:
            config = json.load(f)
        if not isinstance(config, dict) or not isinstance(config.get("sources"), list):
            raise ValueError("config sources must be a list")
        account_type = os.environ.get("DESTINATION_ACCOUNT_TYPE", config.get("destination_account_type"))
        if account_type not in ("personal", "organization"):
            raise ValueError("destination_account_type must be personal or organization")
        if app_id or key:
            token = AppTokenProvider(app_id or "", key, owner, account_type)
        github, other, dst = SourceAPI(token), API(), API(token)
        plan, seen = [], set()
        for i, source in enumerate(config["sources"], 1):
            validate_source(source)
            for name in discover(source, github if source["type"] == "github" else other):
                dest = destination_name(source, name).lower()
                if dest in seen:
                    raise ValueError("duplicate destination repository")
                seen.add(dest)
                plan.append((i, source, name))
        plan = filter_plan(plan, args.source_urls)
        totals = {"renamed": 0, "unchanged": 0, "source_only": 0, "destination_only": 0, "failed": 0, "missing_repository": 0}
        repository_failed = 0
        for repo_index, (_, source, name) in enumerate(plan, 1):
            target = GITHUB + "/repos/" + quote(owner, safe="") + "/" + quote(destination_name(source, name), safe="")
            label = f"{safe_label(source.get('owner'))}/{safe_label(name)}"
            print(f"repository {repo_index}/{len(plan)} started source_repo={label}", flush=True)
            try:
                result = scan_repository(source, name, target, github if source["type"] == "github" else other, dst, secret)
                for key in totals:
                    totals[key] += result[key]
            except Exception as exc:
                repository_failed += 1
                print(f"repository {repo_index}/{len(plan)} failed source_repo={label}: {migration_error(exc)}", flush=True)
            finally:
                print(f"repository {repo_index}/{len(plan)} finished source_repo={label}", flush=True)
        print("summary repositories=" + str(len(plan)) + " repository_failed=" + str(repository_failed) + " " + " ".join(f"{k}={v}" for k, v in totals.items()))
        return 1 if repository_failed or any(totals[k] for k in ("failed", "source_only", "destination_only", "missing_repository")) else 0
    except Exception as exc:
        print("migration failed: " + error_detail(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
