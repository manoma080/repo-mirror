"""Per-source-host concurrency limits shared by both mirror runners."""

import re
from collections import Counter
from urllib.parse import urlparse

CONFIG_KEY = "host_concurrency_limits"
DEFAULT_KEY = "default"
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?")


def source_host(url):
    """Return the normalized host, including a non-default port, of a source URL."""
    parsed = urlparse(url) if isinstance(url, str) else None
    if (parsed is None or parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("invalid host concurrency limits")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("invalid host concurrency limits") from None
    host = parsed.hostname.lower()
    return f"{host}:{port}" if port is not None else host


class HostLimits:
    """Caps for the number of repositories mirrored at once from one host."""

    def __init__(self, limits=None, default=1):
        self.limits = dict(limits or {})
        self.default = default

    @classmethod
    def from_config(cls, config, default):
        """Validate ``host_concurrency_limits`` from a manifest, or return the default."""
        if config is None:
            return cls({}, default)
        if not isinstance(config, dict) or not config:
            raise ValueError("invalid host concurrency limits")
        limits, fallback = {}, default
        for key, value in config.items():
            if not isinstance(key, str) or key != key.strip().lower() or not _HOST.fullmatch(key):
                raise ValueError("invalid host concurrency limits")
            if type(value) is not int or value <= 0:
                raise ValueError("invalid host concurrency limits")
            if key == DEFAULT_KEY:
                fallback = value
            else:
                limits[key] = value
        return cls(limits, fallback)

    def cap(self, host):
        return self.limits.get(host, self.default)


class HostAdmission:
    """Count in-flight repositories per host against the configured caps."""

    def __init__(self, limits):
        self.limits = limits
        self.active = Counter()

    def available(self, host):
        return self.active[host] < self.limits.cap(host)

    def acquire(self, host):
        if not self.available(host):
            raise RuntimeError("host concurrency limit exceeded")
        self.active[host] += 1

    def release(self, host):
        if self.active[host] <= 0:
            raise RuntimeError("host concurrency accounting underflow")
        self.active[host] -= 1


def take_admissible(queue, admission, host_of):
    """Pop the first queued item whose host still has capacity.

    Scanning instead of looking only at the head keeps a saturated host from
    blocking repositories queued behind it.
    """
    for index in range(len(queue)):
        item = queue[index]
        if admission.available(host_of(item)):
            del queue[index]
            return item
    return None
