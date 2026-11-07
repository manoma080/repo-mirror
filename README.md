# Repository mirror

Copy public GitHub and Forgejo repositories to GitHub. A **source** is the original repository; a **mirror** is its copy.

## How it works

| Item | What happens |
| --- | --- |
| Schedule | Runs every three hours on the hour (UTC). You can also run it from **Actions → Mirror repositories → Run workflow**. |
| Branches and tags | Copies all branches and tags, including rewritten history. Deletes branches and tags from the mirror when they are deleted from the source. |
| Releases | For a new mirror, copies the full release history, oldest first by publication time, falling back to creation time. Later runs reconcile the first release returned by the source API, plus the source's latest full release and older releases with pending copies or recorded upload failures. GitHub creates its own source-code archives. |
| Release files | Adds missing files and replaces same-name files whose bytes differ. A file removed from the source is left in the mirror. |
| New mirrors | Creates them as private repositories and disables their GitHub Actions. You can make them public later. |
| Stars | Does not manage stars or Stars lists. |

**Use a dedicated mirror repository.** The workflow can overwrite its branches and tags and delete ones that don't exist in the source.

Release assets with matching names are also replaced when their contents differ, even if you manually added or modified the destination asset. Incomplete starter assets require manual cleanup before retrying.

## Run selected sources

For a manual run, enter comma-separated exact source repository URLs in **source_urls**, such as `https://github.com/alice/project-one, https://code.example.org/bob/project-two`. Leave it blank to mirror all configured repositories. URLs must match configured hosts, owners and discovered repository names exactly; unknown, duplicate or malformed URLs stop the run. All sources are discovered and destination-name collisions checked before selecting repositories; any discovery failure stops a filtered run before mirroring. Scheduled runs mirror all sources.

## Add sources

Edit [`config.json`](config.json). Each entry can copy selected repositories or all public repositories owned by a user.

| Setting | Meaning |
| --- | --- |
| `destination_account_type` | `personal` for a personal GitHub account, or `organization` for an organization. |
| `type` | `github` or `forgejo`. |
| `base_url` | The source site's HTTPS address. |
| `owner` | The source user or organization name. |
| `repos` | A list of repository names, or `"all"`. |
| `prefix` | Optional text placed before each mirror's name. The default is `mirror-OWNER-`. |

Example:

```json
{
  "destination_account_type": "personal",
  "sources": [
    {"type": "forgejo", "base_url": "https://code.example.org", "owner": "alice", "repos": ["project-one"]},
    {"type": "github", "base_url": "https://github.com", "owner": "bob", "repos": "all", "prefix": "bob-"}
  ]
}
```

The first entry creates `mirror-alice-project-one`. Use different prefixes if two sources would create the same name. `"all"` means repositories owned by that user, not repositories they starred.

## Set up the GitHub App

Install the app on the GitHub account or organization that will own the mirrors. Give it access to all mirrors, including new ones.

| Repository permission | Access | Why |
| --- | --- | --- |
| Contents | Read and write | Copy branches, tags and releases. |
| Workflows | Read and write | Copy repositories that contain GitHub Actions workflows. |
| Administration | Read and write | Create repositories and disable their Actions. |

In the management repository, add these under **Settings → Secrets and variables → Actions**:

| Name | Type | Value |
| --- | --- | --- |
| `MIRROR_APP_ID` | Secret | GitHub App ID. |
| `MIRROR_APP_PRIVATE_KEY` | Secret | The app's complete private key. |
| `MIRROR_OWNER` | Variable | GitHub account or organization that owns the mirrors. |

Do not add keys or tokens to the repository's files. After changing app permissions, approve the change on its installation if GitHub asks.

The mirror looks up the app installation for `MIRROR_OWNER`, checks the returned owner, and mints installation tokens as needed. It refreshes when fewer than five minutes remain, including during a long repository: each API request and each destination Git push checks the token again. The app private key is kept in the mirror process and passed to OpenSSL through a pipe, not in command arguments, logs or a file. OpenSSL must be installed. The job remains sequential, so the existing failure journal is not written concurrently by matrix jobs. A single transfer already in progress cannot have its credentials replaced; if it fails, that repository is reported as failed and pending releases/failed uploads can be retried on a later run. GitHub's runner time limit and other network or permission errors can still interrupt a run.

The workflow's installation token is also used for GitHub source API GET requests to the exact HTTPS `api.github.com` origin on port 443, including release and asset listings, to reduce unauthenticated API rate limiting. Forgejo APIs and public source asset downloads do not receive the token. Redirects to other origins do not receive it. GitHub may still reject requests because of permissions, primary or secondary rate limits, or other reasons; a 403 is not proof of rate limiting. When available, the log identifies the source or destination operation and reports a zero remaining rate limit without printing URLs or response bodies.

## Releases and retries

Progress is written to stderr and can be visible in this public management repository. Logs identify public source owner/repository names, release tags and asset names alongside ordinals. Labels with unsafe characters, credential-like patterns or known secrets are redacted; this is conservative, not a guarantee that every possible secret is recognizable. Do not put secrets in source names, tags or asset names. URLs, destination identifiers, credentials, and arbitrary error messages are not logged. Asset counts describe transfer attempts, not final release verification. Logs show comparison decisions and monotonic elapsed times for repository, release, asset, Git, discovery, preflight and network operations; network targets are only source or destination, not addresses. Runtime overhead is time outside repository processing, including discovery and preflight. Timings overlap and should not be added together.

Section headings separate source discovery, repository processing, releases, and the final summary without interrupting individual network timings. HTTP failures report a validated status and, when known, a fixed source/destination operation and method; source asset download failures also report their status. When journaled asset uploads fail, the repository summary includes the first available HTTP status, not necessarily every failure's status. Unavailable or invalid statuses and non-HTTP failures may provide only a generic error. Diagnostics omit URLs (including signed URLs), response bodies, arbitrary exception text and header values other than the explicit zero rate-limit indication; status alone does not establish the cause.

A release with no uploaded files is valid. Each uploaded file must be smaller than 2 GiB.

The source's `/releases/latest` chooses the full release to mark latest on GitHub, even when it is older than the usual one-release window. The mirror explicitly prevents other new releases from being promoted and promotes the selected full release only after all selected releases finish. A failed promotion is attempted again on the next run. Prereleases and drafts are not promoted. If the endpoint returns 404, no release is explicitly promoted; GitHub may still display an existing full release as latest, and the mirror does not remove it. Other endpoint errors fail the repository. A 404 can also mean an inaccessible repository or an older Forgejo server without this endpoint, so it cannot prove that no full release exists. Forgejo's current API documents this endpoint as the most recent non-draft, non-prerelease by creation time. Source changes during a run and concurrent runs can temporarily produce a stale selection; rerun after the source settles. Initialization orders releases by `published_at` when present, otherwise `created_at`. If any chosen timestamp is missing or unusable (including lacking a timezone), it falls back to reversing the source API's list order. GitHub assigns its own release timestamps; source timestamps are not copied.

While a release is being copied or updated, its existing notes temporarily receive a `[repo-mirror:pending:v3]` marker and transaction data; its title is unchanged until the copy finishes. Older v1/v2 name markers remain supported for recovery. If a run stops, a later run can resume a marked release even outside the usual selection window, as long as it still exists at the source. Do not edit or use these reserved markers in release notes or titles. Malformed or conflicting markers and changed source manifests require manual review. The final update restores the source title and notes, including null notes. Very large notes may not leave room for the marker. A new mirror stays in initialization mode until the full history completes.

Source assets in `starter` state are logged and skipped without download or upload-failure journal entries; they do not by themselves keep a release pending or block initialization. A skipped starter remains skipped throughout that transaction, including when its state or metadata changes or its stable ID retains identity across a rename. Such changes deliberately do not invalidate the otherwise unchanged active-asset manifest. If it later becomes uploaded, it is copied when the release is selected in an ordinary future run. Destination starter assets still require manual cleanup.

Signed v3 recovery markers bind the source, tag, destination repository and destination release ID. A newly created release briefly has a non-replacing provisional marker until its ID is known and bound, before asset operations. After interruption, a valid provisional marker can be bound to the discovered release ID if the destination has exactly one matching tag; it never authorizes asset replacement. Ambiguous matches require manual review. Older unbound v3 markers that do not meet these constraints cannot be safely resumed; v1/v2 recovery remains supported. Binding prevents copying a bound marker to another destination or release, but does not prevent replaying an earlier valid marker on the same release: there is no secure durable transaction registry. Avoid concurrent runs and restrict who can edit release notes.

The workflow saves failed uploads in `failures.json` and checks it on the next run. Entries use hashes (short identifiers made from the source details), not passwords or file names. Someone may be able to guess the original details from a hash, so don't treat them as secret.

| Situation | What happens |
| --- | --- |
| An upload succeeds on retry | Its entry is removed from `failures.json`. |
| A run is cancelled or cannot save the file | The release's pending marker still helps the next run resume. |
| Someone changes the management branch during a run | Saving the file may fail rather than overwrite their changes. Run the workflow again. |
| Branch protection blocks the workflow | Allow its GitHub token to push the failure file. |
| A source or pending marker is changed or removed | Old entries may need manual review. |

Only attempted uploads and replacements appear in the file. A repository error can prevent the workflow from reaching later releases in that repository.

A retry checks already uploaded files and copies missing ones. When the source has exactly one `SHA256SUMS` or `SHA256SUMS.txt` (case-insensitive), at most 1 MiB, the mirror also accepts strictly formatted SHA-256 lines for simple file names as a source digest fallback. Malformed lines, duplicate names, unsafe paths, or multiple matching checksum files disable this fallback. The checksum file is reread to detect changes; this does not authenticate it. When both sites publish valid SHA-256 digests for a file, the digests match, and both report the same valid nonnegative integer size, the mirror skips downloading it. Otherwise it downloads and compares the bytes; missing or malformed digests (including on Forgejo) are not used to decide equality, and equal sizes alone do not mean equal contents; the optional timestamp heuristic below can also skip downloads. Digest metadata is included in the source manifest checks; previous digest-bearing and legacy pending markers can be upgraded only after every current source asset is byte-verified against the destination and the source manifest is rechecked; missing or differing destination assets require manual review. Any valid published digest for a downloaded file is checked against its bytes; a mismatch stops the run before replacement. To replace a changed file, the mirror deletes the matching destination file before uploading its replacement; an interrupted upload can temporarily leave it missing. The mirror never deliberately deletes files absent from the source list. The source list is checked again before deletion, but changes between that check and deletion cannot be ruled out. If the list changes during an interrupted copy, the run stops for that repository and needs manual review. If GitHub leaves an incomplete starter file, remove it manually before retrying. A pending release or journal entry whose source release or file has been removed may need manual review. Unsigned v1 and v2 pending releases with conflicting file contents require manual review; they never authorize deleting or replacing existing assets. If a legacy marker already matches the current manifest, matching assets can be retained and missing assets uploaded; older manifest formats may still require all assets to be byte-verified before the marker can be upgraded.

When source assets have an ID, timestamp (`updated_at`, otherwise `created_at`), and size, the mirror can record their identities and verified SHA-256 hashes in a signed JSON release asset, `TIMESTAMPS.txt`. **This timestamp/size fallback is a heuristic, not proof of unchanged source content:** a source can change bytes without changing the recorded timestamp and size, so such changes may be missed even when the destination hash matches the cache. Published source SHA-256 checksums or byte comparison provide stronger evidence. It skips a download only if all identity fields match and GitHub's destination SHA-256 matches the recorded hash. Assets without a timestamp or usable ID are excluded from the cache and compared normally. If the source uses `TIMESTAMPS.txt` or `TIMESTAMPS.txt.repo-mirror-staging`, or an existing timestamp asset is not owned by this mirror, the cache is disabled for that release; source and unrelated assets are not repurposed. Updates stage and verify the replacement before deleting the old owned manifest, then rename the staged asset; this is not atomic, and an interrupted rename leaves the verified staging asset for retry. A stale or conflicting staging asset requires manual reconciliation and is never automatically removed. Concurrent runs are not coordinated and should be avoided.

A checksum file is a **source assertion**, not independent authentication. A malicious or compromised source can supply malicious assets and matching hashes; neither those hashes nor the timestamp heuristic protect against it. Verify provenance separately if the source is not trusted.

## Failures and limits

| Situation | What happens |
| --- | --- |
| One repository fails | The workflow tries the other planned repositories, then reports failure. |
| Source discovery fails | Known repositories whose names could match the failed source's prefix are skipped; other names are attempted, and the run fails. An empty prefix blocks all names. This conservative check can skip names that would not actually collide. Fix discovery or choose distinct prefixes and retry. |
| Invalid configuration or duplicate mirror names | The workflow stops before copying repositories. |
| Empty source repository | No branches or tags are pushed or deleted. |
| Branch protection blocks changes | The mirror may be only partly updated. Check the failed run. |
| Source marker removed | The workflow refuses to modify the mirror. Keep the `[repo-mirror: ...]` text in its repository description. |
| Files need more disk space than available | The run fails. Copies use temporary disk space, sometimes for two copies of a file. |
| Git LFS | Large files stored outside Git are not copied by this workflow. |
| Submodules | Repositories linked inside another repository are not copied automatically. |
| Other data | Issues, pull requests, settings and secrets are not mirrored. |

Only copy content you have permission to mirror. Protect the management repository's default branch: anyone who can change its workflow can change what runs with the app's permissions.

GitHub can delay or skip scheduled runs. It also disables scheduled workflows in a public repository after 60 days without repository activity. Check the Actions page and [re-enable the workflow](https://docs.github.com/actions/using-workflows/disabling-and-enabling-a-workflow) if needed.

## Run locally

Run tests:

```sh
python -m unittest discover -s tests -v
```

To run the mirror locally, set `GH_TOKEN` to an installation token for the GitHub App and `DESTINATION_OWNER` to the account that owns the mirrors. A supplied `GH_TOKEN` is fixed and can expire during a long run. For automatic renewal instead, set `MIRROR_APP_ID`, `MIRROR_APP_PRIVATE_KEY`, and `DESTINATION_OWNER` in the environment; the app credentials take precedence over `GH_TOKEN`. Then run:

```sh
python mirror.py --config config.json
```

A local run changes GitHub repositories. Test with a separate destination first.

### Timestamp cache setup and recovery

A high-entropy persistent secret is required for authenticated release recovery (and is also used by the timestamp heuristic). Generate one (for example `openssl rand -hex 32`) and save it as the management repository's GitHub Actions secret `MIRROR_TIMESTAMP_SECRET`. The workflow passes it to the mirror; for local runs set the same environment variable. Keep it private and persistent across runs, separate from source-controlled files and the source repository. Back it up in a secure secret manager. The mirror fails at startup before network or repository operations if it is missing or empty. Changing or losing it makes existing signed pending markers unverifiable and requires manual review.

Manifest ownership is authenticated with HMAC-SHA256. Unsigned old manifests, manifests signed with a lost or rotated key, and tampered manifests are treated as unknown: they are neither used nor deleted, and caching is disabled for that release. To restore caching, manually inspect and remove the unknown `TIMESTAMPS.txt` and any conflicting staging asset on the destination only after confirming they are not source assets or needed data; rerun to generate a new signed cache. Retain the same secret to recover an interrupted staged replacement; if the source changes or a staging asset conflicts, inspect it manually. Do not run concurrent mirrors against the same destination.
