"""In-process cache of OAuth access tokens, keyed by (user_id, provider_id).

Lives for the lifetime of the inference API process. The authoritative store
is AgentCore Identity's token vault — this cache is just a hot path so the
`OAuthBearerAuth` token provider doesn't have to call AgentCore on every
MCP request.

Tokens are written when:
  * `OAuthConsentHook` warms the cache after a successful vault lookup, or
  * the resume path re-fetches a token after the user completes consent.

Tokens are evicted explicitly via `clear_user_provider` when consent is
revoked or expires; we don't track expiry locally because AgentCore
Identity owns refresh.
"""

from __future__ import annotations

import threading
from typing import Optional


_lock = threading.Lock()
_cache: dict[tuple[str, str], str] = {}
# Per-(user, provider) sticky flag set by `mark_force_reauth` after a tool
# call returns a 401-style error. The consent hook reads this flag on the
# next BeforeToolCallEvent and asks AgentCore Identity for a fresh consent
# URL (`force_authentication=True`) instead of trusting the now-stale
# vault token. Cleared once the new token lands in the cache.
_force_reauth: set[tuple[str, str]] = set()


def get(user_id: str, provider_id: str) -> Optional[str]:
    with _lock:
        return _cache.get((user_id, provider_id))


def set(user_id: str, provider_id: str, token: str) -> None:
    with _lock:
        _cache[(user_id, provider_id)] = token
        _force_reauth.discard((user_id, provider_id))


def clear_user_provider(user_id: str, provider_id: str) -> None:
    with _lock:
        _cache.pop((user_id, provider_id), None)


def clear_user(user_id: str) -> int:
    with _lock:
        keys = [k for k in _cache if k[0] == user_id]
        for key in keys:
            del _cache[key]
        force_keys = [k for k in _force_reauth if k[0] == user_id]
        for key in force_keys:
            _force_reauth.discard(key)
        return len(keys)


def mark_force_reauth(user_id: str, provider_id: str) -> None:
    """Flag (user, provider) for forced re-consent on the next token fetch.

    Used by the OAuth error hook after an MCP tool returns a 401 — the
    cached token is provably stale, so the next BeforeToolCallEvent must
    bypass AgentCore's vault and trigger a fresh consent.
    """
    with _lock:
        _cache.pop((user_id, provider_id), None)
        _force_reauth.add((user_id, provider_id))


def needs_force_reauth(user_id: str, provider_id: str) -> bool:
    with _lock:
        return (user_id, provider_id) in _force_reauth


def clear_force_reauth(user_id: str, provider_id: str) -> None:
    """Clear the force-reauth flag without touching the cached token.

    Called from `complete-consent` after a successful re-authorization, so
    the next status check or token fetch sees the user as connected again
    without needing the agent loop to warm the cache via `set()`.
    """
    with _lock:
        _force_reauth.discard((user_id, provider_id))
