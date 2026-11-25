# Design: `auto_discoveries`

## Summary

`auto_discoveries` is a top-level array in the named configuration (`config.json` for the GitHub runner; `config-forgejo.json` for the Forgejo scheduler under `--config-repo`, or `config.json` in a local Forgejo control repository under `--forgejo-config-repo`/`--forgejo-config-id`, `repo_mirror/forgejo_migration.py:39,461-466`, `repo_mirror/forgejo_control_config.py:16`). Each entry declares an upstream account or GitLab group whose repositories are mirrored as a set, with newly added repositories onboarded automatically.

Discovery is an onboarding pass that runs before any worker starts: it creates missing destination repositories, publishes one pinned `repositories[]` entry per discovered repository in a single byte-conditional write to the authoritative configuration, reads the configuration back, and then runs the mirror phase against the published revision. Once onboarded, each discovered repository is an ordinary pinned `repositories[]` entry. `auto_discoveries` replaces legacy `sources` / `repos: "all"` and supersedes Forgejo-only `groups`.

## Motivation and non-goals

The current codebase has three overlapping configuration mechanisms: named `repositories[]` (`repo_mirror/config_named.py`), legacy `sources` with `repos: "all"` (`repo_mirror/sources.py:49-54,122-132`), and Forgejo-only `groups` (`repo_mirror/gitlab_onboarding_forgejo.py`). Legacy `repos: "all"` is rejected on named configs (`repo_mirror/config_named.py:11-12`), lacks per-repo `id` pins unless onboarded row-by-row on GitHub (`repo_mirror/cli.py:276-322`), blocks `remove_from_config` on children (`repo_mirror/adhoc.py:367-370`), and disables relocation (`repo_mirror/repository.py:314`). Forgejo `groups` handles only direct GitLab groups (`repo_mirror/gitlab_onboarding_forgejo.py:74`). Consolidating all owner/group discovery into `auto_discoveries` gives both runners one uniform onboarding and pinning path.

Non-goals for this iteration:
- No include-filters (such as `include_forks`, `include_archived`, or name globs); everything the owner exposes is included by default.
- No recursive GitLab subgroup discovery or shared-project inclusion (`repo_mirror/sources.py:97`).
- No automatic deletion of destination repositories or automatic adoption of pre-existing unpinned destinations (`README.md:292`).
- No authenticated private Forgejo or private GitLab source mirroring (`README.md:30,95`).

## Schema

Allowed top-level keys in a named configuration are:
- `destinations` (required, exactly one profile)
- `repositories` (required array)
- `host_concurrency_limits` (optional object)
- `auto_discoveries` (optional array of rule objects)
- `groups` (deprecated optional array on Forgejo only during the compatibility window; mixing `groups` and `auto_discoveries` is rejected)

Duplicate JSON keys remain rejected everywhere (`repo_mirror/config_json.py:4-10`). Unknown keys are rejected at the top level, in each `auto_discoveries[]` object, in each `overrides.<repo>` object, and in each `repositories[]` entry.

```json
{
  "auto_discoveries": [
    {
      "url": "https://github.com/BlackBearRevived",
      "type": "github",
      "id": 123456,
      "visibility": "private",
      "ignore_releases": false,
      "destination_name_prefix": "mirror-BlackBearRevived",
      "exclude": ["some-repo"],
      "overrides": {
        "other-repo": {"visibility": "public", "ignore_releases": true}
      }
    }
  ]
}
```

Allowed keys inside each `auto_discoveries[]` object are `url`, `type`, `id`, `visibility`, `ignore_releases`, `destination_name_prefix`, `exclude`, and `overrides`:

- `url` (required string): credential-free HTTPS owner/group URL (no query, fragment, `.git`, trailing slash, whitespace/control characters, or `.`/`..` segments; each path segment matches `NAME` at `repo_mirror/constants.py:15`; Forgejo hosts follow `repo_mirror/forgejo_local.py:30-50`).
- `type` (optional string): `"github"`, `"gitlab"`, or `"forgejo"`. Optional for `github.com` (must be `"github"` if given) and `gitlab.com` (must be `"gitlab"` if given); required as `"gitlab"` for self-hosted GitLab instances, otherwise non-`github.com`/non-`gitlab.com` hosts default to `"forgejo"` (`repo_mirror/adhoc.py:28,42-44`). `"github"` is rejected for non-`github.com` hosts.
- `id` (positive JSON integer, `type(v) is int and v > 0`, rejecting booleans as in `repo_mirror/sources.py:32`): the pinned upstream owner/group identity, always enforced once set. Required in config for GitLab groups (`repo_mirror/gitlab_onboarding_forgejo.py:74`); optional for GitHub, Forgejo and GitLab-user rules, where the first successful discovery pins it before any mirror work (see [Owner identity pinning](#owner-identity-pinning-decided)).
- `visibility` (optional string, default `"private"`): `"private"` or `"public"`. Applies only when creating a destination; on Forgejo, `"public"` requires a public source (`repo_mirror/forgejo_sync.py:640-656`).
- `ignore_releases` (optional JSON boolean, default `false`): non-booleans rejected (`repo_mirror/config_named.py:42-43`, `repo_mirror/forgejo_sync.py:158-159`). Applied to inducted entries unless overridden.
- `destination_name_prefix` (optional string): defaults to `mirror-<owner>` (with `/` replaced by `-` for nested GitLab group paths, matching `repo_mirror/sources.py:139`). Empty `""` is rejected; must match `NAME` and not be `.` or `..`. Default inducted destination name is `<destination_name_prefix>-<repo>`, enforcing `len <= 100` and `NAME` at induction (`repo_mirror/sources.py:141-142`, `repo_mirror/forgejo_sync.py:198-199`).
- `exclude` (optional array of strings, default `[]`): repository basenames matching `NAME` and not `.`/`..`, with no case-insensitive duplicates. Suppresses auto-discovery for those names; explicit `repositories[]` entries without `discovered_by` still win.
- `overrides` (optional object): maps repository basename (matching `NAME`, no case-insensitive duplicate keys, disjoint from `exclude`) to a non-empty subset of `{"name", "visibility", "ignore_releases"}`. `name` must match `NAME`, `len <= 100`, not `.`/`..`, and not match reserved `gitlab-[0-9a-f]{24}` for new entries (`repo_mirror/forgejo_dispatch.py:17,259-262`). `overrides` values apply at induction (and `ignore_releases` may be applied or updated explicitly; changing `overrides` later never silently renames or re-visibilities an already-pinned destination).
- Rule uniqueness: reject duplicate `(origin.lower(), owner.lower())` or duplicate `(origin.lower(), id)` when `id` is present.
- Case handling: host/origin is case-insensitive (`https://github.com` canonical); GitHub owner and repository names are compared case-insensitively (`repo_mirror/config_named.py:45`, `repo_mirror/forgejo_dispatch.py:84-86`); Forgejo and GitLab preserve exact path case in API lookups and generated `source.url`, while collision checks, `exclude`, and `overrides` keys match case-insensitively (`repo_mirror/sources.py:115`, `repo_mirror/forgejo_sync.py:231-240`).

### Ownership back-pointer (`discovered_by`)

Each `repositories[]` entry materialized by a rule carries a top-level string field `"discovered_by"` set to the canonical `url` of the rule that produced it:

```json
{
  "source": {"url": "https://github.com/BlackBearRevived/alpha", "id": 9001},
  "mirrors": {"github": {"name": "mirror-BlackBearRevived-alpha", "id": 7001, "visibility": "private"}},
  "discovered_by": "https://github.com/BlackBearRevived"
}
```

Validators reject any `discovered_by` whose normalized rule URL does not match a declared `auto_discoveries[].url`. `normalize_config` (`repo_mirror/config_named.py:32-58`), `normalize_named` (`repo_mirror/forgejo_sync.py:150-175`), `valid_manifest` (`scheduler/auto_update.py:128`), `plan_dispatch` (`repo_mirror/forgejo_dispatch.py`), and migration config rewrites (`rewrite_config` in `repo_mirror/migrations.py:365`, `prepare_source_transition` in `repo_mirror/forgejo_automatic_transition.py:36`) must all accept and preserve `discovered_by`. When a relocation moves a repository to a different owner, the migration rewrite drops `discovered_by` so the moved repository continues as an explicit pinned entry; same-owner renames preserve `discovered_by`.

## Invariants

- **Pinned steady state:** Every mirrored repository executes as a concrete `repositories[]` entry with positive integer `source.id` and `mirrors.<profile>.id`.
- **Publish before mirror:** No worker starts until onboarding publishes new bindings to the authoritative configuration and verifies the readback byte-for-byte.
- **No unpinned adoption:** A pre-existing destination repository without both saved `source.id` and `mirrors.<profile>.id` is never adopted, even if its description carries a matching `[repo-mirror: ...]` marker (`repo_mirror/adhoc.py:274-275`, `repo_mirror/gitlab_onboarding_forgejo.py:131-138`).
- **No automatic deletion:** Neither onboarding nor `remove_from_config` deletes remote destination repositories.
- **Complete coverage:** Every rule discovers all exposed repositories—forks, archived, empty, and all pages—without include-filters.
- **Origin and credential isolation:** Source API clients never send destination or GitHub tokens to Forgejo/GitLab or cross-origin redirects (`repo_mirror/github_api.py:341-353,583-597`, `repo_mirror/gitlab_source_adapter.py:39-40,89-90,105-121`, `repo_mirror/forgejo_source_adapter.py:110-116`).

## Discovery and publication algorithm

Onboarding runs once per batch before any worker launches, after resuming any pending `migrations.json` intents (and refusing to onboard if pending intents remain, matching `repo_mirror/forgejo_schedule.py:289-304`). It requires a configured authoritative store (`store_from_environment` in `repo_mirror/gitlab_onboarding_github.py:11-24` on GitHub; `--config-repo` or `--forgejo-config-repo`/`--forgejo-config-id` on Forgejo, `repo_mirror/forgejo_schedule.py:131,301-313`, `repo_mirror/forgejo_migration.py:412-475`) and verifies authoritative bytes match the loaded configuration before any writes (`repo_mirror/cli.py:237-238`, `repo_mirror/gitlab_onboarding_forgejo.py:96-97`).

For each `auto_discoveries` rule (in order):

1. **Resolve and verify owner identity:** Fetch owner metadata from the origin-restricted source client and verify positive numeric `id`, canonical login/path, and public visibility where required:
   - *GitHub:* `GET /users/{owner}` (covers users and organizations; new for `auto_discoveries`, not yet in `repo_mirror/sources.py`). List via `/orgs/{owner}/repos?type=all` for `type == "Organization"` or `/users/{owner}/repos?type=owner` for `type == "User"` (current `repo_mirror/sources.py:131` only calls `/users/{owner}/repos`; `/orgs/...` parity with Manobot private access is unverified live).
   - *Forgejo:* `GET /api/v1/users/{owner}` or `/api/v1/orgs/{owner}` anonymously, then `/api/v1/orgs/{owner}/repos` or `/api/v1/users/{owner}/repos` (organization endpoint behavior is unverified live against Forgejo).
   - *GitLab:* For groups, `GET /api/v4/groups/{quote(path, safe='')}` verifying `id`, `visibility == "public"`, and `full_path == path` (`repo_mirror/gitlab_onboarding_forgejo.py:102-105`), then `/projects?include_subgroups=false&with_shared=false` (`repo_mirror/sources.py:97`, `repo_mirror/gitlab_source_adapter.py:44-45`). For GitLab user namespaces (new, unverified live), resolve `GET /api/v4/users?username={owner}` and list `GET /api/v4/users/{id}/projects`, keeping `visibility == "public"` projects.
2. **Paginate and validate upstream records:** Fetch all pages up to the 10,000-page safety limit (`repo_mirror/github_api.py:445`, `repo_mirror/gitlab_onboarding_forgejo.py:38`, `repo_mirror/gitlab_source_adapter.py:49`). Validate every item's repository name (`NAME`, not `.`/`..`), positive integer `id`, canonical path, and HTTPS clone URL (`validate_source_clone_url`, `repo_mirror/repository.py:216-257`), rejecting duplicate names or IDs within the listing (`repo_mirror/sources.py:115`).
3. **Apply the zero-discovery guard:** On raw upstream results before `exclude`, if the rule has existing `discovered_by` children and upstream returned zero repositories, fail the rule closed (see [Failure modes and manual reconciliation](#failure-modes-and-manual-reconciliation)).
4. **Match against existing entries by ID first:** Discard names in `exclude`. For each remaining `(repo_name, source_id)`, check existing `repositories[]` by `(origin.lower(), source_id)` first:
   - If matched by `(origin.lower(), source_id)`: treat as already tracked (even if renamed upstream; the worker's relocation transaction will update it). Verify no conflicting URL or `discovered_by` mismatch.
   - Else if matched by URL: fail closed if `source.id` differs or is missing.
   - Else: queue for induction.
5. **Pre-validate planned inductions:** Apply `overrides[repo]`, compute destination name (`<destination_name_prefix>-<repo>` if not overridden, enforcing `len <= 100` and `NAME`), build candidate `repositories[]` entries with `discovered_by: rule.url`, validate the projected full configuration for schema and case-insensitive destination-name / ID collisions (`repo_mirror/gitlab_onboarding_forgejo.py:124-130`), and verify each planned destination currently returns HTTP 404 (`repo_mirror/gitlab_onboarding_forgejo.py:131-138`).
6. **Create missing destinations:** For each planned repository, create the destination (`private` unless overridden/configured `public`, `auto_init: False`, `description: marker + " [repo-mirror:initializing:v1]"`), disable Actions and verify (`PUT /actions/permissions` + readback on GitHub, `repo_mirror/adhoc.py:286-320`; `has_actions: False` + readback on Forgejo, `repo_mirror/gitlab_onboarding_forgejo.py:151-158`, `repo_mirror/forgejo_git.py:168-197`), and re-verify source and destination IDs.
7. **Publish and read back once per rule:** Append the rule's newly inducted `repositories[]` entries (and the rule `id`, when it was not already present) and perform **one byte-conditional CAS write** to the authoritative config (`GitHubConfigFile.publish` / `GitHubConfigCAS.replace` / `ForgejoFile.replace`), then read back and verify exact byte equality before advancing (`repo_mirror/gitlab_onboarding_forgejo.py:165-169`, `repo_mirror/github_migration.py:85-104`, `repo_mirror/forgejo_control_config.py:98-116`).

Workers then run against the published revision. In `.github/workflows/mirror.yml:203-207,302-326`, `Refresh failure journal` checks `config.json` before `Mirror`, and `Save failure journal` updates only `failures.json` on top of freshly fetched `origin/main`, so Contents API `config.json` writes during onboarding do not conflict with saving `failures.json`.

## Steady state

When discovery finds no unseen repositories and no owner `id` needs pinning, no destination API writes or config publications occur (authoritative bytes are still rechecked, `repo_mirror/gitlab_onboarding_forgejo.py:161-164`). Existing entries execute as ordinary pinned mirrors.

### Leaving a rule

1. **Deleted upstream:** The repository disappears from upstream discovery. Onboarding keeps its `repositories[]` entry and destination untouched and records it as missing upstream. During the mirror phase, the worker fails on upstream 404; the run summary emits a `missing_upstream` / inactive-style diagnostic line alongside the error report. *(Implementation note: verify in follow-up PR whether a confirmed-deleted `discovered_by` child should still fail the batch exit status or downgrade to an inactive-style warning.)*
2. **Transferred or renamed upstream:** Because onboarding matches existing entries by `(origin.lower(), source_id)` first, an upstream rename or transfer is skipped by onboarding and handled by the existing post-batch relocation transaction (`repo_mirror/repository.py:309-348`, `repo_mirror/migrations.py`, `repo_mirror/forgejo_sync.py:552-577,619-639`, `repo_mirror/forgejo_automatic_transition.py`). That path requires:
   - named config (not `repos: "all"`), pinned `source.id` and `mirrors.<profile>.id`,
   - durable migration store + `MIRROR_TIMESTAMP_SECRET` / `--timestamp-secret-file` (>= 32 bytes),
   - same-origin metadata resolution returning the unchanged `source.id` under the new `full_name` / `clone_url`.
   - *Current code gaps to close during implementation:* (a) `destination_plan` (`repo_mirror/migrations.py:122-141`) only renames destinations matching `mirror-<old_owner>-<old_repo>`, and `prepare_source_transition` (`repo_mirror/forgejo_automatic_transition.py:46-49`) only renames destinations matching `<old_repo>`; both must recognize `<rule.destination_name_prefix>-<old_repo>` on same-owner renames (falling back to preserving custom names, and keeping the existing destination name on cross-owner transfers unless default `mirror-<old_owner>-<old_repo>` matches). (b) Currently GitHub runner relocation covers `source["type"] == "github"` only (`repo_mirror/repository.py:314`), Forgejo covers GitHub and Forgejo (`repo_mirror/forgejo_sync.py:619-639`), and GitLab has no automatic relocation on either runner (`repo_mirror/repository.py:297-304`, `repo_mirror/forgejo_sync.py:612-618`).
3. **Newly excluded (`remove_from_config` on a child):** The child's `repositories[]` entry is removed, the repository basename is appended to the owning rule's `exclude` (located via `discovered_by`; any matching `overrides` key is removed), and the remote destination is left untouched.

## Failure modes and manual reconciliation

Any failure during destination creation, Actions disabling, post-creation readback, or CAS config publication stops the batch closed with no automatic rollback or deletion (`PartialOnboarding` in `repo_mirror/gitlab_onboarding_forgejo.py:57-60,170-173`; `OnboardingPublicationError` in `repo_mirror/gitlab_onboarding_github.py:27-28,55-57`). Failed onboarding can leave newly created remote repositories without published pins; on the next run, onboarding refuses to adopt those unpinned destinations. Operators must inspect the authoritative config and every attempted destination (ID, owner, marker, visibility, Actions) and reconcile or manually remove verified partial repositories before retrying (`README.md:152,292`).

### Summary and diagnostics

- **Discovery failures never look empty:** A failed rule lookup or pagination error never emits `discovered=0` and fails the batch (`auto_discovery <i>/<n> (<safe_label(owner)>): FAILED: <safe_reason>`).
- **Rule status line:** Each completed rule logs `auto_discovery <i>/<n> (<safe_label(owner)>): OK discovered=<N> inducted=<M> excluded=<E> tracked=<T>` (with `missing_upstream=<K>` if `K > 0`).
- **Zero-discovery fail-closed guard:** If at least one entry in `repositories[]` has `discovered_by == rule.url` and raw upstream discovery (before `exclude`) returns `0` repositories, the rule fails closed with `"auto-discovery returned zero repositories for a rule with existing entries"`, protecting against silent upstream/API truncation.
- **Orphaned-destination report:** After successful discovery for a rule, the summary lists any `discovered_by` child absent from upstream discovery (`orphaned child repository <safe_label(owner)>/<safe_label(name)>: destination <safe_label(dest)> untouched`), plus any retained legacy `gitlab-<24hex>` entries no longer backed by a rule. Untracked destinations left behind after `remove_from_config` are not enumerated during normal runs (runners do not list all repos on the destination account) and require an out-of-band account audit.

## Security

- **Owner identity and login reuse:** If an account/group login is deleted or renamed and re-registered by another actor on the same forge, a name-only rule would silently mirror the new actor's repositories. Pinning the numeric owner/group `id` ensures a login swap fails closed (`source owner identity mismatch`); see [Owner identity pinning](#owner-identity-pinning-decided). Pin checks and creation/Actions requests remain separate HTTP calls and are not atomic against concurrent replacement (`README.md:95,291-292`).
- **Destination name collisions:** Before creating any destination, onboarding checks case-insensitive uniqueness across all existing, legacy (`mirrors.<profile>.legacy`), and newly planned destination names, verifies 404 remotely, and rejects names over 100 chars or outside `NAME`.
- **Origin and credential boundaries:** `SourceAPI.restrict_origin`, `GitLabSourceAdapter`, and `PublicForgejoSourceAdapter` prevent cross-origin redirects and credential leakage (`repo_mirror/github_api.py:341-353,583-597`, `repo_mirror/gitlab_source_adapter.py:105-121`, `repo_mirror/forgejo_source_adapter.py:110-116`). On Forgejo, `visibility: "public"` forces anonymous GitHub source reads and verifies `private is False` (`repo_mirror/forgejo_sync.py:640-656`). Log output uses `safe_label` and fixed error strings, never printing URLs or raw exceptions (`repo_mirror/logging_utils.py:21-63,264-341`).

## Workflow interface

Both `.github/workflows/mirror.yml:6-45` and `scheduler/forgejo-actions.yml:6-46` (`input_01_*`..`input_06_*` -> `FORGEJO_MANUAL_INPUTS`, `repo_mirror/forgejo_dispatch.py:14-15`) accept `source_urls` containing owner URLs or repository URLs. On GitHub, `.github/workflows/mirror.yml:112-154` runs `repo_mirror/adhoc.py` in the `configure` job to write config changes before `mirror` checks out `config-sha`; on `add_to_config=true` for an owner URL, `configure` writes the `auto_discoveries` rule and `mirror` performs discovery, destination creation, and pin publication before workers start. On Forgejo, `repo_mirror/forgejo_schedule.py:251-278` runs `prepare_manual` inline and should likewise run rule onboarding after publishing the `add_to_config` rule change.

### Owner URL vs repository URL parsing

Every URL must be credential-free HTTPS with no query, fragment, `.git` suffix, or trailing slash:
- **GitHub and Forgejo:** 1 path segment (`https://host/owner`) parses as an owner URL; 2 segments (`https://host/owner/repo`) parse as a repository URL; any other depth is invalid.
- **GitLab (`gitlab.com` or configured self-hosted GitLab origins):** 1 path segment parses as an owner URL (user namespace). Because GitLab groups can nest (`team/sub`), a path with `>= 2` segments is first matched against configured `auto_discoveries[]` and `repositories[]` entries: matching both fails closed as ambiguous. If unconfigured on `gitlab.com`, a read-only `GET /api/v4/groups/{path}` vs `GET /api/v4/projects/{path}` probe disambiguates group vs project; unconfigured self-hosted GitLab URLs remain rejected (`README.md:53`).

### Option semantics and error messages

| Input | Owner URL (`auto_discoveries` rule) | Repository URL (`repositories[]` entry) |
| --- | --- | --- |
| `source_urls` (run filter) | Runs onboarding for that rule and mirrors its `discovered_by` children. Rejects unconfigured owner URLs (no one-time owner mirror). | Mirrors that repository (one-time if unconfigured, or pinned entry). |
| `add_to_config=true` | Adds rule to `auto_discoveries` (`destination_name` sets `destination_name_prefix` on a single new rule; `visibility` / `ignore_releases` set rule defaults). Mirror phase runs onboarding. | Adds pinned `repositories[]` entry (`destination_name` sets `mirrors.<profile>.name`). |
| `remove_from_config=true` | Removes rule from `auto_discoveries` and removes all `repositories[]` entries with `discovered_by == rule.url`; destinations untouched; skips mirror job. | If standalone, removes entry; if `discovered_by` child, removes entry and appends repo name to owning rule's `exclude` (removing any matching `overrides` key). Destinations untouched; skips mirror job. |

Fixed safe error strings (never echoing URLs):
- URL matches both an `auto_discoveries` rule and a `repositories[]` entry: `"ambiguous source URL matches both an auto-discovery rule and a repository entry"`.
- `source_urls` selects both an owner URL and one of its children in the same dispatch: `"overlapping auto-discovery rule and child repository URL"`.
- Unconfigured owner URL with `add_to_config=false`: `"one-time mirroring is not supported for owner URLs; enable add_to_config"`.
- `destination_name` supplied for an already-configured owner rule with a different prefix: `"cannot change destination_name_prefix on an existing auto-discovery rule"`.
- `remove_from_config` on an unconfigured owner or repository URL: `"cannot remove an unconfigured auto-discovery rule or repository"`.

## Runner differences

| Aspect | GitHub runner (`mirror.py` / `repo_mirror/cli.py`) | Forgejo scheduler (`repo_mirror/forgejo_schedule.py` + `scheduler/auto_update.py`) |
| --- | --- | --- |
| Config file | `config.json` in `MIRROR_CONFIG_REPOSITORY` (`main`) | `config-forgejo.json` (`--config-repo`), or `config.json` (`--forgejo-config-repo` + `--forgejo-config-id`) |
| Authority / CAS | `GitHubConfigFile` via `MIRROR_MANAGEMENT_REPOSITORY` + `MIRROR_MANAGEMENT_TOKEN` | `GitHubConfigCAS` (`--config-repo`) or `ForgejoFile` (`--forgejo-config-repo`) |
| Destination creation | `GitHubAPI` (`PUT /actions/permissions`) | `ForgejoDestination` (`has_actions: False` at `POST`) |
| GitLab asset limit | Fixed `MAX_ASSET_SIZE` (`2 GiB - 1`, `repo_mirror/constants.py:9`) | Requires `--asset-max-bytes` <= `2147483647` (`repo_mirror/forgejo_schedule.py:149,177-179`) |
| Pre-exec launcher | None (runs directly from workflow checkout) | Standalone `scheduler/auto_update.py` validates manifest before launching fetched scheduler |
| Public source rule | Mirrors public/installation-visible sources | `visibility: "public"` enforces anonymous public source (`repo_mirror/forgejo_sync.py:640-656`) |

## Migration from `groups`

`auto_discoveries` supersedes the Forgejo-only `groups` block (`repo_mirror/gitlab_onboarding_forgejo.py:63-84`). Under the merged schema, GitLab groups use `auto_discoveries` with `"type": "gitlab"` and required `"id"` verified against `GET /api/v4/groups/{path}` (`id`, `visibility == "public"`, `full_path == path`, `repo_mirror/gitlab_onboarding_forgejo.py:102-105`):

```json
// Before
"groups": [
  {"type": "gitlab", "base_url": "https://gitlab.com", "owner": "team/sub", "id": 7, "repos": "all"}
]

// After
"auto_discoveries": [
  {"url": "https://gitlab.com/team/sub", "type": "gitlab", "id": 7}
]
```

**Compatibility window:** Release 1 accepts either `groups` (emitting a deprecation warning) or `auto_discoveries`, rejecting manifests that specify both. Release 2 (after operator manifests migrate) removes `groups` parsing from `repo_mirror/gitlab_onboarding_forgejo.py`, `repo_mirror/forgejo_schedule.py`, `repo_mirror/forgejo_dispatch.py`, and `scheduler/auto_update.py`.

**Existing `gitlab-<24hex>` entries:** Current `groups` onboarding generates destination names `gitlab-` + 24 hex chars (`repo_mirror/gitlab_onboarding_forgejo.py:109-110`) and skips scheduling them if `groups` is removed (`repo_mirror/forgejo_schedule.py:171-176`). Under `auto_discoveries`, new inductions use `<destination_name_prefix>-<repo>` (default `mirror-team-sub-<repo>`), and `gitlab-<24hex>` remains forbidden for new entries (`repo_mirror/forgejo_dispatch.py:17`). When migrating an existing `groups` manifest, operators have two paths:
1. **Retain existing `gitlab-<24hex>` destinations:** Add `"discovered_by": "https://gitlab.com/team/sub"` to each existing `gitlab-<24hex>` entry in `repositories[]` so it remains scheduled without renaming the destination; newly added projects in `team/sub` receive `<prefix>-<repo>` names.
2. **Retire `gitlab-<24hex>` entries:** Leave old `gitlab-<24hex>` entries without `discovered_by` (retained in config, not scheduled per `repo_mirror/forgejo_schedule.py:171-176`), or perform a reviewed manual destination rename to `<prefix>-<repo>`.

## Removal of the legacy format

Behaviour below is described against `origin/main` (`9b4b4bf`; note local `main` lags at `3d70e41` while `HEAD` is at `origin/main`). Removing `sources`, `repos: "all"`, `destination_account_type`, `prefix`, and per-repository `destinations` mappings retires:

- **Code files:**
  - `repo_mirror/config_named.py:7-8` (pass-through when `'destinations'`/`'repositories'` absent) and legacy `'destination_account_type'` output shape (`repo_mirror/config_named.py:59`).
  - `repo_mirror/sources.py:21,24-54,122-143` (`validate_source` legacy fields `prefix`, `destinations`, `repos: "all"`; `discover()` HTTP listing; legacy `destination_name()` fallback).
  - `repo_mirror/adhoc.py:134-182,333-335,366-383,402-404,475-479` (legacy `sources` branches in `add_to_config`, `add_to_config_pinned`, `remove_from_config`, `one_time_sources`, `main`).
  - `repo_mirror/cli.py:82,90-94,150-193,225-238,276-322,447-455` (legacy config branch, `uncertain_prefixes`, legacy `repos: "all"` GitLab onboarding, non-named migration deferral).
  - `repo_mirror/gitlab_onboarding_github.py:31-58` (`publish_binding` mutating `config['sources'][source_index]['destinations']`).
  - `repo_mirror/repository.py:314` (`source['repos'] == 'all'` guard) and `repo_mirror/forgejo_schedule.py:140-144` / `scheduler/auto_update.py:475-479` (legacy `sources` GitLab rejection).
  - `repo_mirror/logging_utils.py:112-115,155-156` and `README.md:65,91,112-125,209` (legacy error strings and documentation).
  - *(Note: Forgejo's flat URL-array manifest at `repo_mirror/forgejo_sync.py:188-191` is a separate legacy format unaffected by these keys; decide separately whether to retain or retire it.)*
- **Test files:** Dedicated legacy/group suites (`tests/test_gitlab_group_github.py`, `tests/test_gitlab_onboarding.py`, `tests/test_gitlab_group_forgejo.py`, `tests/test_gitlab_readme.py`, `tests/test_discovery_resilience.py`, `tests/test_legacy_relocation_binding.py`, `tests/test_mirror.py`) plus 33 test files using `{"destination_account_type", "sources"}` fixtures for orthogonal features (`test_adhoc_autopin_more.py`, `test_adhoc_sources.py`, `test_autopin_forgejo_redirect.py`, `test_cli_automatic_move.py`, `test_cli_relocation.py`, `test_concurrent_mirroring.py`, `test_config_duplicate_keys.py`, `test_destination_profiles.py`, `test_diagnostics_integration.py`, `test_failure_journal.py`, `test_github_account_type_guard.py`, `test_github_entrypoint_safety.py`, `test_github_metadata_redirect_policy.py`, `test_github_migration_entrypoint.py`, `test_gitlab_github_cli.py`, `test_gitlab_source_adapter.py`, `test_host_limits.py`, `test_http_diagnostics.py`, `test_ignore_releases.py`, `test_inactive_activity.py`, `test_log_secrets.py`, `test_named_github_config.py`, `test_observability.py`, `test_progress.py`, `test_progress_regressions.py`, `test_release_resume.py`, `test_source_auth.py`, `test_starter_asset.py`, `test_token_refresh.py`, `test_workflow_inputs.py`, plus the 3 dedicated ones above) and legacy `destinations` map fixtures in 18 test files (`test_automatic_source_identity.py`, `test_github_personal_auth_binding.py`, `test_github_release_identity_guard.py`, `test_github_relocation_security.py`, `test_github_repository_pins.py`, `test_migration_attestation.py`, `test_migration_evidence_required.py`, `test_personal_owner_guard.py`, `test_release_failure_identity.py`, etc.).

**Preserved behaviours and migration path:** Organization-account destinations, custom names, `ignore_releases`, per-repository overrides, ID pins, and `legacy` relocation bindings are all preserved in named configuration (`destinations.<profile>.account_type` must be retained or added to explicit `destination_profile`, because explicit profiles currently force `personal` at `repo_mirror/config_named.py:59` and `repo_mirror/forgejo_schedule.py:166-167`). Operators on legacy configs migrate by independently verifying `source.id` and `mirrors.<profile>.id` and applying the mapping below before cutover:

| Legacy field | Named / `auto_discoveries` target | Notes |
| --- | --- | --- |
| `destination_account_type` | `destinations.<profile>.account_type` | Preserves `"personal"` or `"organization"` |
| `sources[]` (`repos: [...]`) | `repositories[]` | One entry per repo; verify and record `source.id` and `mirrors.<profile>.id` |
| `sources[]` (`repos: "all"`) | `auto_discoveries[]` | Set `url` (`<base_url>/<owner>`), optional `type`, and `id` |
| `sources[].prefix` | `auto_discoveries[].destination_name_prefix` | Legacy concatenated `prefix + name` (`repo_mirror/sources.py:140`); `auto_discoveries` formats `<prefix>-<repo>` — strip trailing `-` or keep explicit `repositories[]` entries |
| `sources[].destinations.<repo>` | `mirrors.<profile>` or `auto_discoveries[].overrides.<repo>` | Preserves `name`, `visibility`, `ignore_releases`, `source_id`, `destination_id`, `legacy` |

## Test plan

- **Update existing suites:** Convert legacy/group test modules and fixtures listed in [Removal of the legacy format](#removal-of-the-legacy-format) to named config + `auto_discoveries`, and update schema/launcher/dispatch tests (`tests/test_named_github_config.py`, `tests/test_workflow_inputs.py`, `tests/test_adhoc_sources.py`, `tests/test_forgejo_dispatch.py`, `tests/test_forgejo_schedule.py`, `tests/test_forgejo_auto_update.py`, `tests/test_forgejo_local_config_launcher.py`).
- **Add `auto_discoveries` test coverage:**
  - Schema validation: unknown keys, empty `destination_name_prefix`, 100-char destination-name limit, case-insensitive collisions, `exclude`/`overrides` overlap, `discovered_by` orphan rejection, and `normalize_config` / `normalize_named` / `rewrite_config` preservation.
  - Onboarding CAS & fail-closed guarantees: single CAS write + readback per rule, partial creation raising `PartialOnboarding` without rollback, refusal to adopt unpinned destinations, zero-discovery guard when `discovered_by` children exist, and owner `id` mismatch refusal.
  - Workflow UX & steady state: owner vs repo URL parsing (including nested GitLab paths and ambiguity errors), `add_to_config` / `remove_from_config` on owner and child URLs (`exclude` append), ID-first matching across an upstream rename, and `groups` -> `auto_discoveries` compatibility + `gitlab-<24hex>` retention.

## Owner identity pinning (decided)

Every rule pins the numeric upstream owner/group identity, on the same footing as repository IDs: the identity is configuration, not a convention.

- **GitLab groups:** `id` is required in config, as today. A run verifies `id`, `full_path` and `visibility` against `GET /api/v4/groups/{path}` before discovery.
- **GitHub, Forgejo and GitLab user namespaces:** `id` may be omitted in the operator's config. The first successful discovery verifies the owner metadata, pins `"id"` into the rule, and publishes it in the **same byte-conditional write** that publishes the rule's repository pins, with readback. Every later run compares the live owner identity against the pinned value and fails closed (`source owner identity mismatch`) before any destination creation or publication.
- **Why:** a login is a name that can be freed, and a rule keyed only by `url` mirrors whoever holds the name at run time. Pinning the id turns a silent takeover into a failed run.
- **Accepted residual risk:** the first run of a rule without `id` trusts the owner the URL resolves to at that moment (trust on first use). This window is one run, and the value it records is published atomically with the rule's first pins, so it is visible in review. Operators who want no window at all may put `id` in the config by hand; the schema accepts it and the same verification applies.
- **Backfill:** a rule that lacks `id` on a configuration written before this feature is pinned the same way — first successful discovery, single write, readback.

## Rollout order

The currently deployed `scheduler/auto_update.py` strictly validates named manifests (`scheduler/auto_update.py:81,113,128`) and **rejects** two new keys required by this design: top-level `"auto_discoveries"` and per-repository `"discovered_by"`.

Deploy in order, stopping writers between stages:

1. **Forgejo launcher first:** Publish scheduler/validator code supporting `auto_discoveries` and `discovered_by` (plus the `groups` deprecation warning) without modifying `config-forgejo.json` / `config.json` yet. Review and reinstall `scheduler/auto_update.py` outside the checkout (`scheduler/README.md:68-82`) and verify one run against the unchanged manifest.
2. **GitHub runner:** Merge `auto_discoveries` support to `main` on the code repository while `config.json` still uses explicit `repositories[]` (or legacy format during the compatibility step, if staged).
3. **Manifest migration:** Stop schedules/drain writers, convert `groups` and legacy `sources` manifests to `auto_discoveries` + `repositories[]`, run a manual pilot, verify pins and `discovered_by`, then re-enable schedules.
4. **Cleanup PR (after cutover):** Remove `groups` compatibility and the legacy `sources`/`destination_account_type`/`prefix` code paths, then review and reinstall `scheduler/auto_update.py` once more.
