"""Process-wide monotonic counter for role-mutation invalidation.

Caches that hold authorization-relevant state (most importantly, the
user-profile cache in ``apis.shared.auth.dependencies``) tag each entry
with the watermark value that was current at the time it was stored.
On read, a cached entry is treated as a miss whenever the watermark has
advanced past the entry's tag. Callers that mutate roles or otherwise
need to invalidate authorization caches call :func:`bump_roles_version`.

This is intentionally an in-process counter, not a distributed one. In a
multi-task ECS deployment each task's counter runs independently — that
matches how the existing ``AppRoleCache`` works (see ``cache.py``) and
keeps the dependency surface minimal. Cross-task coherence already lags
by ``refresh_leeway_seconds`` for BFF sessions; this watermark only
shortens the window for a process that has just observed a mutation.

The module exposes a small, thread-safe API:

* :func:`get_roles_version` — read the current value
* :func:`bump_roles_version` — increment and return the new value
"""

from __future__ import annotations

import itertools
import threading

# Start at 1 so any "0" sentinel a caller stores is automatically considered
# stale on the first comparison.
_counter = itertools.count(1)
_value: int = next(_counter)
_lock = threading.Lock()


def get_roles_version() -> int:
    """Return the current watermark value."""
    return _value


def bump_roles_version() -> int:
    """Advance the watermark and return the new value.

    Thread-safe. Multiple concurrent callers each receive a distinct value;
    no updates are lost.
    """
    global _value
    with _lock:
        _value = next(_counter)
        return _value
