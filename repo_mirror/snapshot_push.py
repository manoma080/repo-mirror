"""Explicit, lease-protected transport of branch and tag snapshots.

All public functions take ``run(args) -> bytes``: a checked Git runner with
credentials configured for the relevant remote. Arguments exclude ``git``.
Use separate suitably authenticated runners when accessing different hosts.
``store`` is a fresh bare repository; keep it until all pushes complete.

``capture_snapshot(run, store, remote)`` fetches and pins advertised branch and
 tag objects and returns {full_refname: object_id}; races raise RuntimeError.
``archive_snapshot(run, store, archive, snapshot)`` requires an empty archive,
creates those exact refs with empty leases, then verifies them by ls-remote.
``push_snapshot(run, store, destination, snapshot, observed, switch_default=None)``
reconciles only the destination refs in the previously captured ``observed``
mapping. Pass the *same* observed mapping whose contents were archived, never a
fresh listing. When the old default branch must be removed, switch_default is
called with a surviving new branch ref after updates and must change remote HEAD;
this is verified before deletion. A failed step may leave partial changes; never
retry without a fresh safety decision and snapshot. Concurrent new refs survive.
"""

import re
import uuid
from contextlib import contextmanager


@contextmanager
def git_phase(phase):
    try:
        yield
    except Exception as exc:
        if not hasattr(exc, "mirror_git_phase"):
            setattr(exc, "mirror_git_phase", phase)
        raise


class SnapshotError(RuntimeError):
    """A snapshot could not be proven safe or consistent."""


def _refs(run, remote, all_refs=False):
    result = {}
    patterns = [] if all_refs else ["refs/heads/*", "refs/tags/*"]
    data = run(["ls-remote", "--refs", "--", remote, *patterns])
    for line in data.decode("utf-8", "surrogateescape").splitlines():
        oid, ref = line.split("\t")
        if (
            not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid)
            or not ref.startswith("refs/")
            or ref in result
        ):
            raise SnapshotError("invalid remote ref advertisement")
        if not all_refs and not ref.startswith(("refs/heads/", "refs/tags/")):
            raise SnapshotError("unexpected remote ref")
        result[ref] = oid
    return result


def capture_snapshot(run, store, remote):
    """Fetch and pin precisely the advertised branches and tags, or fail."""
    with git_phase("snapshot fetch"):
        refs = _refs(run, remote)
        if refs:
            prefix = "refs/repo-mirror/snapshots/" + uuid.uuid4().hex + "/"
            specs = [f"{ref}:{prefix}{ref}" for ref in refs]
            run(["-C", store, "fetch", "--no-tags", "--", remote, *specs])
            for ref, oid in refs.items():
                actual = run(["-C", store, "rev-parse", "--verify", prefix + ref]).decode("ascii").strip()
                if actual != oid:
                    raise SnapshotError("remote changed during snapshot capture")
    return refs


def _validate(snapshot):
    for ref, oid in snapshot.items():
        if (
            not isinstance(ref, str)
            or not ref.startswith(("refs/heads/", "refs/tags/"))
            or not isinstance(oid, str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid)
        ):
            raise SnapshotError("invalid snapshot")


def _push(run, store, remote, updates, expected):
    if not updates:
        return
    leases = [f"--force-with-lease={ref}:{expected.get(ref, '')}" for ref in updates]
    specs = [f"{oid}:{ref}" if oid else f":{ref}" for ref, oid in updates.items()]
    run(["-C", store, "push", "--atomic", *leases, "--", remote, *specs])


def _verify(run, remote, wanted, removed=()):
    with git_phase("remote verification"):
        actual = _refs(run, remote)
        if any(actual.get(ref) != oid for ref, oid in wanted.items()) or any(ref in actual for ref in removed):
            raise SnapshotError("remote verification failed")


def archive_snapshot(run, store, archive, snapshot):
    """Copy a snapshot into a proven empty archive and verify every ref."""
    _validate(snapshot)
    if _refs(run, archive, all_refs=True):
        raise SnapshotError("archive is not empty")
    _push(run, store, archive, snapshot, {})
    actual = _refs(run, archive, all_refs=True)
    if actual != snapshot:
        raise SnapshotError("archive verification failed")


def _default_branch(run, remote):
    data = run(["ls-remote", "--symref", "--", remote, "HEAD"]).decode("utf-8", "surrogateescape")
    lines = [line for line in data.splitlines() if line.startswith("ref: ") and line.endswith("\tHEAD")]
    if len(lines) != 1:
        raise SnapshotError("cannot determine remote default branch")
    ref = lines[0][5:].split("\t", 1)[0]
    if not ref.startswith("refs/heads/"):
        raise SnapshotError("invalid remote default branch")
    return ref


def push_snapshot(run, store, destination, snapshot, observed, switch_default=None):
    with git_phase("snapshot push"):
        return _push_snapshot(run, store, destination, snapshot, observed, switch_default)


def _push_snapshot(run, store, destination, snapshot, observed, switch_default=None):
    """Apply snapshot with explicit leases against the archived destination."""
    _validate(snapshot)
    _validate(observed)
    if not snapshot:
        raise SnapshotError("refusing to replace destination with empty snapshot")
    removed = set(observed) - set(snapshot)
    updates = {ref: oid for ref, oid in snapshot.items() if observed.get(ref) != oid}
    # All snapshot refs, including unchanged refs, participate in a lease check.
    updates.update({ref: oid for ref, oid in snapshot.items() if ref in observed})
    default = _default_branch(run, destination) if any(r.startswith("refs/heads/") for r in removed) else None
    if default in removed and (
        switch_default is None or not any(r.startswith("refs/heads/") for r in snapshot)
    ):
        raise SnapshotError("default branch removal requires a replacement and callback")
    if default not in removed:
        updates.update(dict.fromkeys(sorted(removed)))
        _push(run, store, destination, updates, observed)
        _verify(run, destination, snapshot, removed)
        return
    # Changing the server's default requires a separate API operation: updates
    # may already have landed if the switch or subsequent deletion fails.
    _push(run, store, destination, updates, observed)
    _verify(run, destination, snapshot)
    if default in removed:
        if switch_default is None:
            raise SnapshotError("default branch removal requires a replacement and callback")
        replacement = next(r for r in sorted(snapshot) if r.startswith("refs/heads/"))
        with git_phase("default switch"):
            switch_default(replacement)
            if _default_branch(run, destination) != replacement:
                raise SnapshotError("default branch change not verified")
    _push(run, store, destination, dict.fromkeys(sorted(removed)), observed)
    _verify(run, destination, snapshot, removed)
