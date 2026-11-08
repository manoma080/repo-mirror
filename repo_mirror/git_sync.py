"""git_sync: see README for an overview."""

import base64
import os
import subprocess
import tempfile


def mirror_git(source_url, destination_url, token, after_push=None):
    # Git is isolated from user/system configuration and never receives the token
    # while contacting the source. Hooks are disabled and no checkout occurs.
    """Copy every branch and tag from the source Git repo to the destination, using a token-safe push."""
    with tempfile.TemporaryDirectory(prefix="repo-mirror-") as tmp:
        env = os.environ.copy()
        env.pop("GH_TOKEN", None)
        env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "",
                "GIT_LFS_SKIP_SMUDGE": "1",
            }
        )
        for key in list(env):
            if key.startswith("GIT_") and key not in (
                "GIT_CONFIG_NOSYSTEM",
                "GIT_CONFIG_GLOBAL",
                "GIT_TERMINAL_PROMPT",
                "GIT_ASKPASS",
                "GIT_LFS_SKIP_SMUDGE",
            ):
                del env[key]

        def run(args, config_env=None):
            e = env.copy()
            if config_env:
                e.update(config_env)
            result = subprocess.run(
                ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "protocol.file.allow=never", *args],
                env=e,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            return result.stdout

        run(["init", "--bare", tmp])
        run(
            [
                "-C",
                tmp,
                "fetch",
                "--no-tags",
                "--force",
                source_url,
                "+refs/heads/*:refs/heads/*",
                "+refs/tags/*:refs/tags/*",
            ]
        )
        if not run(["-C", tmp, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags"]):
            return False

        def credentials():
            current = token() if callable(token) else token
            auth = base64.b64encode(("x-access-token:" + current).encode()).decode()
            return {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + auth,
            }

        refspecs = ["+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"]
        run(["-C", tmp, "push", "--force", destination_url, *refspecs], credentials())
        if after_push:
            after_push()
        run(["-C", tmp, "push", "--force", "--prune", destination_url, *refspecs], credentials())
        return True
