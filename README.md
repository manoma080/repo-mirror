# Repository mirror

Copy public GitHub and Forgejo repositories to GitHub. A **source** is the original repository; a **mirror** is its copy.

## How it works

| Item | What happens |
| --- | --- |
| Schedule | Runs every hour at minute 17. You can also run it from **Actions → Mirror repositories → Run workflow**. |
| Branches and tags | Copies all branches and tags, including rewritten history. Deletes branches and tags from the mirror when they are deleted from the source. |
| New releases | Copies release names, descriptions, tags and uploaded files. GitHub creates its own source-code archives. |
| Existing releases | Leaves completed releases unchanged, even if their source files or descriptions change. |
| New mirrors | Creates them as private repositories and disables their GitHub Actions. You can make them public later. |
| Stars | Does not manage stars or Stars lists. |

**Use a dedicated mirror repository.** The workflow can overwrite its branches and tags and delete ones that don't exist in the source.

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

## Releases and retries

A release with no uploaded files is valid. Each uploaded file must be smaller than 2 GiB.

When a new release is being copied, its name temporarily includes a `[repo-mirror:pending:v1]` marker. If the run stops, the next run uses that marker to continue. Don't edit the marker or use it in your own release names. The original name is restored after the files are copied. Very long names may not fit and will be rejected.

The workflow saves failed uploads in `failures.json` and checks it on the next run. Entries use hashes (short identifiers made from the source details), not passwords or file names. Someone may be able to guess the original details from a hash, so don't treat them as secret.

| Situation | What happens |
| --- | --- |
| An upload succeeds on retry | Its entry is removed from `failures.json`. |
| A run is cancelled or cannot save the file | The release's pending marker still helps the next run resume. |
| Someone changes the management branch during a run | Saving the file may fail rather than overwrite their changes. Run the workflow again. |
| Branch protection blocks the workflow | Allow its GitHub token to push the failure file. |
| A source or pending marker is changed or removed | Old entries may need manual review. |

Only attempted uploads appear in the file. A repository error can prevent the workflow from reaching later releases in that repository.

A retry checks already uploaded files and copies missing ones. If the source's file list changed during an interrupted copy, the run stops for that repository and needs manual review. The mirror does not delete or replace release files. If GitHub leaves an incomplete file after a failed upload, remove that file manually before retrying. Older releases without a marker are assumed complete; check them manually if an earlier copy was interrupted.

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

To run the mirror locally, set `GH_TOKEN` to an installation token for the GitHub App and `DESTINATION_OWNER` to the account that owns the mirrors. Then run:

```sh
python mirror.py --config config.json
```

A local run changes GitHub repositories. Test with a separate destination first.
