# Self-hosted Forgejo scheduler

This directory runs scheduled and manual mirrors into a self-hosted Forgejo server.
A Forgejo Actions workflow ([`forgejo-actions.yml`](forgejo-actions.yml)) invokes an installed copy of [`auto_update.py`](auto_update.py) alongside [`github_app.py`](github_app.py).
On each run, `auto_update.py` fetches `refs/heads/main` from your trusted code remote, validates the manifest, and runs `repo_mirror.forgejo_schedule` from a fresh private snapshot.

## Prerequisites

- Python >= 3.14 for the scheduler and workers, plus `PyJWT` installed in the Python used by `github_app.py` (`--helper-python`).
- A GitHub App and installation ID (`--installation` / `MIRROR_APP_INSTALLATION`), required by `auto_update.py` even when `--remote` is public.
- A Forgejo API token for the mirror account or organization (with repository read/write access and organization access when using `--organization`), saved in an owner-only file (`chmod 600`).

## Setup and workflow installation

1. **Install outside any checkout.** From a reviewed checkout, install `auto_update.py` and `github_app.py` into an owner-only directory (`chmod 700`) and create a separate `--state-dir` (`chmod 700`):

```sh
umask 077
install -d -m 700 /path/to/forgejo-launcher /path/to/forgejo-state
install -m 700 /path/to/checkout/scheduler/auto_update.py /path/to/forgejo-launcher/auto_update.py
install -m 700 /path/to/checkout/scheduler/github_app.py /path/to/forgejo-launcher/github_app.py
```

2. **Provision the recovery secret and journal directory.** Create a stable owner-only secret (at least 32 UTF-8 bytes, separate from tokens and keys) and a persistent `0700` `--journal-dir`. Neither may be a symlink at its final path component; back them up together.

```sh
umask 077
python3 -c 'import os,secrets; p="/path/to/recovery-secret"; fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(fd,secrets.token_hex(32).encode()); os.close(fd)'
mkdir -m 700 /path/to/journals
```

3. **Create a private control repository.** Either:
   - **Self-hosted Forgejo control repo (`--forgejo-config-repo OWNER/NAME` + immutable `--forgejo-config-id ID`):** Create a private repository with default branch `main` and Actions disabled. Look up its numeric `id` via `GET http://127.0.0.1:<port>/api/v1/repos/OWNER/NAME` using your Forgejo API token. Pre-seed three files on `main` before the first run: `config.json`, `failures.json` (`{"version": 1, "journals": {}}`), and `migrations.json` (`{"version": 1, "migrations": {}, "signatures": {}}`).
   - **GitHub control repo (`--config-repo OWNER/NAME`):** Store `config-forgejo.json` on `main` in a private GitHub repository.
   Do not combine `--forgejo-config-repo` and `--config-repo`.

4. **Install the workflow template and wrapper.** Copy [`forgejo-actions.yml`](forgejo-actions.yml) to `.forgejo/workflows/mirror.yml` on the default branch of a separate private workflow repository, bind a runner and host launcher wrapper to that repository only, and replace all five placeholders (never commit rendered values back here):
   - `@SCHEDULE_CRON@` — cron schedule expression
   - `@SCHEDULE_TIMEZONE@` — schedule timezone
   - `@RUNNER_LABEL@` — Forgejo Actions runner label (`runs-on`)
   - `@CONFIG_REPOSITORY@` — control repository slug shown in dispatch help text
   - `@LAUNCHER_WRAPPER@` — host launcher wrapper executable name (invoked as `exec @LAUNCHER_WRAPPER@ mirror`, receiving `FORGEJO_MANUAL_INPUTS` in the environment on manual dispatch; see the example wrapper in the root [`README.md`](../README.md#quick-start--self-hosted-forgejo))

## Launcher and scheduler options

Pass launcher options before `--` and scheduler options after `--`. Do not pass `--manifest`; `auto_update.py` injects the fetched snapshot.

- **Launcher (before `--`):** `--remote` (credential-free HTTPS URL or local path; no SSH), `--state-dir`, `--job-lock` (distinct from `--lock`), `--installation` (or `MIRROR_APP_INSTALLATION`); optional `--helper` (defaults to sibling `github_app.py`), `--helper-python` (defaults to the Python running `auto_update.py`; must have PyJWT), and `--app-id` / `--private-key-file` (or `MIRROR_APP_ID` / `MIRROR_APP_PRIVATE_KEY_FILE`).
- **Forgejo destination (after `--`):** `--token-file` (owner-only Forgejo API token), `--owner`, optional `--organization`, `--port` (default `3000`), `--timestamp-secret-file`, `--journal-dir`, `--lock`, and optional `--sync-python` (defaults to the Python running `forgejo_schedule`).
- **Control repository (after `--`):** `--forgejo-config-repo` + `--forgejo-config-id`, or `--config-repo` (plus `--config-account-type` when needed). Required for `auto_discoveries`, deprecated `groups`, and automatic relocation.
- **GitHub and GitLab sources (after `--`):** `--app-id` and `--private-key-file` with `MIRROR_APP_OWNER` exported in the environment (`auto_update.py` does not forward `--source-app-owner`); optional `--gitlab-token-file` (exported as `MIRROR_GITLAB_TOKEN` into the worker environment; `PRIVATE-TOKEN` is attached only for `https://gitlab.com` — API reads and same-origin asset link downloads — never to a self-hosted GitLab origin); `--asset-max-bytes N` (`1`–`2147483647`, required whenever GitLab sources are configured).
- **Concurrency and timeouts (after `--`):** `--jobs N` (default `4`, range `1`–`64`, combined with manifest `host_concurrency_limits`), `--repository-timeout SECONDS` (default `3600`; SIGTERM then SIGKILL after 2 seconds).

## Manifest, manual dispatch, and runtime behavior

- **Manifest format:** Named JSON with one destination profile (`{}`, `{"account_type": "personal"|"organization"}`, or explicit `{"type":"forgejo","url":"http://127.0.0.1:<port>","owner":"...","auth":"forgejo-home"}`, which rejects `--organization`). Flat URL arrays, deprecated `groups`, and signed relocation entries remain supported.
- **Source access:** GitHub sources use your GitHub App installation (so private GitHub repos visible to that installation can be mirrored when destination visibility is private; `"visibility": "public"` forces anonymous reads and requires a public source). Forgejo and GitLab sources are public-only.
- **Manual dispatch:** Forgejo sorts dispatch inputs alphabetically, so the workflow uses `input_01_source_urls`, `input_02_add_to_config`, `input_03_destination_name`, `input_04_visibility`, `input_05_remove_from_config`, and `input_06_ignore_releases`. Manual dispatch accepts repository URLs only (not owner URLs) and supports one-time unconfigured `gitlab.com` URLs via runtime project-ID lookup.
- **Exit codes and locking:** `--job-lock` covers fetch through batch end; `--lock` covers the batch. Exit codes are `0` (success), `1` (repository or migration failure), `2` (configuration, validation, or lock error), `75` (lock already held), and `130` (interrupted).
- **Destructive sync and limits:** Reconciliation force-updates and deletes branches and tags, checks wipe archives, requires intact `repo-mirror-source: ...` description markers (plus `[repo-mirror:initialized:v1]` after initialization), and cannot force Forgejo's latest release. See [docs/operations.md](../docs/operations.md), [docs/gitlab-sources.md](../docs/gitlab-sources.md), and [docs/auto-discoveries.md](../docs/auto-discoveries.md).

## Manual run example

```sh
MIRROR_APP_OWNER=APP_OWNER MIRROR_APP_ID=APP_ID MIRROR_APP_PRIVATE_KEY_FILE=/path/to/app.pem \
  /path/to/python /path/to/auto_update.py \
  --remote https://github.com/OWNER/repo-mirror \
  --state-dir /path/to/state --job-lock /path/to/job.lock \
  --helper-python /path/to/helper-python --installation INSTALLATION_ID -- \
  --token-file /path/to/token --timestamp-secret-file /path/to/recovery-secret \
  --journal-dir /path/to/journals --owner OWNER --port 3000 \
  --app-id APP_ID --private-key-file /path/to/app.pem \
  --forgejo-config-repo OWNER/mirror-config --forgejo-config-id 123 \
  --asset-max-bytes 104857600 --lock /path/to/batch.lock
```

## Trust boundary, upgrades, and pilot checklist

- **Trust boundary:** `auto_update.py` executes code fetched from `--remote`'s `main` branch with your Forgejo and GitHub credentials. Protect that branch, the installed launcher, and all lock/credential directories. Fetch or validation failure stops the run; there is no fallback to cached code.
- **Staged upgrades:** Updating the checkout does not update the installed launcher. Stop schedules, drain active runs, publish compatible scheduler code, review and reinstall `auto_update.py`, then publish new manifest features.
- **Pilot before scheduling:** Pick two representative sources (including one with releases and assets), run them separately under the real lock with no other writers, verify privacy, source markers, disabled Actions, refs, releases, and asset bytes, then rerun to confirm idempotency. Changed-asset transfers were tested with mocks and still require live verification.
