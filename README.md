# Repository mirror

This project copies public GitHub, Forgejo and GitLab repositories into GitHub repositories (private by default), and supports copying GitHub and public Forgejo and GitLab repositories to a local Forgejo host. The original repository is the **source**. The copy is the **mirror**. Forgejo is a self-hosted Git server.

## What it copies

| Item | What happens |
| --- | --- |
| Schedule | Runs every 3 hours (UTC). You can also start it by hand in **Actions → Mirror repositories → Run workflow**. |
| Branches and tags | Copied, including rewritten history. Branches and tags deleted at the source are deleted in the mirror. |
| Releases | A new mirror gets the source release history visible to its source credentials, oldest first. Later runs compare source and destination lists and sync missing releases, the first and latest source releases, and any release still pending or failed. Other existing releases are not rechecked. |
| Release files | Missing files are added. Files with the same name but different bytes are replaced. Files removed at the source stay in the mirror. |
| New mirrors | Created as **private** repositories with GitHub Actions turned off. You can make them public later. |
| Stars | Not copied. |

**Use a dedicated mirror account or organization.** The workflow can overwrite and delete branches and tags in the mirrors.

## GitHub and Forgejo destinations

Both paths use the shared Git ref reconciliation and release orchestration, with provider-specific API and Git adapters. This is code-level support, not a guarantee that every deployment or transfer has been live-verified.

| | GitHub destination | Local Forgejo destination |
| --- | --- | --- |
| Sources and execution | Public GitHub, Forgejo or GitLab sources; GitHub Actions, scheduled or manual; up to sixteen concurrent repositories. | Any configured GitHub source, plus public Forgejo and GitLab sources; local runner with systemd/cron templates, not automatically installed or enabled; four concurrent processes by default. The scheduled manifest is a restricted subset, not the GitHub config. |
| Git refs and protection | Both reconcile branches and tags, including force updates and deletion. A detected wipe triggers a private, verified archive before updating; the archive contains Git refs and reachable objects only, not releases, assets or LFS. Detection is conditional, not a general backup. | Same shared wipe policy; destination must be private, have the expected source marker and have Actions disabled. |
| Releases and assets | Both initialize with source releases visible to the source credentials; later runs select the first and latest, missing, visible draft, pending and failed releases. Changes to other older releases can be missed. Destination-only releases and ordinary destination-only assets are retained; selected assets with changed bytes can be replaced, and mirror-managed cache assets can be deleted or replaced. | Same shared policy and limitations. Synchronization is not atomic. |
| Latest | Promotes the source latest after selected releases succeed. | Cannot force latest: checks what Forgejo reports and fails if it cannot match the requested latest. |
| Metadata and files | Source release timestamps are not copied. Files must be under 2 GiB. | Forgejo may represent an empty title as its tag and an empty body as one ASCII space; this is not lossless. Uploads pass the source MIME type when available, otherwise `application/octet-stream`. Forgejo often lacks destination digests, so comparison can require downloading bytes; see [Checking files](#checking-files). |

Forgejo sources are read anonymously for both destination paths: only public repositories, refs and published releases accessible without authentication can be mirrored. Private repositories and draft releases are not visible anonymously, and authenticated private Forgejo source support is not implemented. Neither the GitHub token nor the destination Forgejo token is reused for Forgejo source access. For example, a public source URL is `https://forge.example/owner/repo`.

The Forgejo path uses [`config-forgejo.json`](config-forgejo.json), independently of GitHub Actions' `config.json`. It uses the same empty-body comparison rules as GitHub, including the intentional-one-space ambiguity; see [Releases in detail](#releases-in-detail). Its host setup, restricted manifest, authentication, retry journals, safety gates and live-test limitations are documented in [scheduler/README.md](scheduler/README.md). In particular, changed-asset transfers were tested with mocks but still need live verification.

### Shared-code impact on GitHub

The shared release path also changes GitHub behavior: it reads each selected source release by ID for a baseline before processing and, on the normal path, again before finalization, failing on raw same-side metadata drift rather than treating empty and one-space bodies as equivalent. The read-only convergence path performs its own verification instead of normal finalization. Draft finalization treats one space as logically empty, including on GitHub; published GitHub finalization sends the raw source body. Destination release snapshot caching also updates its draft alias when a release changes identity. These are potential effects of shared code, not an attribution of any particular run failure to the Forgejo work. See [`repo_mirror/repository.py`](repo_mirror/repository.py) and the provider adapters in [`repo_mirror/`](repo_mirror/).

## Choose sources for a manual run

Enter exact repository URLs in the **source_urls** box, separated by commas. Leave it empty to mirror everything in the config.

- URLs must be exact credential-free HTTPS repository links; unknown URLs can be mirrored once or added to config.
- Malformed URLs and destination name collisions stop the run.
- The run first looks up every source and checks for name clashes. If any lookup fails, the run stops before copying.
- Scheduled runs always mirror everything.

### Add to config checkbox

- **Ticked** with URLs: the URLs are written into `config.json` and committed to the default branch first. The test and mirror jobs then run on that commit, and only the typed repositories are mirrored. Future scheduled runs include them.
- **Unticked** with URLs: a one-time mirror. The repositories are copied now but are not saved to `config.json`, so scheduled runs skip them. They do not need to be in the config, but the same host, owner and name rules apply.
- **Empty** with add unticked: a regular full run of everything in the config. Add requires URLs.
- Repositories already in the config are not written twice. If nothing changes, no commit is made.
- Repository URLs on `github.com` are read as GitHub sources, and `gitlab.com` as GitLab. Other hosts default to Forgejo unless already configured with an explicit GitLab type. Unconfigured one-time GitLab URLs cannot be onboarded without durable pins; configure them explicitly instead.
- Terms: *config* means `config.json`, the list of sources the mirror reads. *One-time mirror* means a run that copies repositories without saving them for later runs.

### Destination and removal options

- **destination_name** optionally replaces `mirror-<owner>-<repo>` for exactly one URL. Names must be valid GitHub repository names and must not collide with another destination. A pre-existing repository is used only if its source marker matches; visibility is never changed implicitly. Manual config updates reject changing a pinned destination name; use the explicit reconciliation procedure instead.
- **visibility** defaults to unchanged, preserving saved settings; new destinations default to private. Select private or public to explicitly override a saved setting; public exposes mirrored contents. It applies only on creation, not to existing destinations. With add selected, settings are persisted per source repository for scheduled runs; otherwise they apply only to this run.
- **remove_from_config** requires one or more URLs, commits only the config removal and skips the entire mirror job, including journal writes. It does not delete repositories. It cannot be combined with add, custom name or public visibility. Individual URLs covered by a `repos: "all"` source cannot be removed this way; edit the configuration explicitly instead.

## Configure sources

Edit [`config.json`](config.json). Named configuration lists each source repository and its mirror explicitly:

```json
{
  "destinations": {"github": {"type": "github", "url": "https://github.com", "owner": "manoma080", "auth": "mirrorbot"}},
  "repositories": [
    {"source": {"url": "https://github.com/alice/project", "id": 123},
     "mirrors": {"github": {"name": "mirror-alice-project", "id": 456, "visibility": "private"}}}
  ]
}
```

A top-level `host_concurrency_limits` map caps how many repositories are mirrored at once from each source host; `default` applies to hosts without an entry, and omitting the map keeps one shared pool of sixteen:

```json
{"host_concurrency_limits": {"default": 16, "git.etawen.dev": 4}}
```

Hosts are lower-case names, optionally with a non-default port; limits count repositories in flight per source host, not per provider, and never exceed the runner's worker pool. Capping a host trades throughput there for predictable request rates — a host that answers HTTP 429 when several repositories are fetched at once mirrors its repositories in several waves instead of failing.

Exactly one destination profile and one mirror per repository are supported; extra profiles or mirrors are rejected. Explicit profiles have exactly `type`, `url`, `owner`, and `auth`; the example is a personal account, and the CLI requires `DESTINATION_OWNER` to match, the URL to be exactly `https://github.com`, and `MIRROR_APP_ID` and `MIRROR_APP_PRIVATE_KEY` for the `mirrorbot` alias. This applies to scheduled and manual runs; fixed `GH_TOKEN` is not accepted for this explicit alias. The alias identifies the configured credential source, not a secret or independent proof of app identity: verify the deployed secret values belong to Mirrorbot. The workflow uses those app secrets, while the Forgejo scheduler's separate Manobot helper authenticates GitHub source access. Legacy named profiles with `account_type` (`personal` or `organization`) remain supported with their previous behavior. Source URLs may point to GitHub, a public Forgejo instance or a public GitLab instance. GitLab repositories specifically require both source and destination ID pins before syncing; see [GitLab sources](#gitlab-sources). Mirror names default to `mirror-OWNER-REPO` and visibility defaults to private. For other sources, IDs are optional positive integers: a pinned source must return the specified ID, and a pinned destination must already exist with the specified ID and matching source marker; a missing destination is not created. The source clone URL check still applies. These checks are not atomic protection against a repository changing after a check. Manual add/remove retains the named format.

Legacy `destination_account_type` and `sources` configurations remain supported, including `repos: "all"` (owned public repositories, not stars), custom `prefix`, and per-repository `destinations` mappings using `source_id` and `destination_id`. Do not mix the formats.

## GitLab sources

These are code-level capabilities in this checkout, not a claim of a live deployment or end-to-end provider verification. Source access is anonymous and public-only by default, including releases and linked files; private projects are not supported. An optional read token raises gitlab.com's rate limits: set `MIRROR_GITLAB_TOKEN` for the workflow, or `--gitlab-token-file` (owner-only, single line) on the local runner. It applies only to `https://gitlab.com`; self-hosted GitLab origins stay anonymous. The token is sent as `PRIVATE-TOKEN` to that API origin only and never to asset downloads or redirect targets. Use a credential-free HTTPS origin and project URL. Nested group paths are supported; `gitlab.com` is recognized automatically, but a self-hosted GitLab source must be explicitly typed `gitlab`, otherwise it is treated as Forgejo. Obtain IDs from trusted provider records, independently verify the source, destination and ownership, and keep those pins and the source marker stable. GitLab IDs are scoped to their instance, not globally. Pin checks and operations are separate requests, not atomic protection against replacement or changes between checks.

### Explicit project: GitHub runner

Replace all example names and IDs with independently verified real values. This named configuration requires an already existing destination with the pinned ID and matching source marker; it does not create a missing pinned destination. Its profile and credentials follow [Configure sources](#configure-sources).

<!-- gitlab-example: github-project -->
```json
{
  "destinations": {"github": {"type": "github", "url": "https://github.com", "owner": "mirror-owner", "auth": "mirrorbot"}},
  "repositories": [
    {"source": {"type": "gitlab", "url": "https://gitlab.com/team/sub/project", "id": 42},
     "mirrors": {"github": {"name": "mirror-project", "id": 99}}}
  ]
}
```

### Direct group: GitHub runner

Use the legacy `sources` format, not a named `repositories` configuration; do not mix formats. This discovers public projects owned directly by `team/sub`, excluding shared projects and subgroups. It is not a recursive group mirror. Newly discovered projects require the durable management configuration store (`MIRROR_MANAGEMENT_REPOSITORY` and `MIRROR_MANAGEMENT_TOKEN`; the workflow sets these), complete discovery and an authoritative checkout matching that store. The runner creates a destination, then conditionally publishes source/destination ID bindings and reads them back before mirroring. It refuses to adopt an existing unpinned destination. A group source is a continuing discovery rule, not an immutable pin of the group itself on this runner.

<!-- gitlab-example: github-group -->
```json
{
  "destination_account_type": "personal",
  "sources": [
    {"type": "gitlab", "base_url": "https://gitlab.com", "owner": "team/sub", "repos": "all"}
  ]
}
```

### Explicit project and direct group: Forgejo scheduler

These are alternative `config-forgejo.json` manifests. The first pins an existing destination; the second starts with no projects and pins the positive GitLab group ID. Set `--asset-max-bytes` to a positive decimal number no greater than 2,147,483,647 on the scheduler/worker for GitLab, even when no release links are expected. Group onboarding additionally needs the authoritative management `--config-repo` and the normal scheduler credentials, journal and secret; see [scheduler setup](scheduler/README.md). The scheduler publishes concrete source and destination pins before starting workers. Its legacy `sources`/`repos: "all"` form is not supported for GitLab groups. The generated `gitlab-` plus 24 hexadecimal characters namespace is reserved for group bindings; removing a group does not authorize continued syncing of its generated entries.

<!-- gitlab-example: forgejo-project -->
```json
{
  "destinations": {"local": {}},
  "repositories": [
    {"source": {"type": "gitlab", "url": "https://gitlab.com/team/sub/project", "id": 42},
     "mirrors": {"local": {"name": "mirror-project", "id": 99}}}
  ]
}
```

<!-- gitlab-example: forgejo-group -->
```json
{
  "destinations": {"local": {}},
  "repositories": [],
  "groups": [
    {"type": "gitlab", "base_url": "https://gitlab.com", "owner": "team/sub", "id": 7, "repos": "all"}
  ]
}
```

Group discovery checks public direct projects, not shared projects or descendants; the Forgejo path verifies the configured public group's ID and path as well. On either runner, creation, pin publication and sync are not one transaction. A failed or uncertain publication can leave remote repositories, possibly with Actions settings needing attention. Stop and inspect both the authoritative config and all attempted destinations, their IDs, ownership, markers, visibility and Actions settings. Independently reconcile the binding, or remove an unwanted partial repository only after verifying it, before retrying. Never blindly adopt a marker or delete a repository. Concurrent actors can still change state between checks.

### GitLab releases, links and network boundaries

GitLab releases are identified by instance, pinned project ID and exact tag. Latest is selected by greatest timezone-aware `released_at`; missing or invalid times, or a tie for latest, fail rather than guess. GitHub promotes the selected release after successful reconciliation; Forgejo checks its own latest and fails if it differs, because it cannot force parity. GitLab releases are represented as published, non-prerelease releases. Automatic GitLab source archives are excluded, as are Git LFS objects and submodule repositories. Git refs retain pointers, not those external contents.

Explicit GitLab release asset **links** are downloaded anonymously and uploaded as files if their measured size is at most the cap: 2,147,483,647 bytes for GitHub, or the configured Forgejo cap. A verified larger link is preserved as its original link in release notes rather than uploaded; this does not guarantee future availability or immutable bytes. An existing same-name destination asset can make oversized-link reconciliation fail. Bad metadata, unsafe URLs, download errors and inconsistent content fail the release rather than silently preserving an unverified link. The cap is not a total storage limit; transfers and comparisons can use temporary disk and repeat downloads.

Original link URLs must be credential-free, without query strings or fragments, and pass the URL/path checks; generated archive paths are rejected. HTTP and anonymous FTP are permitted but plaintext; prefer HTTPS. The downloader rejects non-public IP literals and DNS answers, connects to validated addresses, checks redirects and prevents HTTPS downgrade. Redirects may contain signed queries but are not published as the preserved link; never put secrets in source URLs or release text. API reads are restricted to the configured HTTPS origin and no destination credential is sent to GitLab or its asset servers. These checks reduce SSRF and credential leakage risks, but untrusted sources still require network isolation and operator review; they are not a sandbox or a guarantee against all races.

### Explicit relocation bindings

A reviewed relocation mapping preserves the **original** source marker and recovery target while using current names for Git and API operations. It does not verify the historical association for you. Before adding one, independently establish the old-to-current source and destination associations and IDs from trusted historical records; a redirect, matching current ID, or destination description alone is not that proof. GitHub IDs are scoped to GitHub; Forgejo IDs are scoped to the Forgejo instance. Treat these mappings as security-sensitive configuration and retain the original binding for as long as its signed drafts, pending releases, manifests or journals can exist. Never replace the original binding with an intermediate name on a later move.

For a GitHub destination, use an explicit repository list and a mapping such as:

```json
{"source":{"url":"https://github.com/new-owner/new-repo", "id":123},
 "mirrors":{"github":{"name":"mirror-new", "id":456,
   "legacy":{"source_owner":"old-owner", "source_name":"old-repo", "destination_name":"mirror-old",
             "signature":"<64-lowercase-hex-characters>"}}}}
```

For the local Forgejo destination, the manifest continues to accept ordinary URL strings; an explicitly verified relocation may instead use:

```json
{"url":"https://forge.example/new-owner/new-repo", "source_id":123, "destination_id":456,
 "destination_name":"mirror-new", "legacy_url":"https://forge.example/old-owner/old-repo",
 "legacy_destination_name":"mirror-old", "signature":"<64-lowercase-hex-characters>"}
```

After independently verifying the history, prepare a claim JSON file with exactly these fields (the values below correspond to the GitHub example and destination owner `dest`):

```json
{"source_provider":"github", "source_origin":"https://github.com",
 "old_source_owner":"old-owner", "old_source_name":"old-repo",
 "current_source_owner":"new-owner", "current_source_name":"new-repo", "source_id":123,
 "destination_origin":"https://github.com", "destination_owner":"dest",
 "old_destination_name":"mirror-old", "current_destination_name":"mirror-new", "destination_id":456}
```

For the GitHub destination, run `python -m repo_mirror.migration_attestation --claim claim.json --i-verified-history` with that runner's existing `MIRROR_TIMESTAMP_SECRET` in the environment; put the printed signature in `legacy.signature`.

For the Forgejo manifest example above, with destination owner `dest` and scheduler `--port 3000`, the corresponding claim is:

```json
{"source_provider":"forgejo", "source_origin":"https://forge.example",
 "old_source_owner":"old-owner", "old_source_name":"old-repo",
 "current_source_owner":"new-owner", "current_source_name":"new-repo", "source_id":123,
 "destination_origin":"http://127.0.0.1:3000", "destination_owner":"dest",
 "old_destination_name":"mirror-old", "current_destination_name":"mirror-new", "destination_id":456}
```

Run `python -m repo_mirror.migration_attestation --claim claim.json --i-verified-history --secret-file /path/to/timestamp-secret`, using the same owner-only recovery secret file as the Forgejo runner, and put the output in the manifest entry's `signature`. The Forgejo signer derives the source origin and names exactly from the URLs; it uses `github` only when the origin is `https://github.com` (case-insensitive), otherwise `forgejo`. The destination origin includes the configured local port and the destination owner is the scheduler's `--owner`. IDs must be positive JSON integers. Claim files must contain exactly the illustrated fields, with values matching the runner's inputs.

GitHub and Forgejo use their respective existing recovery signing secrets; do not assume their secrets or signatures are interchangeable. Each attestation secret must be at least 32 UTF-8 bytes and generated with a cryptographically secure random generator (for example, `python -c 'import secrets; print(secrets.token_hex(32))'`). Length alone does not guarantee entropy. If an existing secret is weak, rotate it with appropriate recovery-state planning; previous attestations must be re-signed. Keep both secrets stable and protected, and do not put secret values in claims, manifests, commands or logs. The signer does not inspect providers or verify evidence: the flag records an operator decision, not proof of history. For manual claims, any change to a claim field or signing secret requires a new reviewed signature; protect the claim file. The Forgejo worker may also sign an in-memory candidate automatically under the prior-pinned trust policy below; it does not publish that candidate. Signature verification does not replace live ID checks or protect against a compromised signing key. On the Forgejo path, any invalid or missing structured-entry signature fails the **entire manifest** before any workers start, including ordinary URL entries; correct and review the manifest before retrying.

The old and current source must be on the same forge origin. The approved automatic trust policy treats the previously configured source and destination ID pins and their original binding as trusted; consistent metadata from the configured forge may identify a new source name, and an existing signed legacy binding must verify. Forgejo can construct and sign an in-memory candidate using the runner's recovery secret, preserving the original recovery identity; signing authorizes that binding under this policy and is not independent proof of history. Unpinned sources, changed IDs, inaccessible URLs, inconsistent metadata, or invalid signatures fail closed. Legacy mappings with `repos: "all"` are rejected: configure that repository explicitly.

A detected move alone is **not** a completed migration. The worker first syncs Git refs, releases and settings into the existing pinned destination, then reports the relocation; the rename and configuration publication happen afterwards as a separate transaction (see below). Without a durable management store the batch reports the relocation and renames nothing. Either way a failure can leave a partial sync: inspect and reconcile before retrying. Rename and config publication are not atomic with anything else, so drain cooperating writers, preserve signing keys and journals, and read back every step. Forgejo's installed launcher must support the structured manifest before it is used. Real provider migration and recovery must be validated separately from unit tests.

### Post-batch migration transaction

The GitHub batch can finish a detected move itself when it has a durable management store. It records the intent in `migrations.json` — a new file in the management repository, separate from `failures.json` — through a Contents API conditional write with a readback, and only then touches the destination:

`prepared` (intent recorded) → `synced` → `renamed` → `configured` (configuration published, record removed).

The configured destination name is authoritative: a name that is merely `mirror-OWNER-REPO` for the old source follows the moved source, while a custom name is preserved and the destination is not renamed. The configuration update is a byte-exact conditional edit of `config.json` that replaces only the migrating entry (every other entry keeps its bytes) and adds the original `legacy` binding; a conflicting writer stops the transaction instead of overwriting it. Every resumed phase re-reads live repository IDs, so a replaced pin, a name collision, or an old-name redirect that resolves to another repository fails closed for review.

The workflow enables this by setting `MIRROR_MANAGEMENT_REPOSITORY` (`${{ github.repository }}`) and `MIRROR_MANAGEMENT_TOKEN` (`${{ github.token }}`, contents write), and the management repository's default branch must be `main`. Without the store the batch reports the relocation and renames nothing. Rename and publication are still not atomic with respect to non-cooperating writers; the ID rechecks and the failing close on a conditional-write conflict are the only protection.

The Forgejo scheduler performs the same transaction with its own durable store: `migrations.json` inside `--journal-dir`, authenticated with the runner's recovery secret, plus conditional publication of `config-forgejo.json`. It requires `--config-repo OWNER/NAME` (and `--config-account-type` when needed); without it, a pending intent makes the run exit instead of renaming. Because a Forgejo source name that has been reused does not redirect, a rename there usually fails closed rather than resolving automatically. Install the launcher after substituting `@CONFIG_REPO@`; a literal placeholder fails every run by design.

Both runners share one transaction engine: `repo_mirror/migrations.py` owns the phase protocol, the journal writes, the pinned identity checks and the byte-exact config rewrite; each provider supplies a thin policy adapter (`github_migration.py` for the named GitHub config, `forgejo_migration.py` for the Forgejo manifest). Change the protocol in the shared engine, not in a provider.

## Set up the GitHub App

The mirror uses a [GitHub App](https://docs.github.com/apps) to act on your behalf. Install it on the account or organization that owns the mirrors, with access to all repositories, including new ones.

| Permission | Level | Why |
| --- | --- | --- |
| Contents | Read and write | Copy branches, tags and releases. |
| Workflows | Read and write | Copy repositories that contain workflow files. |
| Administration | Read and write | Create repositories and turn off their Actions. |

In the management repository, go to **Settings → Secrets and variables → Actions** and add:

| Name | Type | Value |
| --- | --- | --- |
| `MIRROR_APP_ID` | Secret | The app's ID. |
| `MIRROR_APP_PRIVATE_KEY` | Secret | The app's full private key. |
| `MIRROR_OWNER` | Variable | The account or organization that owns the mirrors. |

Never put keys or tokens in the repository's files. If GitHub asks you to approve a permission change, approve it on the app installation.

### How the app login works

- The mirror finds the app's installation for `MIRROR_OWNER` and checks the owner name.
- It gets a short-lived **installation token** (a temporary login). It renews the token when fewer than five minutes remain, including during long runs.
- The private key stays inside the mirror process. It is passed to OpenSSL (the encryption tool) through a pipe, never in command arguments, logs or files. OpenSSL must be installed.
- Up to eight repositories are mirrored concurrently. Failure journal updates and token renewal are synchronized within each run; separate runs must not share the same failure file.
- API clients in the same process share per-host cooldowns after HTTP 429 or a rate-limited HTTP 403. API retry waits are limited to 60 seconds; longer server-requested delays fail rather than retry early. Ordinary 403s are not retried. Existing PUT, PATCH and DELETE retry behavior remains unchanged; POST is not automatically retried. This coordination does not cover separate runs, Git operations, authentication requests or asset transfers.
- A transfer already in progress keeps its old token. If it fails, that repository is reported as failed and retried on a later run.

The token is also sent with GitHub API read requests (to `api.github.com` only, on port 443). This reduces rate-limit errors. Forgejo, GitLab and public file downloads never get the token, and redirects to other sites never get it either.

## Logs and errors

Progress is printed to the run log, which is public in this management repository. Repository logs are buffered in temporary files and printed as complete groups when each repository finishes; groups can appear in completion order rather than configuration order.

- Logs show source owner and repository names, release tags and file names, with a counter for each.
- Anything that looks like a secret is hidden. This is a best effort, not a guarantee. Do not put secrets in names, tags or file names.
- URLs, destination IDs, credentials and raw error text are not logged.
- Time figures show how long each step took. Steps can overlap, so do not add them together.
- A file-count number means upload attempts, not verified results.

When a web request fails, the log shows the HTTP status code and what operation failed (for example "source release list"). It does not show the URL, the response body, or signed download links. A 403 error does not always mean rate limiting, so check the cause.

## Releases in detail

- A release with no files is valid. Ordinary uploaded files must be under **2 GiB**; GitLab links follow the separate policy above.
- **Empty release notes:** Exactly one ASCII space (U+0020) is treated as logically empty in release-body comparisons and draft finalization; an intentional one-space source therefore cannot be distinguished from empty in those paths. Other whitespace, including two or more spaces, tabs and newlines, remains meaningful and is preserved. Source drift checks compare raw bodies, not normalized ones. When finalizing a published GitHub release, the mirror sends the source body unchanged; comparisons may still treat one space and empty as equivalent. Forgejo uses the representation described below.
- **Forgejo destination tradeoff:** Forgejo's release PATCH cannot reliably clear nonempty notes with an empty body. The workaround writes exactly one ASCII space for a truly empty source body, so the persisted Forgejo body is not byte-for-byte identical, even though it will typically appear empty. Pending markers are temporary; recovery after interruption requires a valid signed marker and compatible source and transaction state. Completed published releases should have the pending marker removed; completed drafts have the separate identity-marker exception below. Reconciliation is not atomic, and failures may require manual review.
- **Drafts:** Forgejo source drafts are not visible to anonymous source reads and cannot be copied. For visible drafts from other sources, GitHub may return a synthetic `untagged-` tag for a draft. Source drafts with such tags are rejected rather than copied as real tags (even a legitimate draft tag with that prefix is conservatively rejected). For supported source tags, completed destination drafts retain a visible signed identity marker in their body until publication, so reruns can identify and publish the same release. Pending drafts use an ID-bound v3 recovery marker. Keep `MIRROR_TIMESTAMP_SECRET` stable: changing it, removing/editing a marker, or using older unmarked destination drafts requires manual reconciliation. Names and synthetic destination tags are not identities. If a create succeeds but its ID-bound marker cannot be saved, manual reconciliation can also be necessary.
- **Latest release:** for GitHub and Forgejo sources, `/releases/latest` picks the release to mark as latest on GitHub; GitLab instead selects by `released_at` as described above, even if it is older than the usual window. The GitHub mirror promotes it only after all selected releases are done. For a Forgejo destination, the mirror checks the destination latest rather than promoting it. A failed promotion is retried next run. Pre-releases and drafts are never promoted.
- **404 from `/releases/latest`:** no release is promoted. The repository may be private, or the Forgejo server may be too old. Other errors fail the repository.
- **Order:** releases are sorted by publish time, then creation time. If a time is missing or invalid, the source's list order is used. GitHub sets its own release times; the source's times are not copied.

### Release markers (how the mirror resumes)

While a release is being copied, its notes get a `[repo-mirror:pending:v3]` marker with transaction data. The title stays the same until the copy finishes.

- If a run stops, the next run can resume the marked release, even outside the usual window, if the signed marker is valid and the source and transaction state remain compatible.
- Markers are signed with a secret (see [Secret setup](#secret-setup)). The signature proves the marker came from this mirror.
- Do not write or copy these reserved markers in release notes or titles.
- Malformed or conflicting markers, and changed source file lists, need manual review.
- After the copy, the source's title and notes are restored subject to the empty-note policy above. Very long notes may leave no room for the marker.
- A new mirror stays in "initializing" mode until its full history is copied.
- Auto-pinning checks source and newly created destination identities before disabling Actions and checks the destination again afterward. These name-based API requests are not atomic: a repository can be replaced between the last identity check and the Actions update, so the update could affect a replacement. Post-update checks can detect a change but cannot undo an incorrect update; inspect affected repositories and Actions settings manually.
- Automatic pinning refuses any existing destination without both saved source and destination ID pins, even if its description has an initializing or completed mirror marker. Names and markers do not prove identity. A failed batch can leave newly created remote repositories while leaving config unchanged; Actions disabling may also have failed. Inspect each repository and its Actions settings manually. After independently verifying ownership and contents, remove an unwanted partial repository and retry, or follow your organization's manual reconciliation process to establish trustworthy ID-bound pins. Do not blindly delete repositories or adopt their markers. Identity checks reduce but cannot eliminate races: repositories can change after the last API check and before the configuration is written or used.

### Starter files

A source file in `starter` state is logged and skipped. It does not keep a release pending. If it later becomes a normal uploaded file, it is copied on a later run. Starter files already on the mirror must be cleaned up by hand.

## Asset transfer retries

Asset downloads and uploads retry transient transport errors, HTTP 429 and 5xx up to three times after the first attempt, with exponential backoff and jitter, respecting Retry-After. A Retry-After longer than 60 seconds stops the transfer without sleeping or retrying early. Partial downloads are discarded before retrying. After an ambiguous upload failure and on HTTP 422, the destination asset list is checked: one uploaded asset with matching downloaded bytes counts as success. A starter, duplicate, missing URL or byte mismatch stops recovery; retries never delete or overwrite an asset. Timestamp-manifest transfers use the same policy.

This is best-effort reconciliation, not an exactly-once guarantee. An upload may still be in flight or not yet visible when the list is checked, so a subsequent retry can race it. GitHub documents that a 502 may leave an empty starter asset and that an existing filename can produce 422; it does not guarantee atomic in-flight uniqueness or immediate listing consistency. Inspect and reconcile ambiguous assets manually if necessary. See [GitHub's upload asset documentation](https://docs.github.com/en/rest/releases/assets#upload-a-release-asset).

## Failed uploads

Failed uploads are saved in `failures.json` on the management branch. The next run checks that file.

- Entries use hashes of the source details, not names or secrets. A hash can sometimes be guessed, so do not treat it as secret.
- When an upload succeeds on retry, its entry is removed.
- Only attempted uploads appear in the file.

| Situation | What happens |
| --- | --- |
| A run is cancelled or cannot save the file | A valid release marker can let the next run resume if the source and transaction state remain compatible; otherwise manual review may be needed. |
| Someone changes the management branch during a run | Saving may fail instead of overwriting their change. Run again. |
| Branch protection blocks the workflow | Allow the workflow's GitHub token to push `failures.json`. |
| A source or marker is changed or removed | Old entries may need manual review. |

## Checking files

A retry checks files that are already uploaded and copies missing ones. On the general path, comparison uses the following evidence and falls back to bytes when needed. The Forgejo adapter has a separate authenticated GitHub digest shortcut described below.

1. **Published SHA-256 digest** (a fingerprint of the file's bytes). Used when both sites publish a valid digest, they match, and the sizes match. Forgejo often has no digest.
2. **Checksum file fallback.** If the source has exactly one `SHA256SUMS` or `SHA256SUMS.txt` (up to 1 MiB), its strictly formatted lines are used as digests. Bad lines, duplicate names, unsafe paths or several checksum files turn this off.
3. **Timestamp and size cache (heuristic).** Used only for files with a usable ID, timestamp and size, and only if a verified `REPO-MIRROR-MANIFEST.json` exists. See below.
4. **Byte comparison.** If the first three steps cannot decide, the file is downloaded and compared byte by byte.

For the Forgejo adapter, a valid SHA-256 digest from the authenticated GitHub source listing can avoid downloading the source asset: the mirror downloads and hashes the same-size destination asset and skips the source download if it matches. It then re-downloads and hashes the pinned destination asset during final verification and checks the source asset snapshot again. The timestamp-cache shortcut is disabled for this adapter; checksum-file fallback does not enable this digest shortcut.

Missing or bad digests are never treated as proof of a match. Equal sizes alone do not prove equal contents.

### Limits of the checks

- **Checksum files are a source claim, not proof.** A malicious source can publish bad files with matching hashes. Verify the source yourself if you do not trust it.
- **The file-list fingerprint** (used to guard pending releases) only includes stable fields: name, size, asset ID, state, published digest and checksum-file content. It ignores volatile fields such as download counts, timestamps, download URLs, uploader, labels and content type. A content change that keeps all stable fields the same can slip past it. The byte check before replacing a file covers that gap for files already on the mirror.
- **Timestamp and size cache (`REPO-MIRROR-MANIFEST.json`).** On the general path, the mirror stores each file's ID, timestamp, size and SHA-256 in a signed file named `REPO-MIRROR-MANIFEST.json`. Legacy `TIMESTAMPS.txt` files are not used as cache or managed by the mirror. A file is skipped only if all identity fields match and the mirror's stored hash matches the copy on GitHub. This is a **heuristic**: a source can change a file without changing its timestamp or size, so changes can be missed.
- If the source has the current manifest name or its `.repo-mirror-staging` name (or `MIRROR-REPO-MANIFEST.json`), or the existing current manifest was not made by this mirror, caching is disabled for that release. Source-owned `TIMESTAMPS.txt` and its staging name are copied and updated as ordinary source assets under the usual replacement safeguards. Existing legacy files are never treated as cache or renamed. A conflicting legacy extra can also require manual review during pending-release recovery.
- Updates write the new cache file, check it, delete the old one, then rename. This is not one atomic step. If a rename is interrupted, the verified staging file is kept for the next run. A stale or conflicting staging file needs manual cleanup and is never removed automatically.

## Failures and limits

| Situation | What happens |
| --- | --- |
| One repository fails | The other repositories still run. The workflow then reports failure. A failed release does not stop the next release in the same repository. |
| Source lookup fails | Known repositories with names that could clash with the failed source are skipped. Other names are tried. The run then fails. An empty prefix blocks all names. Fix the lookup or use different prefixes. |
| Bad config or duplicate mirror names | The run stops before copying anything. |
| Empty source repository | Nothing is pushed or deleted. |
| Branch protection blocks changes | The mirror may be partly updated. Check the failed run. |
| Source marker text removed from a mirror's description | The workflow stops rather than change the mirror. Keep the `[repo-mirror: ...]` text in the description. |
| Not enough disk space | The run fails. Copies need temporary space, sometimes two copies of a file. |
| [Git LFS](https://git-lfs.com) files | Large files stored outside normal Git history are not copied. |
| [Submodules](https://git-scm.com/book/en/v2/Git-Tools-Submodules) | Repositories linked inside another repository are not copied. |
| Other data | Issues, pull requests, settings and secrets are not copied. |

Only copy content you have permission to copy. Protect the management repository's default branch. Anyone who can change its workflow can change what runs with the app's permissions.

GitHub can delay or skip scheduled runs. It also turns off scheduled workflows in public repositories after 60 days without activity. If that happens, [re-enable the workflow](https://docs.github.com/actions/using-workflows/disabling-and-enabling-a-workflow).

## Secret setup

The mirror needs a long random secret to sign its markers and cache file.

1. Create one, for example with `openssl rand -hex 32`.
2. Save it as the management repository's Actions secret `MIRROR_TIMESTAMP_SECRET`.
3. Keep it private and backed up in a password manager. Do not store it in the repository.

The mirror stops at startup, before any network or repository work, if the secret is missing or empty. Changing or losing it makes existing markers impossible to verify, and those releases then need manual review.

Signed files that fail verification (old, tampered, or signed with a different key) are treated as unknown. They are not used and not deleted. To restore caching, check the unknown manifest (`REPO-MIRROR-MANIFEST.json`) and any conflicting staging file by hand, delete them only if they are not source files, and run again. Keep the same secret so an interrupted replacement can be recovered.

## Run it locally

This project uses [uv](https://docs.astral.sh/uv/) to manage Python and packages.

```sh
uv sync                                          # install dependencies and dev tools
uv run python -m unittest discover -s tests -v   # run the tests
uv run ruff check .                              # lint
uv run ruff format .                             # format
uv run ty check repo_mirror mirror.py            # type check
```

To mirror locally, set these environment variables:

- `DESTINATION_OWNER` — the account that owns the mirrors.
- Either `MIRROR_APP_ID` plus `MIRROR_APP_PRIVATE_KEY` (automatic token renewal, recommended), or `GH_TOKEN` (a fixed token that can expire during a long run). If both are set, the app credentials win.
- `MIRROR_TIMESTAMP_SECRET` (see [Secret setup](#secret-setup)).

Then run:

```sh
uv run python mirror.py --config config.json
```

A local run changes real GitHub repositories. Test with a separate destination first.

## Inactive sources

At the end of a run, repositories whose newest commit, tag or release is older than a threshold are listed as warnings. The threshold defaults to 365 days and can be changed with `--inactive-after-days` (`0` disables it). The check needs no extra API request: the newest committer or tagger date comes from the refs the run already fetched, and the newest release timestamp comes from the release list the run already reads. Only completed repositories are judged, so a failed mirror is never reported as inactive.

The GitHub workflow prints the list in its job log. The Forgejo runner reports each case through the worker protocol (`FORGEJO_INACTIVE`) and the scheduler re-emits it into the journald log as `Repository N: INACTIVE since <date> (<days> days)`, with the count appended to the batch summary.

Commit dates are author-supplied, so a repository pushed today with old commits still looks inactive; a release published on an old tag counts through its release timestamp. Treat the list as a hint for pruning configuration, not as proof.

## Wipe protection

Before updating a nonempty mirror, the repository workflow compares the old and new default-branch trees and the old default history with all captured source branches and tags. It treats the change as a wipe only if the default-branch file count drops by at least 75% relative to the old count **and** at least 75% of old commits are unreachable from all source branches and tags. Renamed or replaced files count toward the new total. Empty old trees do not trigger the heuristic. This is not a general backup: changes outside the default branch, retained commits, or smaller losses can evade it.

On a trigger, the workflow creates a private `<mirror>-wipe-<UTC timestamp>` repository, disables Actions, reads back both settings, then copies and verifies the captured branch and tag object IDs. It sets the archive's default branch to the old mirror default and verifies it through both the GitHub API and Git before updating the mirror. Archive creation or verification failure stops the update; a failed attempt may leave an empty or partial archive for manual inspection. The archive includes Git refs and reachable Git objects only, not release metadata, release assets, Git LFS objects, repository settings, or hidden refs. The GitHub App needs repository administration write permission to create private repositories and update their default branches, permission to disable Actions, and permission to push workflow files.

Destination ref updates and deletions use explicit leases and atomic pushes; concurrent new refs are left alone. Ref changes can still occur after verification, and default-branch settings cannot be updated atomically with Git pushes. A failed multi-stage update may therefore require inspection and a new run. Snapshot, detection, archive and push elapsed times are logged separately.

An **empty destination** whose fetched pack exceeds 1.5 GiB is instead populated in slices, because GitHub rejects any single push whose pack exceeds 2 GiB. Branch histories are pushed as successive fast-forward commits along the first-parent chain (5000 per slice, halved on a size rejection until one), then the remaining refs one at a time, then the usual verified reconcile. A `refs/repo-mirror/seed-in-progress` marker written after the first slice is removed once the destination verifies; while it exists, a later run may finish an interrupted seed if every destination ref is at or behind its snapshot ref. Seeding is not atomic: an interrupted seed leaves partial refs (the next run resumes), and wipe detection and leases do not apply to it. See [GitHub's 2 GiB push limit](https://docs.github.com/en/get-started/using-git/troubleshooting-the-2-gb-push-limit).

## Code layout

| Path | What it holds |
| --- | --- |
| `mirror.py` | Thin entry point. Keeps `python mirror.py` working. |
| `repo_mirror/cli.py` | Command-line options and the main flow. |
| `repo_mirror/repository.py` | Copies one repository: branches, tags, releases and files. |
| `repo_mirror/github_api.py` | HTTP calls, app login and token renewal. |
| `repo_mirror/sources.py` | Source discovery and mirror naming. |
| `repo_mirror/git_sync.py` | Git fetch and push. |
| `repo_mirror/journal.py` | The failure file (`failures.json`). |
| `repo_mirror/logging_utils.py` | Log lines, timers and redaction. |
| `repo_mirror/constants.py` | Shared limits and settings. |
| `repo_mirror/source_filter.py` | Picks sources for a manual run. |
| `repo_mirror/staleness.py` | Inactive-source threshold and reporting. |
| `tests/` | Unit tests. |

For the lint, format and type-check settings, see [`pyproject.toml`](pyproject.toml).
