"""Detect a destructive source rewrite using commits already in a local Git repository.

The caller supplies a Git runner that accepts an argument list (without ``git``)
 and returns stdout as bytes, raising on failure. It must run in the repository
containing both snapshots and every source ref, with no lazy network fetching.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import tempfile


@dataclass(frozen=True)
class WipeDetection:
    old_file_count: int
    lost_file_count: int
    old_commit_count: int
    unreachable_commit_count: int

    @property
    def is_wipe(self) -> bool:
        return bool(self.old_file_count and self.old_commit_count) and (
            self.lost_file_count * 4 >= self.old_file_count * 3
            and self.unreachable_commit_count * 4 >= self.old_commit_count * 3
        )


def detect_wipe(
    run: Callable[[list[str]], bytes],
    old_commit: str,
    new_commit: str,
    source_refs: Iterable[str],
) -> WipeDetection:
    """Compare default trees and old history against all current source refs.

    Pass explicit old/new default commit IDs and the complete source branch/tag
    ref names. Missing objects or invalid refs raise rather than silently
    undercount. Annotated tags are peeled; refs to trees/blobs do not protect
    any commit. File loss is the net drop in default-tree file count.
    """

    def commit_id(value: str) -> str:
        return run(["rev-parse", "--verify", "--end-of-options", value + "^{commit}"]).strip().decode("ascii")

    old = commit_id(old_commit)
    new = commit_id(new_commit)

    def files(commit: str) -> set[bytes]:
        return set(
            filter(None, run(["ls-tree", "-r", "-z", "--name-only", "--full-tree", commit]).split(b"\0"))
        )

    old_files = files(old)
    new_files = files(new)
    old_commits = set(run(["rev-list", old]).splitlines())
    exclusions = set()
    for ref in source_refs:
        if not (ref.startswith("refs/heads/") or ref.startswith("refs/tags/")):
            raise ValueError(f"not a source branch or tag ref: {ref!r}")
        peeled = run(["rev-parse", "--verify", "--end-of-options", ref + "^{}"]).strip().decode("ascii")
        if run(["cat-file", "-t", peeled]).strip() == b"commit":
            exclusions.add(peeled)
    # The runner only accepts argv, not stdin. A private temporary input file and
    # a fixed Git shell alias bridge that interface without hitting ARG_MAX. Only
    # Git-resolved object IDs enter the file; the path is passed as an argument.
    with tempfile.NamedTemporaryFile(mode="w", encoding="ascii") as revisions:
        revisions.write(old + "\n")
        revisions.writelines("^" + oid + "\n" for oid in sorted(exclusions))
        revisions.flush()
        lost_commits = run([
            "-c", 'alias.wipe-rev-list=!f() { git rev-list --stdin < "$1"; }; f',
            "wipe-rev-list", revisions.name,
        ]).splitlines()
    return WipeDetection(len(old_files), max(0, len(old_files) - len(new_files)), len(old_commits), len(lost_commits))
