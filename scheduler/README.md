# Forgejo host schedule

The templates in this repository are not automatically installed or enabled. Check the target host's actual service and timer state before making changes.

`config-forgejo.json` currently contains 11 GitHub mirror URLs; these GitHub repositories are the sources for Forgejo. Nine user-skipped repositories are excluded from this Forgejo batch. Review every manifest entry and its destination mapping before use. This local manifest is separate from the GitHub Actions `config.json`, uses a JSON array of URL strings or explicit relocation entries rather than GitHub's source configuration objects, and is not automatically synchronized with it. Adding a GitHub mirror to `config.json` does not add it to the local Forgejo schedule.

Entries may also be HTTPS Forgejo repository URLs of the form `https://forge.example/owner/repo` (optional port). Credentials, query strings, fragments, subpaths, and `.git` suffixes are not accepted. GitHub sources remain restricted to the existing `https://github.com/manoma080/mirror-*` pattern. Ordinary string entries use the final URL component as destination name. A separately verified relocation may instead use a structured mapping with an explicit destination name and original recovery identity; see [Explicit relocation bindings](../README.md#explicit-relocation-bindings). Do not publish a structured entry until the separately installed launcher has been upgraded to understand it. Source URLs are passed unchanged to workers. Case-insensitive current and historical name collisions are rejected before workers start. The standalone launcher performs structural checks, while the fetched scheduler validates the full manifest again before the batch lock and workers. Neither check proves the host actually serves Forgejo, that the source is reachable, or that a historical association is valid. Review hosts and mappings before use; do not put credentials in URLs.

Forgejo sources are accessed anonymously: only public repositories, refs and published releases accessible without authentication can be mirrored. Private repositories and draft releases are not visible anonymously; authenticated private Forgejo source support is not implemented. Neither the GitHub source installation token nor the destination Forgejo token is reused for Forgejo source access.

The templates use a local owner-only Forgejo token file for destination writes and a GitHub App ID and private-key file for refreshable source installation tokens. These are separate credentials from the GitHub Actions secrets; the app installation must have access to the configured source repositories. Never commit credentials or private source details.

The runner validates and snapshots the full manifest and holds an owner-only nonblocking flock for the entire batch. It starts a separate sync process per source, with up to four concurrent processes by default; set `--jobs N` (1–64) to change the limit. Each worker has a 3600-second runtime limit measured from its launch; set `--repository-timeout SECONDS` to a positive finite value to adjust it. On timeout the process group receives SIGTERM, followed by SIGKILL after a two-second grace period, and the repository counts as failed; remaining repositories continue. SIGINT/SIGTERM also stop active process groups. Processes that deliberately leave their worker's process group cannot be controlled by this mechanism. Each completion is logged as a 1-based manifest position rather than a potentially sensitive repository name; completion order may differ from manifest order. Both templates select refreshable GitHub App credentials: the sync process obtains installation tokens through the app provider as needed, rather than relying on a single helper-minted token for the whole repository. A failed source does not stop subsequent sources; final exit status is 1 if any fail, 2 for configuration/lock errors, 75 if another batch holds the lock, or 130 on interruption. SIGINT/SIGTERM terminate active process groups before releasing the batch lock. Child output is suppressed; logs contain manifest positions, counts, and only allowlisted failure reasons. Detailed diagnostics require a separate controlled investigation. Do not run other sync processes without the same lock or other writers against these destinations.

**This is destructive reconciliation.** Shared Git logic can force-update branches and tags and delete destination-only refs. When wipe detection triggers, it first creates a private, timestamped `-wipe-` Git archive and verifies the captured refs; archive failure stops the update. This is conditional, not a backup for every destructive change. The archive contains Git refs and reachable objects only, not releases, assets, LFS objects, settings or hidden refs. The shared release policy processes source releases visible to the source credentials during initialization; on later runs it selects the first listed release, the source latest release, missing releases, visible drafts, pending releases and previously failed releases. It can update metadata and replace same-name assets for selected releases, but changes to older existing releases that are not selected can be missed. Destination-only releases and ordinary destination-only assets are retained; mirror-owned cache assets can be deleted or replaced. Forgejo cannot explicitly select the latest release: if the source latest is not already Forgejo's latest, synchronization fails rather than pretending it selected it. The destination must be private, have the expected source provenance marker, and have Actions disabled. Asset bytes are compared, but a same-size source mutation without a digest can race verification. A failure can leave earlier successful changes in place; inspect before retrying, especially after an interrupted upload.

**Empty release notes:** Forgejo's release PATCH cannot reliably clear nonempty notes with an empty body. The workaround writes exactly one ASCII space (U+0020) for a truly empty source body; it typically displays as empty, but is not byte-identical. Exactly one space is treated as logically empty in release-body comparisons and draft finalization, so an intentional one-space source cannot be distinguished from empty in those paths. Other whitespace, including two or more spaces, tabs and newlines, is preserved. Drift checks compare raw source and destination values; published finalization sends the raw source body, with Forgejo encoding empty as one space. Pending transaction markers can support recovery after interruption only when valid and the source and transaction state remain compatible; completed drafts retain their separate identity marker. Reconciliation is not atomic and can require manual review.

An isolated live Hydra test covering all 13 releases passed twice. Actual changed-asset transfers have only been exercised with mocks; they still require passing live verification. This does not replace the authorized pilot gate below.

Before an authorized bulk test, select two representative manifest sources, including one with releases and assets, and obtain authorization for their destination writes. Run each pilot separately under the same lock and without other writers. For each, verify the destination's privacy, provenance and disabled Actions; compare source and destination branch/tag refs, release metadata and asset bytes; then rerun and inspect the result for unexpected changes or conflicts. Record sanitized results without credentials or signed URLs. Reconcile any failure before proceeding. These are verification steps, not a claim that pilots have already passed.

The templates are inert files, not installed. Prefer the systemd user timer when the user manager is available; cron is a fallback. Both run at 00:00, 06:00, 12:00 and 18:00 local time (every six hours). The timer also has a startup delay, and `Persistent=true` catches up a missed calendar event when the user manager returns; cron does not. Systemd user timers require the user manager to remain running (or user lingering to be configured separately). Do not install or enable either until review and an authorized bulk test have passed. Do not use both.

After that gate, replace all placeholders in the selected template:

- `@PYTHON@`: absolute Python executable with the sync package's dependencies available; it runs both the installed launcher and fetched scheduler. Ensure `openssl` and `git` are available on the scheduler's PATH.
- `@UPDATER@`: absolute path to the separately installed, reviewed launcher, outside any checkout or snapshot.
- `@REMOTE@`: trusted repository HTTPS URL without embedded credentials (or a trusted local repository path); the launcher fetches its `refs/heads/main` on every run. SSH-style URLs are not supported.
- `@STATE_DIR@`: absolute path to an owner-only persistent directory for temporary snapshots (mode 0700). Protect its parents.
- `@JOB_LOCK_FILE@`: absolute path to the launcher job lock in an owner-controlled directory, distinct from `@LOCK_FILE@` and not a hard link to it.
- `@HELPER_PYTHON@`: absolute Python executable with PyJWT installed for the Manobot helper. This may differ from `@PYTHON@`. The launcher uses its configured default Manobot helper and installation; to override them, add `--helper /absolute/path/to/github_app.py --installation INSTALLATION_ID` before `--`. Configure the helper's credentials separately and securely for the scheduler environment.
- `@TOKEN_FILE@`: absolute path to the Forgejo token file.
- `@LOCK_FILE@`: absolute path to the persistent batch lock file in an owner-controlled directory.
- `@OWNER@`: verified Forgejo account name.
- `@APP_ID@`: GitHub App ID.
- `@PRIVATE_KEY_FILE@`: absolute path to the GitHub App private key file, not its contents.
- `@TIMESTAMP_SECRET_FILE@`: absolute path to a pre-provisioned, stable owner-only recovery signing secret, separate from the source manifest, Forgejo token and GitHub App key (including hard links). Generate it cryptographically with at least 32 bytes of randomness. Never put its value in a template or log. Preserve it across runs and restarts; changing it prevents verification of existing recovery markers.
- `@JOURNAL_DIR@`: absolute path to a pre-existing owner-only persistent directory for recovery journals. Preserve its contents across runs and restarts; do not place it in temporary or ephemeral storage.

### Install the launcher and understand the trust boundary

From a reviewed checkout, install a stable copy **outside** the checkout, in an owner-controlled location. For example, as the scheduler user (substitute your reviewed checkout and protected installation directory):

```sh
umask 077
install -d -m 700 /absolute/protected/forgejo-launcher
install -m 700 /absolute/reviewed/checkout/scheduler/auto_update.py /absolute/protected/forgejo-launcher/auto_update.py
install -d -m 700 /absolute/protected/forgejo-state
```

Set `@UPDATER@` and `@STATE_DIR@` to those installed paths. Updating the checkout does **not** update the installed launcher: review and explicitly reinstall it when upgrading. Protect the launcher, helper, interpreters, configuration, state, lock parent directories and credentials from untrusted writes. The fetched remote's `main` is executable code with access to the scheduler's credentials and Forgejo: trust and review who can change it, not just the manifest. A private snapshot binds the scheduler and manifest to the same freshly fetched commit. Fetch, authentication, network, snapshot or validation failure stops the job; it does not fall back to a cached checkout or run an old manifest. Network availability is therefore required for each scheduled and manual run.

An older installed launcher rejects Forgejo-source URLs even after the remote scheduler is updated. Stop the schedule, review and reinstall the new launcher, and verify the installed command before enabling a mixed-source manifest.

The launcher holds `@JOB_LOCK_FILE@` nonblockingly across fetch, snapshot and the entire scheduler run; contention exits 75. The scheduler separately holds `@LOCK_FILE@` for its batch; both paths must remain stable across all launchers and manual runs. Direct scheduler or sync invocations bypass the job lock and must not overlap; use the launcher instead. The updater state directory is not the persistent recovery journal: `@JOURNAL_DIR@` must be a separate directory, preserved independently.

The scheduler does not create the secret or journal directory. Provision both securely before enabling the job, owned by the scheduler user (secret mode 0600, directory mode 0700), and protect their parent directories from untrusted modifications. Back up the secret and journals securely together. Neither may be a symlink at the final path component.

The retry journal records hashed failure identifiers, not credentials or URLs, and prioritizes previously failed assets on a subsequent run; success removes their entries. Journals are separated by destination port, owner and source, and are distinct from the GitHub Actions failure file. Preserve them across deployments; they do not replace inspection of partial or ambiguous writes and are not a backup of mirrored data.

For a new installation, in a protected parent directory, generate a secret once (the exclusive create refuses to overwrite an existing one) and create the journal directory as the scheduler user:

```sh
umask 077
python3 -c 'import os,secrets; p="/absolute/protected/path/recovery-secret"; fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(fd,secrets.token_hex(32).encode()); os.close(fd)'
mkdir -m 700 /absolute/protected/path/journals
chmod 600 /absolute/protected/path/recovery-secret
```

The file contains 64 hexadecimal characters representing 32 random bytes; the scheduler rejects values shorter than 32 bytes after stripping surrounding whitespace. Length checks do not establish randomness. Never regenerate an existing secret as part of a deployment.

### Migrating an existing timer or cron job

Stop the old timer or disable the old cron entry; wait for running batches and direct syncs to finish. Preserve and securely back up the existing recovery secret and journal; do not rotate or delete them to clear an error. Install the reviewed launcher outside the checkout as above, provision its private state directory and distinct job lock path, and verify the trusted remote and Manobot environment. Render all placeholders using the existing scheduler lock, recovery secret and journal paths. Replace the old service or cron entry, ensuring no second schedule remains; for systemd run `systemctl --user daemon-reload`. After the review and pilot gate, enable only the selected schedule and verify the installed command and timer or crontab. Existing direct commands with the old manifest name must be retired, not merely renamed.

For a manual run, use the same rendered launcher command and both lock paths as the schedule, not `python -m repo_mirror.forgejo_schedule` directly. For example (replace every placeholder, including credentials paths):

```sh
@PYTHON@ @UPDATER@ --remote @REMOTE@ --state-dir @STATE_DIR@ --job-lock @JOB_LOCK_FILE@ --helper-python @HELPER_PYTHON@ -- --token-file @TOKEN_FILE@ --timestamp-secret-file @TIMESTAMP_SECRET_FILE@ --journal-dir @JOURNAL_DIR@ --owner @OWNER@ --app-id @APP_ID@ --private-key-file @PRIVATE_KEY_FILE@ --lock @LOCK_FILE@
```

The launcher injects the fetched manifest; do not supply `--manifest`. Add any scheduler options after `--`.

Use single paths/names without spaces or systemd/cron metacharacters. Do not embed tokens or private keys in templates. Add `--organization` if the owner is an organization, and adjust `--port` if needed. Keep the lock path the same in every invocation and in an owner-controlled directory. Verify the Forgejo token and app private key files are owner-only and owned by the running user. Ensure the installed launcher, helper and credentials remain at their configured paths, and that local Forgejo is running. Protect the remote and its main branch from untrusted edits: each job executes code fetched from it.

For systemd, copy the rendered service and supplied timer to `~/.config/systemd/user/`, run `systemctl --user daemon-reload`, then enable/start the timer only after the gate. Verify it with `systemctl --user list-timers forgejo-mirror.timer` and inspect sanitized journal results. For cron, add only the rendered line to the existing user crontab after the gate; verify with `crontab -l`. Neither timer nor cron is configured by this repository automatically.
