"""In-process TTL cache of pending AgentCore OAuth consent sessions.

Defense-in-depth for the `complete_consent` endpoint. AgentCore's
`CompleteResourceTokenAuth` takes a `userIdentifier` and a `sessionUri`;
the binding between them is enforced by AgentCore, but we add a local
check so that even if that binding is ever loosened (or someone learns a
`session_uri` belonging to another user), they cannot drive it to
completion under their own identity here.

At `initiate_consent` we parse the `session_uri` out of the authorization
URL AgentCore hands us and call `remember(user_id, session_uri)`. At
`complete_consent` we call `consume(user_id, session_uri)`; if the pair
wasn't remembered (or it expired, or it belongs to a different user) the
route rejects the request with 403.

This cache is process-local. In the AgentCore Runtime deployment every
session is routed to a single worker for its lifetime, so initiate and
complete normally hit the same process. If they don't — or if the worker
restarts between them — `consume` returns False and the user sees a
403 with guidance to re-initiate consent, which is the right failure
mode: prefer a confusing error over a silent authorization weakening.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

# 10 minutes matches the typical OAuth authorize-code TTL. Shorter than
# AgentCore's own session lifetime so a cleanup-on-access is adequate.
_TTL_SECONDS = 600

_lock = threading.Lock()
# session_uri -> (user_id, expires_at_monotonic)
_pending: dict[str, tuple[str, float]] = {}


def remember(user_id: str, session_uri: str) -> None:
    """Record that `user_id` initiated consent with `session_uri`."""
    now = time.monotonic()
    expires_at = now + _TTL_SECONDS
    with _lock:
        _prune_locked(now)
        _pending[session_uri] = (user_id, expires_at)


def consume(user_id: str, session_uri: str) -> bool:
    """Return True if `session_uri` was initiated by `user_id` and is unexpired.

    The entry is removed on a successful match (single-use). Misses are
    left in place so a concurrent legitimate completer still succeeds.
    """
    now = time.monotonic()
    with _lock:
        _prune_locked(now)
        entry = _pending.get(session_uri)
        if entry is None:
            return False
        owner_id, expires_at = entry
        if expires_at <= now:
            _pending.pop(session_uri, None)
            return False
        if owner_id != user_id:
            return False
        _pending.pop(session_uri, None)
        return True


def forget_user(user_id: str) -> int:
    """Drop every pending session initiated by `user_id`. Returns the count."""
    with _lock:
        keys = [uri for uri, (owner, _) in _pending.items() if owner == user_id]
        for key in keys:
            _pending.pop(key, None)
        return len(keys)


def _prune_locked(now: float) -> None:
    """Evict expired entries. Caller must hold `_lock`."""
    expired = [uri for uri, (_, exp) in _pending.items() if exp <= now]
    for uri in expired:
        _pending.pop(uri, None)


# Query-parameter names AgentCore has been observed to use for the
# session identifier when it hands back an authorization URL. We try
# each in order; whichever hits first is returned.
_SESSION_URI_PARAMS = ("request_uri", "session_id", "sessionUri", "sessionId")


def extract_session_uri(authorization_url: str) -> Optional[str]:
    """Parse the AgentCore session identifier out of an authorization URL.

    Returns None when none of the expected parameter names are present.
    Callers should treat that as a soft failure (log + skip server-side
    tracking) rather than blocking consent — AgentCore's own binding is
    still in force.
    """
    if not authorization_url:
        return None
    try:
        parsed = urlparse(authorization_url)
    except ValueError:
        return None
    params = parse_qs(parsed.query, keep_blank_values=False)
    for key in _SESSION_URI_PARAMS:
        values = params.get(key)
        if values and values[0]:
            return values[0]
    return None
