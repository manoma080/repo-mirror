# Repository mirror

This project copies public GitHub and Forgejo repositories into private GitHub repositories. The original repository is the **source**. The copy is the **mirror**. Forgejo is a self-hosted Git server.

## What it copies

| Item | What happens |
| --- | --- |
| Schedule | Runs every 3 hours (UTC). You can also start it by hand in **Actions → Mirror repositories → Run workflow**. |
| Branches and tags | Copied, including rewritten history. Branches and tags deleted at the source are deleted in the mirror. |
| Releases | A new mirror gets the full release history, oldest first. Later runs compare source and destination lists and sync missing releases, the first and latest source releases, and any release still pending or failed. Other existing releases are not rechecked. |
| Release files | Missing files are added. Files with the same name but different bytes are replaced. Files removed at the source stay in the mirror. |
| New mirrors | Created as **private** repositories with GitHub Actions turned off. You can make them public later. |
| Stars | Not copied. |

**Use a dedicated mirror account or organization.** The workflow can overwrite and delete branches and tags in the mirrors.

## Choose sources for a manual run

Enter exact repository URLs in the **source_urls** box, separated by commas. Leave it empty to mirror everything in the config.

- URLs must match a configured host, owner and repository name exactly.
- Unknown, duplicate or malformed URLs stop the run.
- The run first looks up every source and checks for name clashes. If any lookup fails, the run stops before copying.
- Scheduled runs always mirror everything.

### Add to config checkbox

- **Ticked** with URLs: the URLs are written into `config.json` and committed to the default branch first. The test and mirror jobs then run on that commit, and only the typed repositories are mirrored. Future scheduled runs include them.
- **Unticked** with URLs: a one-time mirror. The repositories are copied now but are not saved to `config.json`, so scheduled runs skip them. They do not need to be in the config, but the same host, owner and name rules apply.
- **Empty** (ticked or not): a regular full run of everything in the config.
- Repositories already in the config are not written twice. If nothing changes, no commit is made.
- Repository URLs on `github.com` are read as GitHub sources. Any other host is read as a Forgejo source.
- Terms: *config* means `config.json`, the list of sources the mirror reads. *One-time mirror* means a run that copies repositories without saving them for later runs.

## Configure sources

Edit [`config.json`](config.json). Each entry copies chosen repositories, or all public repositories owned by one user or organization.

| Setting | Meaning |
| --- | --- |
| `destination_account_type` | `personal` (your own account) or `organization`. |
| `type` | `github` or `forgejo`. |
| `base_url` | The source site's HTTPS address. |
| `owner` | The source user or organization name. |
| `repos` | A list of repository names, or `"all"`. |
| `prefix` | Optional text added before each mirror name. Default: `mirror-OWNER-`. |

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

The first entry creates `mirror-alice-project-one`. If two sources would create the same name, give them different prefixes. `"all"` means repositories the user owns, not repositories they starred.

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
- Jobs run one after another, so the failure file is never written by two jobs at once.
- A transfer already in progress keeps its old token. If it fails, that repository is reported as failed and retried on a later run.

The token is also sent with GitHub API read requests (to `api.github.com` only, on port 443). This reduces rate-limit errors. Forgejo and public file downloads never get the token, and redirects to other sites never get it either.

## Logs and errors

Progress is printed to the run log, which is public in this management repository.

- Logs show source owner and repository names, release tags and file names, with a counter for each.
- Anything that looks like a secret is hidden. This is a best effort, not a guarantee. Do not put secrets in names, tags or file names.
- URLs, destination IDs, credentials and raw error text are not logged.
- Time figures show how long each step took. Steps can overlap, so do not add them together.
- A file-count number means upload attempts, not verified results.

When a web request fails, the log shows the HTTP status code and what operation failed (for example "source release list"). It does not show the URL, the response body, or signed download links. A 403 error does not always mean rate limiting, so check the cause.

## Releases in detail

- A release with no files is valid. Each file must be under **2 GiB**.
- **Drafts:** GitHub may return a synthetic `untagged-` tag for a draft. Source drafts with such tags are rejected rather than copied as real tags (even a legitimate draft tag with that prefix is conservatively rejected). For supported source tags, completed destination drafts retain a visible signed identity marker in their body until publication, so reruns can identify and publish the same release. Pending drafts use an ID-bound v3 recovery marker. Keep `MIRROR_TIMESTAMP_SECRET` stable: changing it, removing/editing a marker, or using older unmarked destination drafts requires manual reconciliation. Names and synthetic destination tags are not identities. If a create succeeds but its ID-bound marker cannot be saved, manual reconciliation can also be necessary.
- **Latest release:** the source's `/releases/latest` picks the release to mark as latest on GitHub, even if it is older than the usual window. The mirror promotes it only after all selected releases are done. A failed promotion is retried next run. Pre-releases and drafts are never promoted.
- **404 from `/releases/latest`:** no release is promoted. The repository may be private, or the Forgejo server may be too old. Other errors fail the repository.
- **Order:** releases are sorted by publish time, then creation time. If a time is missing or invalid, the source's list order is used. GitHub sets its own release times; the source's times are not copied.

### Release markers (how the mirror resumes)

While a release is being copied, its notes get a `[repo-mirror:pending:v3]` marker with transaction data. The title stays the same until the copy finishes.

- If a run stops, the next run can resume the marked release, even outside the usual window, as long as it still exists at the source.
- Markers are signed with a secret (see [Secret setup](#secret-setup)). The signature proves the marker came from this mirror.
- Do not write or copy these reserved markers in release notes or titles.
- Malformed or conflicting markers, and changed source file lists, need manual review.
- After the copy, the source's title and notes are restored (including empty notes). Very long notes may leave no room for the marker.
- A new mirror stays in "initializing" mode until its full history is copied.

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
| A run is cancelled or cannot save the file | The release marker still lets the next run resume. |
| Someone changes the management branch during a run | Saving may fail instead of overwriting their change. Run again. |
| Branch protection blocks the workflow | Allow the workflow's GitHub token to push `failures.json`. |
| A source or marker is changed or removed | Old entries may need manual review. |

## Checking files

A retry checks files that are already uploaded and copies missing ones. Each file is compared in this order. The first step that gives an answer wins.

1. **Published SHA-256 digest** (a fingerprint of the file's bytes). Used when both sites publish a valid digest, they match, and the sizes match. Forgejo often has no digest.
2. **Checksum file fallback.** If the source has exactly one `SHA256SUMS` or `SHA256SUMS.txt` (up to 1 MiB), its strictly formatted lines are used as digests. Bad lines, duplicate names, unsafe paths or several checksum files turn this off.
3. **Timestamp and size cache (heuristic).** Used only for files with a usable ID, timestamp and size, and only if a verified `TIMESTAMPS.txt` exists. See below.
4. **Byte comparison.** If the first three steps cannot decide, the file is downloaded and compared byte by byte.

Missing or bad digests are never treated as proof of a match. Equal sizes alone do not prove equal contents.

### Limits of the checks

- **Checksum files are a source claim, not proof.** A malicious source can publish bad files with matching hashes. Verify the source yourself if you do not trust it.
- **The file-list fingerprint** (used to guard pending releases) only includes stable fields: name, size, asset ID, state, published digest and checksum-file content. It ignores volatile fields such as download counts, timestamps, download URLs, uploader, labels and content type. A content change that keeps all stable fields the same can slip past it. The byte check before replacing a file covers that gap for files already on the mirror.
- **Timestamp and size cache (`TIMESTAMPS.txt`).** The mirror stores each file's ID, timestamp, size and SHA-256 in a signed file named `TIMESTAMPS.txt`. A file is skipped only if all identity fields match and the mirror's stored hash matches the copy on GitHub. This is a **heuristic**: a source can change a file without changing its timestamp or size, so changes can be missed.
- If the source already has a `TIMESTAMPS.txt`, or the existing one was not made by this mirror, the cache is turned off for that release. Source files are never reused or deleted for this.
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

Signed files that fail verification (old, tampered, or signed with a different key) are treated as unknown. They are not used and not deleted. To restore caching, check the unknown `TIMESTAMPS.txt` and any conflicting staging file by hand, delete them only if they are not source files, and run again. Keep the same secret so an interrupted replacement can be recovered.

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

## Wipe protection

Before updating a nonempty mirror, the repository workflow compares the old and new default-branch trees and the old default history with all captured source branches and tags. It treats the change as a wipe only if the default-branch file count drops by at least 75% relative to the old count **and** at least 75% of old commits are unreachable from all source branches and tags. Renamed or replaced files count toward the new total. Empty old trees do not trigger the heuristic. This is not a general backup: changes outside the default branch, retained commits, or smaller losses can evade it.

On a trigger, the workflow creates a private `<mirror>-wipe-<UTC timestamp>` repository, disables Actions, reads back both settings, then copies and verifies the captured branch and tag object IDs. It sets the archive's default branch to the old mirror default and verifies it through both the GitHub API and Git before updating the mirror. Archive creation or verification failure stops the update; a failed attempt may leave an empty or partial archive for manual inspection. The archive includes Git refs and reachable Git objects only, not release metadata, release assets, Git LFS objects, repository settings, or hidden refs. The GitHub App needs repository administration write permission to create private repositories and update their default branches, permission to disable Actions, and permission to push workflow files.

Destination ref updates and deletions use explicit leases and atomic pushes; concurrent new refs are left alone. Ref changes can still occur after verification, and default-branch settings cannot be updated atomically with Git pushes. A failed multi-stage update may therefore require inspection and a new run. Snapshot, detection, archive and push elapsed times are logged separately.

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
| `tests/` | Unit tests. |

For the lint, format and type-check settings, see [`pyproject.toml`](pyproject.toml).
