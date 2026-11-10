"""One-time, explicitly approved identity binding for one release draft only."""
import hashlib
import hmac
import json

SOURCE = "[repo-mirror: forgejo:https://git.etawen.dev/earthonion/yt_adskip_ps4]"
TARGET = "https://api.github.com/repos/manoma080/mirror-earthonion-yt_adskip_ps4"
FILES = {
    "yt_adblock-debug.prx": "585a2a266718648cc35b8bf0992f75490187f260fe623860c6deafe42d14cdf9",
    "yt_adblock.prx": "81f4e52199e928de5932c84209fc39969f7f1386600bd81b00cbab547eedcfdc",
}


def make_binding(source, destination, source_hashes, destination_hashes, secret):
    """Build the exact production marker from validated fixed-scope data."""
    if source_hashes != FILES or destination_hashes != dict(FILES, **{"TIMESTAMPS.txt": "96f8c48f6d31b0ea87b053aef1a076b1519ed96d177406815e43580cedb90a48"}):
        raise ValueError("asset mismatch")
    if source != {"id": 141, "tag_name": "v1.36", "name": "v1.36", "body": "GoldHEN plugin (release + debug PRX)", "draft": False, "prerelease": False}:
        raise ValueError("source mismatch")
    if destination != {"id": 437729977, "tag_name": "untagged-446b630fff7654a5b702", "name": "v1.36", "body": source["body"], "draft": True, "prerelease": False} or not secret:
        raise ValueError("destination mismatch")
    state = {"phase": "WAITING_SOURCE", "digest": None, "replace": False, "skipped": []}
    payload = {"version": "v3", "source": SOURCE, "tag": "v1.36", "target": TARGET, "release_id": 437729977, "state": state}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), b"repo-mirror:release-recovery:v3\0" + encoded, hashlib.sha256).hexdigest()
    return destination["body"] + "\n[repo-mirror:pending:v3]" + json.dumps(dict(state, signature=signature), sort_keys=True, ensure_ascii=True, separators=(",", ":"))


SOURCE_API = "https://git.etawen.dev/api/v1/repos/earthonion/yt_adskip_ps4/releases"
RELEASE = TARGET + "/releases/437729977"
TIMESTAMP_HASH = "96f8c48f6d31b0ea87b053aef1a076b1519ed96d177406815e43580cedb90a48"


def _stable(value):
    # Counts can change merely because someone downloaded an asset.
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k not in ("download_count", "archive_download_count")}
    if isinstance(value, (list, tuple)):
        return [_stable(v) for v in value]
    return value


def _assets(items):
    if not isinstance(items, list) or any(not isinstance(x, dict) or not isinstance(x.get("name"), str) for x in items):
        raise ValueError("invalid assets")
    result = {x["name"]: x for x in items}
    if len(result) != len(items):
        raise ValueError("duplicate asset")
    return result


def _read(src, dst):
    repo = dst.request("GET", TARGET)
    source = src.request("GET", SOURCE_API + "/tags/v1.36")
    source_assets = src.request("GET", SOURCE_API + "/141/assets")
    release = dst.request("GET", RELEASE)
    assets = dst.pages(RELEASE + "/assets")
    releases = list(dst.pages(TARGET + "/releases"))
    if _stable(_assets(source.get("assets"))) != _stable(_assets(source_assets)):
        raise ValueError("source asset lists disagree")
    if _stable(_assets(release.get("assets"))) != _stable(_assets(assets)):
        raise ValueError("destination asset lists disagree")
    if any(not isinstance(r, dict) for r in releases):
        raise ValueError("invalid release list")
    approved = [r for r in releases if r.get("id") == 437729977]
    drafts = [r for r in releases if r.get("draft") is True]
    if (len(approved) != 1 or _stable(approved[0]) != _stable(release)
            or len(drafts) != 1 or drafts[0].get("id") != 437729977
            or any(r.get("tag_name") == "v1.36" for r in releases)):
        raise ValueError("ambiguous destination releases")
    return repo, source, source_assets, release, assets, releases


def reconcile(src, dst, secret):
    import os
    if not secret or os.environ.get("MIRROR_TIMESTAMP_SECRET") != secret:
        raise ValueError("missing or mismatched signing secret")
    before = _read(src, dst)
    repo, source, source_assets, release, assets, releases = before
    if repo.get("full_name") != "manoma080/mirror-earthonion-yt_adskip_ps4" or repo.get("owner", {}).get("login") != "manoma080" or repo.get("description") != SOURCE:
        raise ValueError("destination identity mismatch")
    expected_source = {"id": 141, "tag_name": "v1.36", "name": "v1.36", "body": "GoldHEN plugin (release + debug PRX)", "draft": False, "prerelease": False, "target_commitish": "eacb279874b53a176d86504249cf0d50ab276336"}
    expected_release = {"id": 437729977, "tag_name": "untagged-446b630fff7654a5b702", "name": "v1.36", "draft": True, "prerelease": False, "target_commitish": "main"}
    if any(source.get(k) != v or type(source.get(k)) is not type(v) for k,v in expected_source.items()) or any(release.get(k) != v or type(release.get(k)) is not type(v) for k,v in expected_release.items()):
        raise ValueError("release identity mismatch")
    sa, da = _assets(source_assets), _assets(assets)
    if set(sa) != set(FILES) or set(da) != set(FILES) | {"TIMESTAMPS.txt"}:
        raise ValueError("unexpected assets")
    hashes = dict(FILES, **{"TIMESTAMPS.txt": TIMESTAMP_HASH})
    for name, asset in da.items():
        if asset.get("state") != "uploaded" or asset.get("digest") != "sha256:" + hashes[name]:
            raise ValueError("destination asset mismatch")
    import tempfile
    for name, asset in sa.items():
        url = "https://git.etawen.dev/earthonion/yt_adskip_ps4/releases/download/v1.36/" + name
        if asset.get("browser_download_url") != url:
            raise ValueError("source download URL mismatch")
        with tempfile.TemporaryFile() as out:
            src.download(url, out)
            size = out.tell()
            out.seek(0)
            digest = hashlib.file_digest(out, "sha256").hexdigest()
        if digest != FILES[name] or size != asset.get("size") or size != da[name].get("size"):
            raise ValueError("source download mismatch")
    if da["TIMESTAMPS.txt"].get("size") != 587:
        raise ValueError("timestamp asset mismatch")
    core_source = {k: source[k] for k in ("id", "tag_name", "name", "body", "draft", "prerelease")}
    core_destination = {k: release[k] for k in ("id", "tag_name", "name", "draft", "prerelease")}
    core_destination["body"] = source["body"]
    marked = make_binding(core_source, core_destination, FILES, hashes, secret)
    if release.get("body") not in (source["body"], marked):
        raise ValueError("unexpected destination body")
    from repo_mirror.repository import find_tag_releases
    prospective = [dict(r, body=marked) if r.get('id') == 437729977 else r for r in releases]
    if find_tag_releases(None, TARGET, "v1.36", releases=prospective, marker=SOURCE) != [dict(release, body=marked)]:
        raise ValueError("production selection mismatch")
    if _stable(_read(src, dst)) != _stable(before):
        raise ValueError("state changed before write")
    if release["body"] == marked:
        return "already bound"
    dst.request("PATCH", RELEASE, {"body": marked})
    immediate = dst.request("GET", RELEASE)
    expected_immediate = dict(release, body=marked)
    immediate_without_updated = dict(immediate)
    expected_immediate.pop("updated_at", None)
    immediate_without_updated.pop("updated_at", None)
    if _stable(immediate_without_updated) != _stable(expected_immediate):
        raise ValueError("immediate readback failed; manual inspection required")
    after = _read(src, dst)
    old_release, new_release = dict(release), dict(after[3])
    old_release["body"] = marked
    # GitHub updates this field as a consequence of PATCH. All other release
    # fields and all assets must remain unchanged.
    old_release.pop("updated_at", None); new_release.pop("updated_at", None)
    if _stable(old_release) != _stable(new_release) or _stable(before[:3]) != _stable(after[:3]) or _stable(before[4]) != _stable(after[4]):
        raise ValueError("post-write verification failed; manual inspection required")
    from repo_mirror.repository import find_tag_releases
    import os
    # Verify with the actual production reader and the same process secret.
    if os.environ.get("MIRROR_TIMESTAMP_SECRET") != secret:
        raise ValueError("signing secret environment mismatch")
    if find_tag_releases(None, TARGET, "v1.36", releases=after[5], marker=SOURCE) != [after[3]]:
        raise ValueError("signature verification failed")
    return "bound"


def main():
    import os
    from urllib.request import Request
    from repo_mirror.github_api import API, AppTokenProvider

    if os.environ.get("DESTINATION_OWNER") != "manoma080":
        raise ValueError("destination owner mismatch")
    class SinglePatchAPI(API):
        def request(self, method, url, data=None, **kwargs):
            if method == "GET":
                return super().request(method, url, data, **kwargs)
            if method != "PATCH" or url != RELEASE or set(data or {}) != {"body"}:
                raise ValueError("refusing unexpected write")
            req = Request(url, method="PATCH", data=json.dumps(data).encode(), headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json", "Content-Type": "application/json", "User-Agent": "repo-mirror"})
            # Never retry an ambiguous PATCH without a fresh validation.
            with self.opener.open(req, timeout=60) as response:
                return json.load(response)
    token = AppTokenProvider(os.environ.get("MIRROR_APP_ID", ""), os.environ.pop("MIRROR_APP_PRIVATE_KEY", ""), "manoma080", "personal")
    print(reconcile(API(), SinglePatchAPI(token), os.environ.get("MIRROR_TIMESTAMP_SECRET", "")))


if __name__ == "__main__":
    main()
