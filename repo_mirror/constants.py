"""constants: see README for an overview."""

import re
from contextvars import ContextVar

GITHUB = "https://api.github.com"


MAX_ASSET_SIZE = 2 * 1024 * 1024 * 1024 - 1


CHUNK_SIZE = 1024 * 1024


NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


LOG_SECRETS: ContextVar[tuple[str, ...]] = ContextVar("log_secrets", default=())


REPOSITORY_TIME: ContextVar[list[float] | None] = ContextVar("repository_time", default=None)
