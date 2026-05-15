"""In-process cache of OAuth access tokens, keyed by (user_id, provider_id).

Lives for the lifetime of the inference API process. The authoritative store
is AgentCore Identity's token vault — this cache is just a hot path so the
`OAuthBearerAuth` token provider doesn't have to call AgentCore on every
MCP request.

Tokens are written when:
  * `OAuthConsentHook` warms the cache after a successful vault lookup, or
  * the resume path re-fetches a token after the user completes consent.

Each entry has a TTL — by default a little under the upstream token's
lifetime (Google's access tokens are 3600s; we expire locally at 3000s).
On expiry `get` returns None so `OAuthConsentHook._gate` re-asks AgentCore
Identity, which transparently refreshes the access token using the
refresh_token in the vault. Without a TTL the cache would hand out an
expired token until the upstream MCP server returned 401, and the 401
retry path would force a full re-consent — i.e. the user reconnects
hourly even though refresh would have worked.

Tokens are evicted explicitly via `clear_user_provider` when consent is
revoked. Disconnect intent ("user pressed Disconnect") is *not* held here
— it lives in the DDB-backed `OAuthDisconnectRepository` so it's visible
across replicas. The cache only holds tokens.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

# Default TTL for cached access tokens. Google access tokens last 3600s;
# expiring locally at 3000s gives us a 10-minute safety margin so we
# refresh before the upstream rejects the token. Most other providers
# (Microsoft, GitHub) use the same or longer lifetimes, so this is a safe
# floor across the board.
DEFAULT_TTL_SECONDS = 3000

_lock = threading.Lock()
# (user_id, provider_id) -> (token, expires_at_monotonic)
_cache: dict[tuple[str, str], tuple[str, float]] = {}


def get(user_id: str, provider_id: str) -> Optional[str]:
    """Return the cached token if present and not expired, else None.

    Expired entries are evicted on access; we don't run a sweeper because
    the cache is small (one entry per (user, provider) pair) and a stale
    entry that's never read costs nothing.
    """
    with _lock:
        entry = _cache.get((user_id, provider_id))
        if entry is None:
            return None
        token, expires_at = entry
        if time.monotonic() >= expires_at:
            del _cache[(user_id, provider_id)]
            return None
        return token


def set(
    user_id: str,
    provider_id: str,
    token: str,
    *,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
) -> None:
    with _lock:
        _cache[(user_id, provider_id)] = (token, time.monotonic() + ttl_seconds)


def clear_user_provider(user_id: str, provider_id: str) -> None:
    with _lock:
        _cache.pop((user_id, provider_id), None)


def clear_user(user_id: str) -> int:
    with _lock:
        keys = [k for k in _cache if k[0] == user_id]
        for key in keys:
            del _cache[key]
        return len(keys)
