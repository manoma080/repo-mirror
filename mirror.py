"""Command-line entry point for the repository mirror.

The implementation lives in the ``repo_mirror`` package. This file only
starts it, so ``python mirror.py`` keeps working and older imports still
resolve.
"""

import http.client  # noqa: F401  (re-exported for tests)
import socket  # noqa: F401
import ssl  # noqa: F401
import subprocess  # noqa: F401
import sys
import time  # noqa: F401

from repo_mirror.cli import main
from repo_mirror.constants import GITHUB, LOG_SECRETS, MAX_ASSET_SIZE, NAME, REPOSITORY_TIME
from repo_mirror.git_sync import mirror_git
from repo_mirror.github_api import (
    API,
    AppTokenProvider,
    SafeRedirect,
    SourceAPI,
    copy_asset,
)
from repo_mirror.journal import FailureJournal, ReleaseRecoveryError, StarterAssetError
from repo_mirror.logging_utils import error_detail, safe_label
from repo_mirror.repository import mirror_repository
from repo_mirror.sources import (
    check_existing,
    destination_name,
    discover,
    get_existing,
    source_marker,
    validate_source,
)

__all__ = [
    "API",
    "GITHUB",
    "LOG_SECRETS",
    "MAX_ASSET_SIZE",
    "NAME",
    "REPOSITORY_TIME",
    "AppTokenProvider",
    "FailureJournal",
    "ReleaseRecoveryError",
    "SafeRedirect",
    "SourceAPI",
    "StarterAssetError",
    "check_existing",
    "copy_asset",
    "destination_name",
    "discover",
    "error_detail",
    "get_existing",
    "main",
    "mirror_git",
    "mirror_repository",
    "safe_label",
    "source_marker",
    "validate_source",
]

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Avoid printing subprocess stderr, URLs, response bodies or credentials.
        detail = error_detail(exc)
        print("mirror failed: " + detail, file=sys.stderr, flush=True)
        sys.exit(1)
