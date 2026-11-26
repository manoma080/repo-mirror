# `auto_discoveries` reference

## Summary

`auto_discoveries` is a top-level array in the named configuration (`config.json` for the GitHub runner; `config-forgejo.json` for the Forgejo scheduler under `--config-repo`, or `config.json` in a self-hosted Forgejo control repository under `--forgejo-config-repo`/`--forgejo-config-id`).
Each entry declares an upstream account or direct GitLab group whose exposed repositories are mirrored as a set, with newly added repositories onboarded automatically.

Discovery is an onboarding pass (`repo_mirror/auto_discoveries_onboarding.py`) that runs once per batch before any worker starts: it creates missing destination repositories, publishes one pinned `repositories[]` entry per discovered repository in one byte-conditional write per rule to the authoritative configuration, reads the configuration back, and then runs the mirror phase against the published revision.
Once onboarded, each discovered repository is an ordinary pinned `repositories[]` entry.
`auto_discoveries` replaced the removed legacy `sources` / `repos: "all"` format and supersedes Forgejo-only `groups`.

### Non-goals

- No include-filters (such as `include_forks`, `include_archived`, or name globs); everything the owner exposes is included by default.
- No recursive GitLab subgroup discovery or shared-project inclusion.
- No automatic deletion of destination repositories or automatic adoption of pre-existing unpinned destinations.
- No authenticated private Forgejo or private GitLab source mirroring.

## Schema

Allowed top-level keys in a named configuration are:

- `destinations` (required, exactly one profile)
- `repositories` (required array)
- `host_concurrency_limits` (optional object)
- `auto_discoveries` (optional array of rule objects)
- `groups` (deprecated optional array on Forgejo only; translated in memory into `auto_discoveries` at load time with one deprecation warning per group; duplicate declarations across `groups` and `auto_discoveries` are rejected)

Duplicate JSON keys are rejected everywhere (`repo_mirror/config_json.py`).
Unknown keys are rejected at the top level, in each `auto_discoveries[]` object, in each `overrides.<repo>` object, and in each `repositories[]` entry.
Legacy `sources` and `destination_account_type` keys are rejected on both runners.

```json
{
  "auto_discoveries": [
    {
      "url": "https://github.com/example-owner",
      "type": "github",
      "id": 123456,
      "visibility": "private",
      "ignore_releases": false,
      "destination_name_prefix": "mirror-example-owner",
      "exclude": ["some-repo"],
      "overrides": {
        "other-repo": {"visibility": "public", "ignore_releases": true}
      }
    }
  ]
}
```

Allowed keys inside each `auto_discoveries[]` object are `url`, `type`, `id`, `visibility`, `ignore_releases`, `destination_name_prefix`, `exclude`, and `overrides`:

- `url` (required string): credential-free HTTPS owner/group URL (no query, fragment, `.git`, trailing slash, whitespace/control characters, or `.`/`..` segments; each path segment matches `NAME` in `repo_mirror/constants.py`; Forgejo hosts follow `repo_mirror/forgejo_local.py`).
- `type` (optional string): `"github"`, `"gitlab"`, or `"forgejo"`. Optional for `github.com` (must be `"github"` if given) and `gitlab.com` (must be `"gitlab"` if given); required as `"gitlab"` for self-hosted GitLab instances, otherwise non-`github.com`/non-`gitlab.com` hosts default to `"forgejo"`. `"github"` is rejected for non-`github.com` hosts.
- `id` (positive JSON integer, rejecting booleans): the pinned upstream owner/group identity, always enforced once set. **Required in config for every GitLab `auto_discoveries` rule** (including single-segment GitLab user namespaces, enforced in `repo_mirror/config_named.py`, `repo_mirror/auto_discoveries_onboarding.py`, and `scheduler/auto_update.py`). Optional for GitHub and Forgejo rules, where the first successful discovery pins it before any mirror work (see [Owner identity pinning](#owner-identity-pinning)).
- `visibility` (optional string, default `"private"`): `"private"` or `"public"`. Applies only when creating a destination; on Forgejo, `"public"` forces anonymous GitHub source reads and requires a public source.
- `ignore_releases` (optional JSON boolean, default `false`): non-booleans rejected. Applied to inducted entries unless overridden.
- `destination_name_prefix` (optional string): defaults to `mirror-<owner>` (with `/` replaced by `-` for nested GitLab group paths). Empty `""` is rejected; must match `NAME`, not be `.` or `..`, and not match reserved `gitlab-[0-9a-f]{24}`. Default inducted destination name is `<destination_name_prefix>-<repo>`, enforcing `len <= 100` and `NAME` at induction.
- `exclude` (optional array of strings, default `[]`): repository basenames matching `NAME` and not `.`/`..`, with no case-insensitive duplicates. Suppresses auto-discovery for those names; explicit `repositories[]` entries without `discovered_by` still win.
- `overrides` (optional object): maps repository basename (matching `NAME`, no case-insensitive duplicate keys, disjoint from `exclude`) to a non-empty subset of `{"name", "visibility", "ignore_releases"}`. `name` must match `NAME`, `len <= 100`, not `.`/`..`, and not match reserved `gitlab-[0-9a-f]{24}` for new entries. `overrides` values apply at induction; changing `overrides` later never silently renames or changes visibility of an already-pinned destination.
- Rule uniqueness: duplicate `(origin.lower(), owner.lower())` or duplicate `(origin.lower(), id)` (when `id` is present) are rejected.
- Case handling: host/origin is case-insensitive (`https://github.com` canonical); GitHub owner and repository names are compared case-insensitively; Forgejo and GitLab preserve exact path case in API lookups and generated `source.url`, while collision checks, `exclude`, and `overrides` keys match case-insensitively.

### Ownership back-pointer (`discovered_by`)

Each `repositories[]` entry materialized by a rule carries a top-level string field `"discovered_by"` set to the canonical `url` of the rule that produced it:

```json
{
  "source": {"url": "https://github.com/example-owner/alpha", "id": 9001},
  "mirrors": {"github": {"name": "mirror-example-owner-alpha", "id": 7001, "visibility": "private"}},
  "discovered_by": "https://github.com/example-owner"
}
```

Validators (`normalize_config` in `repo_mirror/config_named.py`, `normalize_named` in `repo_mirror/forgejo_sync.py`, `valid_manifest` in `scheduler/auto_update.py`, `plan_dispatch` in `repo_mirror/forgejo_dispatch.py`, and migration config rewrites in `repo_mirror/migrations.py` and `repo_mirror/forgejo_automatic_transition.py`) accept and preserve `discovered_by` and reject any `discovered_by` whose URL does not match a declared `auto_discoveries[].url`.
When a relocation moves a repository to a different owner, the migration rewrite drops `discovered_by` so the moved repository continues as an explicit pinned entry; same-owner renames preserve `discovered_by` and follow `<destination_name_prefix>-<new_repo>`.

## Invariants

- **Pinned steady state:** Every mirrored repository executes as a concrete `repositories[]` entry with positive integer `source.id` and `mirrors.<profile>.id`.
- **Publish before mirror:** No worker starts until onboarding publishes new bindings to the authoritative configuration and verifies the readback byte-for-byte.
- **No unpinned adoption:** A pre-existing destination repository without both saved `source.id` and `mirrors.<profile>.id` is never adopted, even if its description carries a matching source marker.
- **No automatic deletion:** Neither onboarding nor `remove_from_config` deletes remote destination repositories.
- **Complete coverage:** Every rule discovers all exposed repositories—forks, archived, empty, and all pages—without include-filters.
- **Origin and credential isolation:** Source API clients never send destination or GitHub tokens to Forgejo/GitLab or cross-origin redirects.

## Discovery and publication algorithm

Onboarding runs once per batch before any worker launches, after resuming any pending `migrations.json` intents (and refusing to onboard if pending intents remain).
On Forgejo, manual dispatch skips `auto_discoveries` onboarding whenever `source_urls` selects specific repositories.
Onboarding requires an authoritative store (`store_from_environment` in `repo_mirror/github_migration.py` on GitHub; `--config-repo` or `--forgejo-config-repo`/`--forgejo-config-id` on Forgejo) and verifies authoritative bytes match the loaded configuration before any writes.

For each `auto_discoveries` rule (in order):

1. **Resolve and verify owner identity:** Fetch owner metadata from the origin-restricted source client and verify positive numeric `id`, canonical login/path, and public visibility where required:
   - *GitHub:* `GET /users/{owner}`, then list via `/orgs/{owner}/repos?type=all` for organizations or `/users/{owner}/repos?type=owner` for users.
   - *Forgejo:* `GET /api/v1/users/{owner}` or `/api/v1/orgs/{owner}` anonymously, then `/api/v1/orgs/{owner}/repos` or `/api/v1/users/{owner}/repos`.
   - *GitLab:* Requires `id` in config. Resolves group (`GET /api/v4/groups/{quote(path, safe='')}` verifying `id`, `visibility == "public"`, `full_path == path`, then `/projects?include_subgroups=false&with_shared=false`) or user namespace (`GET /api/v4/users?username={owner}`, then `/api/v4/users/{id}/projects`, keeping `visibility == "public"` projects).
2. **Paginate and validate upstream records:** Fetch all pages up to the 10,000-page safety limit. Validate every item's repository name (`NAME`, not `.`/`..`), positive integer `id`, canonical path, and HTTPS clone URL, rejecting duplicate names or IDs within the listing.
3. **Apply the zero-discovery guard:** On raw upstream results before `exclude`, if the rule has existing `discovered_by` children and upstream returned zero repositories, fail the rule closed.
4. **Match against existing entries by ID first:** Discard names in `exclude`. For each remaining `(repo_name, source_id)`, check existing `repositories[]` by `(origin.lower(), source_id)` first:
   - **Matched by `(origin.lower(), source_id)`:** Treat as already tracked (even if renamed upstream; the worker's relocation transaction updates it). Verify no conflicting URL or `discovered_by` mismatch.
   - **Matched by URL with a differing `source.id`:**
     - *GitHub runner, `github` `discovered_by` entry with a derived destination name and pinned destination ID:* Onboarding checks `GET /repositories/{pinned_id}`. If GitHub confirms HTTP 404 (upstream repository was deleted and recreated at the same URL), onboarding archives (renames) the old mirror and adopts the recreated repository under the new `source.id` and destination ID.
     - *All other ID mismatches (Forgejo runner, non-GitHub rules, standalone entries, custom destination names, unpinned destinations, or upstream still alive/indeterminate):* Onboarding records a per-repository `IdentityConflict`, skips mirroring that repository, and continues the rest of the batch rather than aborting it.
     - Missing or invalid `source.id` on an existing matched entry fails closed.
   - **Unmatched:** Queue for induction.
5. **Pre-validate planned inductions:** Apply `overrides[repo]`, compute destination name (`<destination_name_prefix>-<repo>` if not overridden, enforcing `len <= 100` and `NAME`), build candidate `repositories[]` entries with `discovered_by: rule.url`, validate the projected full configuration for schema and case-insensitive destination-name / ID collisions, and verify each planned destination currently returns HTTP 404.
6. **Create missing destinations:** For each planned repository, create the destination (`private` unless overridden/configured `public`, `auto_init: False`, initializing description marker), disable Actions, then read the repository back to confirm its identity.
7. **Publish and read back once per rule:** Append the rule's newly inducted `repositories[]` entries (and the rule `id`, when it was not already present) and perform **one byte-conditional CAS write** to the authoritative config (`GitHubConfigFile.publish` / `GitHubConfigCAS.replace` / `ForgejoFile.replace`), then read back and verify exact byte equality before advancing.

Workers then run against the published revision.
In `.github/workflows/mirror.yml`, `Refresh failure journal` checks `config.json` before `Mirror`, and `Save failure journal` updates only `failures.json` on top of freshly fetched `origin/main`, so Contents API `config.json` writes during onboarding do not conflict with saving `failures.json`.

## Steady state

When discovery finds no unseen repositories, no adoptions, no owner relocation, and no owner `id` needs pinning, no destination API writes or config publications occur (authoritative bytes are still rechecked).
Existing entries execute as ordinary pinned mirrors.

### Leaving a rule

1. **Deleted upstream:** The repository disappears from upstream discovery. Onboarding keeps its `repositories[]` entry and destination untouched and reports it as an orphaned child. During the mirror phase, the worker fails on upstream 404.
2. **Transferred or renamed upstream:** Because onboarding matches existing entries by `(origin.lower(), source_id)` first, an upstream rename or transfer is skipped by onboarding and handled by the post-batch relocation transaction (`repo_mirror/migrations.py`, `repo_mirror/forgejo_automatic_transition.py`). Same-owner renames follow `<rule.destination_name_prefix>-<new_repo>` and preserve `discovered_by`; cross-owner transfers drop `discovered_by`. Automatic relocation currently covers GitHub sources on the GitHub runner and GitHub/Forgejo sources on the Forgejo runner (not GitLab on either runner).
3. **Newly excluded (`remove_from_config` on a child on GitHub):** The child's `repositories[]` entry is removed, the repository basename is appended to the owning rule's `exclude` (any matching `overrides` key is removed), and the remote destination is left untouched.

## Failure modes and manual reconciliation

Any failure during destination creation, Actions disabling, post-creation readback, or CAS config publication stops the batch closed with no automatic rollback or deletion (`PartialOnboarding` in `repo_mirror/auto_discoveries_onboarding.py`).
Failed onboarding can leave newly created remote repositories without published pins; on the next run, onboarding refuses to adopt those unpinned destinations.
Operators must inspect the authoritative config and every attempted destination (ID, owner, marker, visibility, Actions) and reconcile or manually remove verified partial repositories before retrying.

### Summary and diagnostics

- **Discovery failures never look empty:** A failed rule lookup or pagination error never emits `discovered=0` and fails the batch (`auto_discovery <i>/<n> (<safe_label(owner)>): FAILED: <safe_reason>`).
- **Rule status line:** Each completed rule logs:
  `auto_discovery <i>/<n> (<safe_label(owner)>): OK discovered=<N> inducted=<M> excluded=<E> tracked=<T> [missing_upstream=<K>] conflicts=<C> adopted=<A>`
- **Zero-discovery fail-closed guard:** If at least one entry in `repositories[]` has `discovered_by == rule.url` and raw upstream discovery (before `exclude`) returns `0` repositories, the rule fails closed with `"auto-discovery returned zero repositories for a rule with existing entries"`.
- **Orphaned-destination report:** After successful discovery for a rule, the summary lists any `discovered_by` child absent from upstream discovery, plus any retained legacy `gitlab-<24hex>` entries no longer backed by a rule. Untracked destinations left behind after `remove_from_config` are not enumerated during normal runs and require an out-of-band account audit.

## Security

- **Owner identity and login reuse:** Pinning the numeric owner/group `id` ensures a login swap fails closed (`source owner identity mismatch`). Pin checks and creation/Actions requests remain separate HTTP calls and are not atomic against concurrent replacement.
- **Destination name collisions:** Before creating any destination, onboarding checks case-insensitive uniqueness across all existing, legacy (`mirrors.<profile>.legacy`), and newly planned destination names, verifies 404 remotely, and rejects names over 100 chars or outside `NAME`.
- **Origin and credential boundaries:** `SourceAPI.restrict_origin`, `GitLabSourceAdapter`, and `PublicForgejoSourceAdapter` prevent cross-origin redirects and credential leakage. On Forgejo, `visibility: "public"` forces anonymous GitHub source reads and verifies `private is False`. Log output uses `safe_label` and fixed error strings, never printing URLs or raw exceptions.

## Workflow interface

Owner URLs in manual workflow dispatch are supported on the **GitHub runner** (`.github/workflows/mirror.yml` + `repo_mirror/adhoc.py` + `repo_mirror/cli.py`), **not** on the Forgejo runner.
Forgejo manual dispatch (`repo_mirror/forgejo_dispatch.py`) parses repository URLs only and skips `auto_discoveries` onboarding whenever `source_urls` is non-empty.

### GitHub workflow: owner URL vs repository URL parsing

Every URL must be credential-free HTTPS with no query, fragment, `.git` suffix, or trailing slash:

- **GitHub and Forgejo:** 1 path segment (`https://host/owner`) parses as an owner URL; 2 segments (`https://host/owner/repo`) parse as a repository URL; any other depth is invalid.
- **GitLab (`gitlab.com` or configured self-hosted GitLab origins):** 1 path segment parses as an owner URL (user namespace). Because GitLab groups can nest (`team/sub`), a path with `>= 2` segments is first matched against configured `auto_discoveries[]` and `repositories[]` entries (matching both fails closed as ambiguous). If unconfigured on `gitlab.com`, only a 2-segment path triggers the read-only `GET /api/v4/groups/{path}` vs `GET /api/v4/projects/{path}` probe to disambiguate group vs project, while 3+ segment `gitlab.com` paths parse straight as repositories; on an unconfigured self-hosted host, 1–2 segment URLs default to Forgejo, and only 3+ segment paths (or URLs failing that depth rule) are rejected (`repo_mirror/adhoc.py`, `tests/test_auto_discoveries_workflow.py`).

### GitHub workflow: option semantics

| Input | Owner URL (`auto_discoveries` rule) | Repository URL (`repositories[]` entry) |
| --- | --- | --- |
| `source_urls` (run filter) | Runs onboarding for that rule and mirrors its `discovered_by` children. Rejects unconfigured owner URLs (no one-time owner mirror). | Mirrors that repository (one-time if unconfigured for GitHub/Forgejo; unconfigured GitLab URLs are rejected). |
| `add_to_config=true` | Adds rule to `auto_discoveries` (`visibility` / `ignore_releases` set rule defaults; `destination_name` is **rejected** for discovery rules). Mirror phase runs onboarding. | Adds pinned `repositories[]` entry (`destination_name` sets `mirrors.<profile>.name`). |
| `remove_from_config=true` | Removes rule from `auto_discoveries`, **keeps** all child `repositories[]` entries, and strips `discovered_by` (preserving non-default mirror `name`); destinations untouched; skips mirror job. | If standalone, removes entry; if `discovered_by` child, removes entry and appends repo basename to owning rule's `exclude` (removing any matching `overrides` key). Destinations untouched; skips mirror job. |

Key safe error strings on GitHub dispatch:
- URL matches both an `auto_discoveries` rule and a `repositories[]` entry: `"ambiguous source URL matches both an auto-discovery rule and a repository entry"`.
- `source_urls` selects both an owner URL and one of its children: `"overlapping auto-discovery rule and child repository URL"`.
- Unconfigured owner URL with `add_to_config=false`: `"one-time mirroring is not supported for owner URLs; enable add_to_config"`.
- `destination_name` supplied alongside a discovery rule: `"destination name is derived per repository for a discovery rule"`.

## Runner differences

| Aspect | GitHub runner (`mirror.py` / `repo_mirror/cli.py`) | Forgejo scheduler (`repo_mirror/forgejo_schedule.py` + `scheduler/auto_update.py`) |
| --- | --- | --- |
| Config file | `config.json` in `MIRROR_CONFIG_REPOSITORY` (`main`) | `config-forgejo.json` (`--config-repo`), or `config.json` (`--forgejo-config-repo` + `--forgejo-config-id`) |
| Authority / CAS | `GitHubConfigFile` via `MIRROR_MANAGEMENT_REPOSITORY` + `MIRROR_MANAGEMENT_TOKEN` | `GitHubConfigCAS` (`--config-repo`) or `ForgejoFile` (`--forgejo-config-repo`) |
| Destination creation | `GitHubAPI` (`PUT /actions/permissions`) | `ForgejoDestination` (`has_actions: False` at `POST`) |
| GitLab asset limit | Fixed `MAX_ASSET_SIZE` (`2 GiB - 1`) | Requires `--asset-max-bytes` <= `2147483647` |
| Manual owner-URL dispatch | Supported (`add_to_config`, `remove_from_config`, run filter) | Not supported (repository URLs only) |
| Deleted-and-recreated adoption | Archives old mirror and adopts recreated `github` `discovered_by` repo with derived name | Records `IdentityConflict` and skips |
| Pre-exec launcher | None (runs directly from workflow checkout) | Standalone `scheduler/auto_update.py` validates manifest before launching fetched scheduler |
| Public source rule | Mirrors public/installation-visible sources | `visibility: "public"` enforces anonymous public source |

## Owner identity pinning

Every rule pins the numeric upstream owner/group identity, on the same footing as repository IDs:

- **GitLab (groups and user namespaces):** `id` is **required** in config up front. Every run verifies `id` and canonical path (and `visibility == "public"` for groups) before discovery.
- **GitHub and Forgejo:** `id` may be omitted in the operator's config. The first successful discovery verifies the owner metadata, pins `"id"` into the rule, and publishes it in the **same byte-conditional write** that publishes the rule's repository pins, with readback. Every later run compares the live owner identity against the pinned value and fails closed (`source owner identity mismatch`) before any destination creation or publication.
- **Residual risk:** For GitHub and Forgejo rules without an initial `id`, the first run trusts the owner the URL resolves to at that moment (trust on first use). Operators who want no TOFU window may set `id` in config by hand.

## Migration from deprecated Forgejo `groups`

`auto_discoveries` supersedes the Forgejo-only `groups` block (`repo_mirror/gitlab_onboarding_forgejo.py`):

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

Forgejo currently accepts either `groups` (translated in memory to `auto_discoveries` at load time without rewriting the config file, emitting one deprecation warning per group) or `auto_discoveries`, and rejects manifests that declare the same group in both.

**Existing `gitlab-<24hex>` entries:** Legacy `groups` onboarding generated `gitlab-` + 24 hex character destination names and skips scheduling them if `groups` is removed. Under `auto_discoveries`, new inductions use `<destination_name_prefix>-<repo>` (default `mirror-team-sub-<repo>`). When migrating an existing `groups` manifest:
1. **Retain existing `gitlab-<24hex>` destinations:** Add `"discovered_by": "https://gitlab.com/team/sub"` to each existing `gitlab-<24hex>` entry in `repositories[]` (without setting `destination_name_prefix` on the rule) so it stays scheduled without renaming; newly added projects receive `<prefix>-<repo>` names.
2. **Retire `gitlab-<24hex>` entries:** Leave old `gitlab-<24hex>` entries without `discovered_by` (retained in config, not scheduled), or perform a reviewed manual destination rename to `<prefix>-<repo>`.
