"""Forward only the safe portions of shared mirror progress to Forgejo Actions."""
import os
import re

from repo_mirror.logging_utils import safe_label

_N = r'[0-9]{1,10}'
_TIME = rf'{_N}\.[0-9]{{3}}s'
_SAFE = tuple(re.compile(pattern) for pattern in (
    rf'step (?:git (?:source-snapshot|destination-snapshot|wipe-detection|wipe-archive|snapshot-push)|release-discovery target=source|asset-byte-compare) elapsed={_TIME}',
    rf'network target=(?:source|destination) operation=(?:API|asset|asset-hop) method=(?:GET|POST|PUT|PATCH|DELETE|request) elapsed={_TIME}',
    rf'releases discovered={_N} selected={_N} skipped={_N}',
    rf'release {_N}/{_N} (?:completed|assets={_N})',
    rf'assets uploaded={_N} updated={_N} skipped={_N} retried={_N} upload_failed={_N}',
    r'release resuming pending update',
    r'releases ignored by configuration',
))
_LABELS = tuple(re.compile(pattern) for pattern in (
    rf'(===== Release {_N}/{_N}: )(.+)( =====)',
    rf'(release {_N}/{_N} started tag=)(.+)()',
    rf'(asset {_N}/{_N} started asset_name=)(.+)()',
))
_REDACT = tuple(re.compile(pattern) for pattern in (
    rf'(asset {_N}/{_N} comparing downloaded bytes) reason=.+',
    rf'(asset {_N}/{_N} unchanged) \(published SHA-256\) reason=matching-published-digests',
))


def safe_progress(raw):
    """Forward named progress using the GitHub workflow's label sanitizer."""
    line = raw.decode('utf-8', errors='replace') if len(raw) <= 512 else ''
    if any(pattern.fullmatch(line) for pattern in _SAFE):
        return line
    for pattern in _LABELS:
        match = pattern.fullmatch(line)
        if match:
            prefix, label, suffix = match.groups()
            return prefix + safe_label(label) + suffix
    for pattern in _REDACT:
        match = pattern.fullmatch(line)
        if match:
            return match[1]
    return None


class ProgressReader:
    """Tail the worker's stderr without moving its shared file write offset."""

    def __init__(self):
        self.offset = 0
        self.partial = b''
        self.discard_partial = False

    def drain(self, file, *, finished=False):
        # Bound each live read so a noisy worker cannot starve the scheduler's
        # timeout checks. On exit read through EOF after its process group dies.
        while True:
            chunk = os.pread(file.fileno(), 65536, self.offset)
            if not chunk:
                return
            self.offset += len(chunk)
            lines = chunk.split(b'\n')
            if len(lines) == 1:
                if not self.discard_partial:
                    self.partial += chunk
                    if len(self.partial) > 512:
                        self.partial = b''
                        self.discard_partial = True
            else:
                if not self.discard_partial:
                    progress = safe_progress(self.partial + lines[0])
                    if progress:
                        yield progress
                for line in lines[1:-1]:
                    progress = safe_progress(line)
                    if progress:
                        yield progress
                self.partial = lines[-1] if len(lines[-1]) <= 512 else b''
                self.discard_partial = len(lines[-1]) > 512
            if not finished:
                return
