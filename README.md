# Repository mirror

Mirrors configured Git repositories to a GitHub account or organization. The Actions workflow tests the project before running hourly (at minute 17), and can also be started using **Actions → Mirror repositories → Run workflow**. It checks out only the management repository's default branch; mirrored repositories are data, not programs to run.

## Configuration

A source has `type` (`forgejo` or `github`), an HTTPS `base_url`, an `owner`, and `repos`, either a list of repository names or the string `"all"`. The default destination prefix is `mirror-OWNER-`, using the source owner; an explicit `prefix` overrides it. Set `destination_account_type` to `personal` or `organization` in configuration, or set `DESTINATION_ACCOUNT_TYPE` to override it. Replace these illustrative values before use:

```json
{
  "destination_account_type": "personal",
  "sources": [
    {"type": "forgejo", "base_url": "https://code.example.org", "owner": "alice", "repos": ["project-one"], "prefix": "forgejo-"},
    {"type": "github", "base_url": "https://api.github.com", "owner": "bob", "repos": ["project-two"], "prefix": "github-"}
  ]
}
```

For `all`, discovery enumerates repositories exposed for the specified owner; it is not a starred-repository list. Use distinct prefixes where names might collide and review the resulting destination names before enabling the workflow. Only configure sources and destinations you are authorized to mirror.

## GitHub App setup

1. Create a GitHub App and install it on the **destination** owner. Grant repository **Contents: read and write** and **Workflows: read and write** to push history containing workflow files, and **Administration: read and write** to create/manage repositories and disable Actions. The installation must have access to all destination repositories, including newly created ones; review organization policies and approval requirements.
2. In the management repository's Actions secrets, add `MIRROR_APP_ID` and `MIRROR_APP_PRIVATE_KEY` (the complete PEM private key). Set the Actions variable `MIRROR_OWNER` to the destination account or organization login. Do not commit the key or tokens.
3. Update and review `config.json`, enable Actions, and trigger the workflow manually. Inspect the run and destination repositories. The workflow generates a short-lived installation token and passes it as `GH_TOKEN`; `DESTINATION_OWNER` comes from `MIRROR_OWNER`.

Run tests locally with `python -m unittest discover -s tests -v`. With the environment variables above set, run `python mirror.py --config config.json` to mirror. A local run can modify destination repositories; use a disposable destination for initial testing.

## Operations and safety

Mirroring can overwrite destination refs; do not use an actively developed repository as a destination. A mirror is not a complete backup of issues, pull requests, settings, secrets, or other host metadata; verify release and asset coverage separately before relying on it. Protect the management repository's default branch and workflow changes: trusted maintainers can change code that runs with the App token. Review source ownership, prefixes, visibility, and access before each configuration change. Do not execute code from source mirrors or grant the App broader permissions than needed.

**Ref and asset limits:** runs force-push branches and tags, update the GitHub default branch, then prune destination branches and tags absent upstream. This deletes destination-only refs. If the source is empty, no refs are pushed or pruned; if its default branch is unavailable, pruning is refused. GitHub branch protection or rulesets can block updates or deletion, leaving a partially updated mirror. Release assets must be smaller than 2 GiB; downloads stream to temporary files and uploads stream from disk with a fixed content length. Comparing an existing asset can require temporary disk space for two copies; insufficient disk space fails the run. Forgejo release attachments are fetched as a single array because that endpoint may ignore pagination. Source asset URLs and redirects must use HTTPS and resolve exclusively to public addresses; downloads connect directly and do not use proxies. Release assets are never deleted or replaced: a same-name content mismatch stops the run for manual review. Existing mirrors must retain the exact source marker in their repository description; unmarked or differently marked repositories are rejected, including legacy mirrors. New mirrors start private; a marked mirror can later be made public. Names are preflighted for collisions before any destination modifications, but concurrent external changes can still race with a run.

Git LFS objects are separate from Git commits: mirroring Git refs alone does not guarantee LFS objects are transferred or remain accessible. Arrange and verify LFS object migration separately; LFS quotas and authentication may apply. Submodule repositories are also separate and need their own mirroring and URL handling.

GitHub may delay or drop scheduled runs under load. In a public management repository, GitHub automatically disables scheduled workflows after 60 days without repository activity; monitor Actions and [re-enable the workflow](https://docs.github.com/actions/using-workflows/disabling-and-enabling-a-workflow) when necessary. A manual run or a successful past run is not a substitute for monitoring the schedule and mirror freshness.
