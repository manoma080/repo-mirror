# Repository mirror

This tool copies Git repositories from GitHub, Forgejo, and GitLab into your own GitHub account or self-hosted Forgejo server.
The original repository is the **source**, the copy is the **mirror**, and the server holding your mirrors is the **destination**.
You list your sources in a JSON **manifest** stored in a separate private **control repository**, so your mirror list stays private while this code repository stays public.
Each run mirrors branches, tags, releases, and release files, and creates new mirrors as private repositories with Actions disabled.

## What it does not do

- **No stars, issues, pull requests, wikis, settings, or secrets.** Only Git branches, tags, releases, and release files are copied.
- **No Git LFS objects or submodules.** Git refs keep pointers, not external LFS files or linked repositories.
- **No private sources without credentials.** Forgejo and GitLab sources are read as public repositories only; private GitHub sources work only when your GitHub App installation can read them.
- **No destination deletion.** Removing a source from the manifest never deletes its mirror.
- **No atomic sync.** A failure mid-run can leave partial ref or release updates that need inspection before retrying.
- **Partial release selection after initialization.** After the first full copy, later runs only check the first listed release, the latest release, missing releases, visible drafts, pending releases, and previously failed releases.
- **Refs-only wipe archive.** If a destructive rewrite triggers a **wipe archive** (a private backup repository created before force-updating), that archive holds Git refs and reachable objects only—not releases, assets, or LFS.
- **No source release timestamps.** Destinations set their own release timestamps; source timestamps are not copied.
- **Some file checks download bytes.** When a source or destination lacks a trusted SHA-256 digest, the runner downloads release files to compare bytes.

## Quick start — GitHub runner

1. **Create a GitHub App.** Give it **Read and write** **Repository permissions** on **Contents**, **Administration**, **Actions**, and **Workflows**, disable **Webhooks**, copy the **App ID** from the app's **General** settings page, and generate a private key (`.pem`).
2. **Install the app.** Install it on the dedicated personal account or organization that will own your mirrors (`MIRROR_OWNER`), granting access to all repositories.
3. **Fork this repo and create a private control repository.** Place both your fork (default branch `main`, with GitHub Actions enabled on its **Actions** tab) and a private `<MIRROR_OWNER>/repo-mirror-config` repository (default branch `main`, containing `config.json`) under the `MIRROR_OWNER` account or organization. If your fork lives under a different owner than `MIRROR_OWNER`, edit `MIRROR_CONFIG_REPOSITORY` in `.github/workflows/mirror.yml` to `<MIRROR_OWNER>/repo-mirror-config`.
4. **Set secrets and variables.** In your fork (**Settings → Secrets and variables → Actions**), add:
   - Secrets: `MIRROR_APP_ID`, `MIRROR_APP_PRIVATE_KEY` (full PEM), `MIRROR_TIMESTAMP_SECRET` (at least 32 random bytes, e.g. `openssl rand -hex 32`), and optional `MIRROR_GITLAB_TOKEN` (a `gitlab.com` read token that raises the `gitlab.com` rate limit during the `mirror` job; sent only to `https://gitlab.com` API and same-origin asset requests, never to self-hosted GitLab origins — see [docs/gitlab-sources.md](docs/gitlab-sources.md)).
   - Variables: `MIRROR_OWNER` (destination account or organization) and optional `MIRROR_ACCOUNT_TYPE` (`personal` or `organization`, default `personal`).
5. **Run the workflow.** Open **Actions → Mirror repositories → Run workflow** to run manually, or wait for the 3-hour UTC schedule.
6. **Read the summary.** Each finished repository prints its own log group; the bottom summary lists succeeded, failed, and inactive repositories.

## Quick start — self-hosted Forgejo

**Prerequisites:** Python >= 3.14 on the host, `PyJWT` installed for `scheduler/github_app.py`, a GitHub App + installation ID (required by `auto_update.py` even when `--remote` is public), a Forgejo API token for the mirror owner with repository read/write access (and organization access if mirroring to an org), an owner-only recovery secret (`--timestamp-secret-file`, >= 32 UTF-8 bytes), and a persistent `0700` `--journal-dir`. See [`scheduler/README.md`](scheduler/README.md) for full setup details.

1. **Prepare self-hosted Forgejo.** Run Forgejo on loopback HTTP (`http://127.0.0.1:<port>`, default `3000`), create a dedicated mirror account or organization, and save its API token in an owner-only file (`chmod 600`).
2. **Install the launcher.** From a reviewed checkout, copy [`scheduler/auto_update.py`](scheduler/auto_update.py) and [`scheduler/github_app.py`](scheduler/github_app.py) into an owner-only directory (`chmod 700`) outside any checkout.
3. **Set up the control repository.** Either create a private self-hosted Forgejo repository (`--forgejo-config-repo OWNER/NAME` and numeric `--forgejo-config-id ID`) with default branch `main`, Actions disabled, and three pre-seeded files on `main` (`config.json`, `failures.json` containing `{"version": 1, "journals": {}}`, and `migrations.json` containing `{"version": 1, "migrations": {}, "signatures": {}}`; get the numeric ID from `GET http://127.0.0.1:<port>/api/v1/repos/OWNER/NAME`), **or** store `config-forgejo.json` in a private GitHub repository (`--config-repo OWNER/NAME`).
4. **Place the workflow and wrapper.** Copy [`scheduler/forgejo-actions.yml`](scheduler/forgejo-actions.yml) to `.forgejo/workflows/mirror.yml` in a separate private workflow repository, bind a runner and host launcher wrapper to it, and replace `@SCHEDULE_CRON@`, `@SCHEDULE_TIMEZONE@`, `@RUNNER_LABEL@`, `@CONFIG_REPOSITORY@`, and `@LAUNCHER_WRAPPER@`. The workflow runs `exec @LAUNCHER_WRAPPER@ mirror` and exports `FORGEJO_MANUAL_INPUTS` on manual dispatch; the wrapper must forward that environment to `auto_update.py`, for example:

```sh
#!/bin/sh
set -eu
[ "${1:-}" = mirror ] || exit 2
export MIRROR_APP_OWNER=APP_OWNER
export MIRROR_APP_ID=APP_ID
export MIRROR_APP_PRIVATE_KEY_FILE=/path/to/app.pem
exec /path/to/python3 /path/to/forgejo-launcher/auto_update.py \
  --remote https://github.com/OWNER/repo-mirror \
  --state-dir /path/to/forgejo-state --job-lock /path/to/job.lock \
  --installation INSTALLATION_ID -- \
  --token-file /path/to/token --timestamp-secret-file /path/to/recovery-secret \
  --journal-dir /path/to/journals --owner OWNER --port 3000 \
  --app-id APP_ID --private-key-file /path/to/app.pem \
  --forgejo-config-repo OWNER/mirror-config --forgejo-config-id 123 \
  --lock /path/to/batch.lock
```

5. **Run a pilot.** Test with two representative sources (including one with releases and assets) before enabling the schedule. See [`scheduler/README.md`](scheduler/README.md) for all flags, lock files, and pilot checks.

## Configuration reference

Both runners use the named JSON format with `destinations` and `repositories`, plus optional `auto_discoveries` and `host_concurrency_limits` (Forgejo also still accepts deprecated `groups` and legacy flat URL arrays).
Legacy `sources` and `destination_account_type` keys are rejected.
The GitHub runner reads `config.json` from `<owner>/repo-mirror-config`; the Forgejo scheduler reads `config-forgejo.json` (with `--config-repo`) or `config.json` (with `--forgejo-config-repo`).

### `destinations`

Declare exactly one destination profile.
Use an explicit profile (`type`, `url`, `owner`, `auth`) for a personal account, or `{"account_type": "personal"}` / `{"account_type": "organization"}` (Forgejo also accepts `{}`).
Because explicit profiles always select `"personal"`, organization destinations on GitHub (`MIRROR_ACCOUNT_TYPE=organization`) must use `{"account_type": "organization"}`.
The neutral `"auth": "mirrorbot"` alias tells the GitHub runner to require your GitHub App credentials and reject a static `GH_TOKEN`; `"auth": "forgejo-home"` refers to the Forgejo `--token-file`.

Minimal valid starter `config.json` for a GitHub source into a personal GitHub mirror account (replace `"destinations"` with `{"github": {"account_type": "organization"}}` for an organization):

```json
{
  "destinations": {
    "github": {"type": "github", "url": "https://github.com", "owner": "mirror-owner", "auth": "mirrorbot"}
  },
  "repositories": [
    {"source": {"url": "https://github.com/example-owner/example-repo"}, "mirrors": {"github": {}}}
  ]
}
```

### `repositories`

List each source repository and its single mirror route.
Default mirror names are `mirror-<owner>-<repo>` on GitHub and `<repo>` for explicit entries on Forgejo (`auto_discoveries`-inducted Forgejo entries default to `mirror-<owner>-<repo>`, and nested GitLab owners replace `/` with `-`); visibility defaults to `"private"`.
Optional positive integer `id` fields pin source and destination identities.
For explicit GitLab entries, the GitHub runner requires **both** `source.id` and `mirrors.<profile>.id` pins (and rejects unconfigured one-time GitLab URLs), whereas the Forgejo runner requires `source.id` only (and allows one-time `gitlab.com` manual dispatch via runtime lookup). Find a GitLab project's numeric ID under **Settings → General** or via `GET /api/v4/projects/<url-encoded-path>`.
Set `"ignore_releases": true` on a mirror entry to copy branches and tags only.

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

For self-hosted Forgejo (`config-forgejo.json` or control-repo `config.json`):

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

### `auto_discoveries`

Automatically onboard all exposed repositories from an upstream owner or direct GitLab group (excluding subgroups and shared projects) before workers start.
Rules require an authoritative control repository and pin the numeric owner `id` (required up front for every GitLab rule—find it under **Settings → General** or via `GET /api/v4/groups/<url-encoded-path>` / `GET /api/v4/users?username=<user>`; pinned on first discovery for GitHub and Forgejo) in a single byte-conditional write with readback.
Pre-existing unpinned destinations are never adopted; use `exclude` to skip repository basenames and `overrides` for per-repo settings.

<!-- gitlab-example: github-group -->
```json
{
  "destinations": {"github": {"type": "github", "url": "https://github.com", "owner": "mirror-owner", "auth": "mirrorbot"}},
  "repositories": [],
  "auto_discoveries": [
    {"type": "gitlab", "url": "https://gitlab.com/team/sub", "id": 7}
  ]
}
```

For self-hosted Forgejo (pass `--asset-max-bytes` between `1` and `2147483647` after `--` in the Forgejo launcher wrapper whenever GitLab sources are configured):

<!-- gitlab-example: forgejo-group -->
```json
{
  "destinations": {"local": {}},
  "repositories": [],
  "auto_discoveries": [
    {"type": "gitlab", "url": "https://gitlab.com/team/sub", "id": 7}
  ]
}
```

### `host_concurrency_limits`

Cap how many repositories run at once per source host (lower-case hostname, optional port) to avoid HTTP 429 bursts.
Defaults are 16 concurrent workers on the GitHub runner and 4 (`--jobs`, 1–64) on the Forgejo scheduler.

```json
{"host_concurrency_limits": {"default": 16, "git.example": 4}}
```

## Day-two operations

- **Schedules:** The GitHub workflow runs every 3 hours (UTC). GitHub disables scheduled workflows in public repositories after 60 days of inactivity.
- **Manual runs:** Both workflows accept six inputs (`input_01_*`..`input_06_*` on Forgejo): `source_urls` (comma-separated exact HTTPS URLs; blank mirrors everything; owner URLs are accepted on GitHub only, not Forgejo), `add_to_config`, `destination_name` (one repository URL only), `visibility` (new destinations only), `remove_from_config` (updates config only; never deletes mirrors), and `ignore_releases`.
- **Failures and retries:** A failed repository or release does not stop the rest of the batch. Failed uploads are recorded as hashed entries in `failures.json` (or per-destination Forgejo retry journals) and retried on the next run.
- **Inactive sources:** Completed repositories with no commit, tag, or release activity for 365 days (configurable via `--inactive-after-days` when invoking `mirror.py` or `forgejo_sync.py` directly; `0` disables) are reported as warnings.
- **Where to look:**
  - [docs/operations.md](docs/operations.md) — release selection, markers and resume, file checks, asset retries, wipe protection, large-repo seeding, secret setup, local development, and limits.
  - [docs/gitlab-sources.md](docs/gitlab-sources.md) — GitLab rate limits and visibility caveats, release asset links and SSRF boundaries, relocation bindings, and post-batch migrations.
  - [docs/auto-discoveries.md](docs/auto-discoveries.md) — `auto_discoveries` schema, onboarding algorithm, workflow behavior, and migration from `groups`.
  - [scheduler/README.md](scheduler/README.md) — self-hosted Forgejo launcher setup, CLI flags, trust boundary, and pilot checklist.

## Security notes

- **Use a dedicated mirror account or organization.** The runner force-updates and deletes branches and tags in mirrors and can create repositories and change their Actions settings.
- **Least privilege and token scope.** The GitHub App installation token is sent only to `https://api.github.com` (port 443), `https://uploads.github.com` (port 443), and `https://github.com` for Git pushes and GitHub source fetches—never to Forgejo, GitLab, public file downloads, or cross-origin redirects. Keep `MIRROR_APP_PRIVATE_KEY`, `MIRROR_TIMESTAMP_SECRET`, and Forgejo token files owner-only (`chmod 600`) and outside Git.
- **Protect your workflow and control repository.** Anyone who can edit the workflow or the fetched `main` branch can run code with the app's permissions. Keep source markers (`[repo-mirror: ...]` on GitHub, `repo-mirror-source: ...` on Forgejo) intact in mirror descriptions.
