# GitLab sources, relocations, and migrations

This page documents GitLab source behavior, explicit relocation bindings, and the post-batch migration transaction shared by both runners.

## GitLab sources

GitLab support is public-only and anonymous by default, including releases and asset links.
Private GitLab projects are not supported.

- **Rate-limit token:** `repo_mirror/repository.py` reads `MIRROR_GITLAB_TOKEN` from the environment (used when invoking `mirror.py` locally; `.github/workflows/mirror.yml` currently passes `MIRROR_GITLAB_TOKEN` only to the `configure` job, which runs `repo_mirror/adhoc.py`—and `adhoc.py` does not read it—not to the `mirror` job). On the Forgejo runner, `--gitlab-token-file` (owner-only, single line) is exported as `MIRROR_GITLAB_TOKEN` into the worker environment (and read by `gitlab_onboarding_forgejo.py`). `GitLabSourceAdapter` sends `PRIVATE-TOKEN` on API requests to `https://gitlab.com` only (never to self-hosted GitLab origins or cross-origin redirects), while `gitlab_link_headers` in `repo_mirror/repository.py` attaches `PRIVATE-TOKEN` to same-origin release asset link downloads for any configured `source["type"] == "gitlab"` host.
- **Host detection:** `https://gitlab.com` is recognized automatically. Self-hosted GitLab instances must set `"type": "gitlab"` explicitly; otherwise non-GitHub hosts default to Forgejo.
- **Identity pins:** On the **GitHub runner**, explicit GitLab entries require positive integer `source.id` **and** `mirrors.<profile>.id` pins before syncing, and unconfigured one-time GitLab URLs are rejected. On the **Forgejo runner**, explicit GitLab entries require `source.id` only (`mirrors.<profile>.id` is pinned on `add_to_config`), and manual dispatch supports unconfigured one-time `gitlab.com` URLs via runtime project-ID lookup. Find numeric IDs under **Settings → General** or via `GET /api/v4/projects/<url-encoded-path>` / `GET /api/v4/groups/<url-encoded-path>`. GitLab IDs are scoped to their instance, not globally.
- **Direct groups (`auto_discoveries`):** A GitLab `auto_discoveries` rule requires a positive integer `id` and discovers public projects owned directly by that group, excluding shared projects and subgroups. It is not a recursive group mirror.
- **Forgejo `--asset-max-bytes`:** Whenever a Forgejo manifest includes GitLab repositories, `groups`, or GitLab `auto_discoveries` rules, you must pass `--asset-max-bytes` with an integer from `1` through `2147483647`, even if no release links are expected.
- **Non-atomic onboarding:** Creating destinations, disabling Actions, publishing pins, and syncing are separate requests. A failed or uncertain onboarding run can leave newly created remote repositories without published pins; inspect both the authoritative config and every attempted destination before reconciling or removing verified partial repositories.

### GitLab releases, asset links, and network boundaries

- **Release identity and latest:** GitLab releases are identified by `(origin, project_id, exact tag)` and represented as published, non-prerelease releases. Latest is chosen by greatest timezone-aware `released_at`; missing or invalid timestamps, or a tie for latest, fail closed.
- **Excluded items:** Automatic GitLab source archives, Git LFS objects, and submodules are excluded.
- **Asset links:** Explicit release asset links are downloaded anonymously and uploaded if their measured size is at most the cap (`2,147,483,647` bytes on GitHub, or `--asset-max-bytes` on Forgejo). Verified oversized links are preserved as Markdown links in release notes instead of uploaded; an existing same-name destination asset can make oversized-link reconciliation fail. Invalid metadata, unsafe URLs, download errors, or inconsistent bytes fail the release.
- **URL and SSRF checks:** Original link URLs must be credential-free, without query strings or fragments, and cannot point to generated archive paths. `https`, `http`, and anonymous `ftp` are accepted (prefer `https`). The downloader rejects non-public IP literals and DNS answers, connects to the validated address, re-checks redirects, and blocks HTTPS downgrade. Redirects may carry signed queries and are never published as the preserved link. These checks reduce SSRF and credential-leakage risk, but untrusted sources still require network isolation and review.

## Explicit relocation bindings

A reviewed relocation binding preserves a mirror's **original** source marker and recovery target after an upstream rename or transfer while using the current names for Git and API calls.
Signatures authorize an operator decision; they do not prove history.
Independently verify old-to-current source and destination IDs from trusted records before signing, and keep the original binding for as long as signed drafts, pending releases, manifests, or journals exist.

### Configuration format

GitHub destination (`config.json`):

```json
{
  "source": {"url": "https://github.com/new-owner/new-repo", "id": 123},
  "mirrors": {
    "github": {
      "name": "mirror-new",
      "id": 456,
      "legacy": {
        "source_owner": "old-owner",
        "source_name": "old-repo",
        "destination_name": "mirror-old",
        "signature": "<64-lowercase-hex-characters>"
      }
    }
  }
}
```

Self-hosted Forgejo destination (named manifest uses `mirrors.<profile>.legacy` with `url`, `name`, and `signature`; legacy flat manifests accept the structured object below):

```json
{
  "url": "https://forge.example/new-owner/new-repo",
  "source_id": 123,
  "destination_id": 456,
  "destination_name": "mirror-new",
  "legacy_url": "https://forge.example/old-owner/old-repo",
  "legacy_destination_name": "mirror-old",
  "signature": "<64-lowercase-hex-characters>"
}
```

### Signing a claim

Prepare a JSON file with these exact 12 fields (example for GitHub destination owner `dest`):

```json
{
  "source_provider": "github",
  "source_origin": "https://github.com",
  "old_source_owner": "old-owner",
  "old_source_name": "old-repo",
  "current_source_owner": "new-owner",
  "current_source_name": "new-repo",
  "source_id": 123,
  "destination_origin": "https://github.com",
  "destination_owner": "dest",
  "old_destination_name": "mirror-old",
  "current_destination_name": "mirror-new",
  "destination_id": 456
}
```

- **GitHub runner:** Export `MIRROR_TIMESTAMP_SECRET` (at least 32 UTF-8 bytes) and run:
  `python -m repo_mirror.migration_attestation --claim claim.json --i-verified-history`
- **Forgejo runner:** Use `destination_origin` set to `http://127.0.0.1:<port>` (and `source_provider` set to `"github"` only for `https://github.com`, otherwise `"forgejo"`), then run:
  `python -m repo_mirror.migration_attestation --claim claim.json --i-verified-history --secret-file /path/to/recovery-secret`

GitHub and Forgejo signing secrets are not interchangeable.
On Forgejo, any missing or invalid signature in a structured entry fails the entire manifest before workers start.

## Post-batch migration transaction

When a pinned source answers under a new canonical name on the same forge origin with its unchanged numeric `source.id`, the worker syncs into the **existing** destination using the original recovery identity and reports the relocation.
Renaming the destination and publishing the updated configuration happen afterward in a separate journaled transaction:

`prepared` → `synced` → `renamed` → `configured` (record removed after config publication).

- **Shared engine:** `repo_mirror/migrations.py` owns the phase protocol, journal writes, pinned ID rechecks, and byte-exact configuration rewrite; `github_migration.py` and `forgejo_migration.py` supply provider adapters.
- **Destination naming:** A default derived name (`mirror-<old_owner>-<old_repo>` on GitHub; `<old_repo>` for standalone entries or `<prefix>-<old_repo>` for same-owner `discovered_by` renames on Forgejo) follows the moved source. Custom destination names are preserved and not renamed.
- **GitHub store:** Enabled via `MIRROR_MANAGEMENT_REPOSITORY` (set to `MIRROR_CONFIG_REPOSITORY` in `.github/workflows/mirror.yml`) and `MIRROR_MANAGEMENT_TOKEN` on `main`. Writes `migrations.json` and updates `config.json` with conditional blob-SHA writes and readback.
- **Forgejo store:** Uses `--journal-dir/migrations.json` (authenticated with the recovery secret) or `migrations.json` in the self-hosted Forgejo control repository, plus conditional publication of `config-forgejo.json` (`--config-repo`) or `config.json` (`--forgejo-config-repo`). Without an authoritative config store, pending intents stop the batch instead of renaming.
- **Failure handling:** Every resumed phase rechecks live source and destination IDs. Conflicts, reused names, or concurrent config edits fail closed for manual review. Rename and config publication remain separate, non-atomic steps.
