"""Judge whether a source repository has gone quiet for long enough to warn."""

import contextvars
from contextlib import contextmanager
from datetime import UTC, datetime

DEFAULT_INACTIVE_DAYS = 365


def parse_inactive_days(value):
    """Accept a whole, non-negative number of days; zero disables the warning."""
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise ValueError("inactive threshold must be a whole number of days") from None
    if days < 0:
        raise ValueError("inactive threshold must not be negative")
    return days


_observer: contextvars.ContextVar[dict | None] = contextvars.ContextVar("repo_mirror_activity", default=None)


def begin():
    """Start collecting activity for the current context; return (record, token)."""
    record = {}
    return record, _observer.set(record)


def end(token):
    """Stop collecting activity for a token returned by ``begin``."""
    _observer.reset(token)


@contextmanager
def observing():
    """Collect the newest commit and release timestamps of one repository."""
    record, token = begin()
    try:
        yield record
    finally:
        end(token)


def observe(kind, stamp):
    """Publish one activity observation to the active collector, if any."""
    active = _observer.get()
    if active is None or not isinstance(stamp, datetime):
        return
    current = active.get(kind)
    if current is None or stamp > current:
        active[kind] = stamp


def newest(activity):
    """Return the newest recorded commit or release timestamp, or None."""
    stamps = [
        stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)
        for stamp in (activity or {}).values()
        if isinstance(stamp, datetime)
    ]
    return max(stamps) if stamps else None


def idle_days(activity, now=None):
    """Return whole days since the newest recorded activity, or None if unknown."""
    stamp = newest(activity)
    if stamp is None:
        return None
    now = now or datetime.now(UTC)
    return max(0, int((now - stamp).total_seconds() // 86400))


def stale(activity, threshold, now=None):
    """Return ``(days, stamp)`` when activity is at least ``threshold`` days old."""
    if not threshold:
        return None
    days = idle_days(activity, now)
    if days is None or days < threshold:
        return None
    return days, newest(activity)


def stamp_date(stamp):
    """Format a timestamp as an unambiguous UTC calendar date."""
    return stamp.astimezone(UTC).strftime("%Y-%m-%d") if stamp is not None else "unknown"
