"""Shared test helper: patch one name in every repo_mirror module that defines it.

Several modules look up the same helper names (for example ``mirror_git``), so
patching only one module would leave the other call sites unpatched. This
helper patches all of them with one shared mock, so assertions on the mock see
calls from any module.
"""

from __future__ import annotations

import contextlib
import importlib
import pkgutil
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import repo_mirror

_UNSET: Any = object()


@contextlib.contextmanager
def patch_everywhere(name: str, new: Any = _UNSET, **kwargs: Any) -> Iterator[Any]:
    """Replace ``name`` in every repo_mirror module that defines it.

    With ``new`` given, the value is used as-is (for constants such as
    ``CHUNK_SIZE``). Otherwise a shared MagicMock built from ``kwargs`` is used.
    Yields the replacement so tests can make assertions on it.
    """
    shared = new if new is not _UNSET else MagicMock(**kwargs)
    modules = [importlib.import_module("mirror")]  # entry point; fakes read names from it
    modules += [
        importlib.import_module(f"repo_mirror.{info.name}")
        for info in pkgutil.iter_modules(repo_mirror.__path__)
    ]
    with contextlib.ExitStack() as stack:
        for module in modules:
            if hasattr(module, name):
                stack.enter_context(patch.object(module, name, new=shared))
        yield shared
