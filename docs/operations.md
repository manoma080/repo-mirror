# Operations reference

This page covers how `repo-mirror` copies releases, checks files, retries failures, protects against destructive wipes, handles tokens, and logs progress.

## Destination comparison

Both runners share the Git ref reconciliation and release orchestration engines, with provider-specific API and Git adapters.

| Area | GitHub destination | Self-hosted Forgejo destination |
| --- | --- | --- |
| Concurrency | Up to 16 concurrent repositories (`max_workers = 16`), further capped by `host_concurrency_limits`. | 4 concurrent worker processes by default (`--jobs`, 1–64), further capped by `host_concurrency_limits`. |
| Configuration files | `config.json`, `failures.json`, and `migrations.json` in `MIRROR_CONFIG_REPOSITORY` (`main`). | `config-forgejo.json` under `--config-repo` (GitHub), or `config.json`, `failures.json`, and `migrations.json` under `--forgejo-config-repo`/`--forgejo-config-id` (self-hosted Forgejo). |
| Git refs and wipes | Force-updates and deletes branches and tags; creates a private, verified wipe archive before updating when wipe detection triggers. | Same shared wipe policy; destination must be private (unless explicitly configured public), carry the source marker, and have Actions disabled. |
| Latest release | Promotes the source latest release after selected releases succeed. | Cannot force latest; checks what Forgejo reports and fails if it does not match. |
| Empty release notes | Sends raw source body on published finalization; treats one ASCII space as logically empty in comparisons and draft finalization. | Writes one ASCII space (U+0020) for an empty source body because Forgejo PATCH cannot reliably clear nonempty notes with an empty string. |

## Releases in detail

- **Initialization vs later runs:** A new mirror (or one whose description still carries `[repo-mirror:initializing:v1]`) copies all source releases visible to its credentials, oldest first. Later runs select only the first listed release, the source latest release, missing releases, visible drafts, pending releases, and previously failed releases. Changes to other older releases can be missed.
- **Order and timestamps:** During initialization only, releases are sorted by `published_at` (falling back to `created_at` when `published_at` is absent), or by reverse source list order if any timestamp is missing or invalid. Destinations set their own timestamps; source timestamps are not copied.
- **Asset size ceiling:** Ordinary uploaded files must be at most `2,147,483,647` bytes (2 GiB − 1 byte). A release with no files is valid.
- **Empty release notes:** Exactly one ASCII space (U+0020) is treated as logically empty in release-body comparisons and draft finalization, so an intentional single-space source cannot be distinguished from empty there. Other whitespace (two or more spaces, tabs, newlines) is preserved. Source drift checks compare raw bodies.
- **Drafts:** Anonymous Forgejo source reads cannot see drafts. For visible drafts from other sources, synthetic `untagged-` tags are conservatively rejected. Completed destination drafts keep a visible signed `[repo-mirror:draft-identity:v1]` marker in their body until publication so reruns match the same release.
- **Latest release:** For GitHub and Forgejo sources, `/releases/latest` selects the latest release; a 404 means no release is promoted, while other errors fail the repository. Pre-releases and drafts are never promoted. GitLab selects latest by `released_at` (see [docs/gitlab-sources.md](gitlab-sources.md)).
- **Shared-code behavior:** Each selected source release is read by ID before processing and again before finalization, failing on raw metadata drift. Destination release snapshot caching also updates its draft alias when a release changes identity.

### Release markers and resume

While a release is being copied, its notes carry a signed `[repo-mirror:pending:v3]` marker with transaction state.

- If a run stops mid-release, the next run resumes the marked release even outside the normal selection window, provided the signature is valid and source and transaction state still match.
- Markers are signed with `MIRROR_TIMESTAMP_SECRET` (or `--timestamp-secret-file` on Forgejo). Do not write reserved `[repo-mirror:pending:...]` or `[repo-mirror:draft-identity:v1]` markers in source release titles or notes.
- Malformed or conflicting markers, or changed source file lists, require manual review.
- New mirrors stay in `[repo-mirror:initializing:v1]` mode until the full initial release history finishes.
- Auto-pinning checks source and newly created destination identities before and after disabling Actions, and refuses any pre-existing destination without both saved source and destination ID pins. Because API calls are not atomic, a failed batch can leave newly created remote repositories without published config pins; inspect and reconcile them manually before retrying.

### Starter files

A source asset in `starter` state is logged and skipped without keeping the release pending; if it later finishes uploading at the source, a later run copies it.
Starter assets already present on the destination stop recovery and must be removed manually.

## Asset transfer retries

- **Downloads:** Transient transport errors, HTTP 429, and HTTP 5xx are retried up to three times with exponential backoff and jitter, honoring `Retry-After` up to 60 seconds (longer delays fail immediately). Partial downloads are discarded before retrying.
- **Uploads:** On HTTP 422 or transient transfer errors, the runner lists destination assets: a single uploaded asset with matching downloaded bytes counts as success, while starter, duplicate, missing-URL, or mismatched assets stop recovery without deleting or overwriting anything. Only direct HTTP 429 upload rejections are retried up to three times after reconciliation.
- **Not exactly-once:** An upload can still be in flight when the destination list is checked, so a retry can race it. Inspect ambiguous assets manually if needed.

## Failed uploads and retry journals

- **GitHub runner:** Failed uploads are saved as hashed entries in `failures.json` in the private control repository. The workflow fences the branch with `.failure-journal-in-progress` before mirroring, updates `failures.json` on completion, and uploads a recovery artifact if saving fails.
- **Forgejo scheduler:** Per-destination retry journals are stored in `--journal-dir` (scoped by port, owner, and recovery identity) and aggregated into `failures.json` when using `--forgejo-config-repo`.
- **Behavior:** Entries store hashes rather than raw names or secrets, though hashes of guessable inputs are not secret. Successful retries remove their entries. Concurrent edits to the control branch or branch protection blocking your GitHub App can stop journal writes.

## Checking files

When a release already has files on the destination, the runner checks them in order:

1. **Published SHA-256 digest:** Used when both sides publish a valid matching digest and equal size. Forgejo often omits digests.
2. **Checksum file fallback:** If the source has exactly one `SHA256SUM`, `SHA256SUMS`, `SHA256SUM.txt`, or `SHA256SUMS.txt` file (case-insensitive, up to 1 MiB), strictly formatted lines supply digests. Malformed lines, duplicate names, unsafe paths, or multiple checksum files disable this fallback. Checksum files are a source claim, not proof.
3. **Timestamp and size cache (`REPO-MIRROR-MANIFEST.json`):** When enabled (non-GitLab, non-pending releases where the source does not own the manifest filename), the runner stores a signed manifest of asset IDs, timestamps, sizes, and SHA-256 hashes on both GitHub and Forgejo destinations. This is a heuristic: a source that changes bytes without changing timestamp or size can be missed. Legacy `TIMESTAMPS.txt` files are treated as ordinary source assets, not cache. Updates write a staging file, verify it, delete the old manifest, and rename—this is not atomic, and stale or untrusted manifests require manual cleanup.
4. **Byte comparison:** If earlier steps cannot decide, the runner downloads and compares bytes. Equal sizes alone never prove equal content.

For GitHub sources on **both** runners, the authenticated GitHub source adapter (`trusted_listing`) bypasses timestamp-cache reads: when the listing provides a valid SHA-256 digest and equal size, the runner hashes the same-size destination file to avoid downloading the source asset, then re-verifies after updates (and still updates `REPO-MIRROR-MANIFEST.json` when records are written).

## Wipe protection and large-repository seeding

- **Wipe detection:** Before updating a nonempty mirror, the runner compares the old and new default-branch trees and checks old default commits against all captured source branches and tags. It triggers a wipe archive only when the default-branch file count drops by at least 75% **and** at least 75% of old commits are unreachable from all source branches and tags.
- **Wipe archive:** On a trigger, the runner creates a private `<mirror>-wipe-<UTC timestamp>` repository, disables Actions, copies and verifies the captured branch and tag object IDs, and sets its default branch before updating the mirror. Archive failure stops the update. The archive holds Git refs and reachable Git objects only—not releases, assets, LFS objects, settings, or hidden refs.
- **Leases:** Ref updates and deletions use explicit leases and atomic pushes; concurrent new refs are left alone. Default-branch updates are separate API calls and not atomic with Git pushes.
- **Large empty destinations:** If an empty destination's fetched pack exceeds 1,500 MiB, the runner seeds it in slices (5,000 first-parent commits per slice, halved on size rejection down to 1) to stay under GitHub's 2 GiB push limit, marking progress with `refs/repo-mirror/seed-in-progress` until final verification. Seeding is not atomic, and wipe detection does not apply during seeding.

## App authentication, rate limits, and logging

- **Token renewal:** In `.github/workflows/mirror.yml`, the `configure` and `mirror` jobs each mint a short-lived installation token via `./.github/actions/mirror-token`. Inside `mirror.py` (and `forgejo_sync` with `--app-id`), `AppTokenProvider` renews the token when fewer than 5 minutes remain, passing the private key to `openssl` over a pipe (never in arguments, files, or logs).
- **Rate-limit cooldowns:** API clients in the same process share per-host cooldowns on HTTP 429 or rate-limited HTTP 403, waiting up to 60 seconds (longer server delays fail immediately). Ordinary 403s and POST requests are not automatically retried.
- **Logs:** Per-repository logs are buffered in temporary files and printed in completion order. Logs show sanitized owner/repo labels, release tags, asset names, upload-attempt counts, and step timings (which may overlap). URLs, destination IDs, credentials, and raw error bodies are omitted; redaction is best effort, so never place secrets in names or tags.

## Secret setup

Generate `MIRROR_TIMESTAMP_SECRET` (or Forgejo's `--timestamp-secret-file`) with at least 32 random bytes (e.g. `openssl rand -hex 32`) and back it up outside Git.
Both runners refuse to start if the secret is missing or empty (and Forgejo/attestation checks require at least 32 UTF-8 bytes).
Rotating or losing the secret invalidates existing recovery markers and cached manifests, requiring manual reconciliation.

## Inactive sources

At the end of a run, completed repositories whose newest commit, tag, or release is at least 365 days old (configurable via `--inactive-after-days` when invoking `mirror.py` or `forgejo_sync.py` directly; `0` disables—`forgejo_schedule.py` and `scheduler/auto_update.py` do not accept or forward this flag) are printed as warnings.
No extra API calls are made; failed repositories are excluded.
When `ignore_releases` is enabled, the GitHub runner appends `(releases ignored; commit activity only)`; the Forgejo runner's `FORGEJO_INACTIVE` record has fixed fields (`url`, `since`, `idle_days`) without that note.

## Failures and limits

- A single failed repository or release does not stop other repositories or releases, but the batch exits non-zero.
- Bad configuration, duplicate mirror names, or failed `auto_discoveries` onboarding stop the run before copying.
- Empty source repositories push and delete nothing.
- Removing the source marker (`[repo-mirror: ...]` on GitHub, `repo-mirror-source: ...` on Forgejo) from a mirror's description stops updates to that mirror.
- Temporary disk space is required during transfers (sometimes two copies of an asset).

## Local development and code layout

This project uses [uv](https://docs.astral.sh/uv/) for Python dependencies:

```sh
uv sync                                          # install dependencies and dev tools
uv run python -m unittest discover -s tests -v   # run unit tests
uv run ruff check .                              # lint
uv run ruff format .                             # format
uv run ty check repo_mirror mirror.py            # type check
```

To run the GitHub mirror locally, export `DESTINATION_OWNER`, `MIRROR_TIMESTAMP_SECRET`, and either `MIRROR_APP_ID` + `MIRROR_APP_PRIVATE_KEY` (recommended) or `GH_TOKEN`, then run `uv run python mirror.py --config /path/to/config.json`. Test against a separate destination first.

| Path | Role |
| --- | --- |
| `mirror.py` | Thin CLI entry point. |
| `repo_mirror/cli.py` | GitHub runner CLI, worker pool, onboarding, and post-batch migrations. |
| `repo_mirror/repository.py` | Per-repository Git, release, asset, marker, and cache orchestration. |
| `repo_mirror/github_api.py` | HTTP client, GitHub App token renewal, rate-limit cooldowns, and asset transfers. |
| `repo_mirror/git_sync.py`, `snapshot_push.py`, `wipe_detection.py` | Git snapshots, lease-protected pushes, large-repo seeding, and wipe detection. |
| `repo_mirror/auto_discoveries_onboarding.py` | Fail-closed `auto_discoveries` onboarding, ID pinning, and GitHub deletion adoption. |
| `repo_mirror/migrations.py` | Shared post-batch relocation transaction engine. |
| `repo_mirror/forgejo_schedule.py`, `forgejo_sync.py` | Self-hosted Forgejo batch scheduler and per-repository worker. |
| `scheduler/auto_update.py`, `github_app.py`, `forgejo-actions.yml` | Standalone Forgejo launcher, GitHub App helper, and workflow template. |
| `tests/` | Unit test suite. |
