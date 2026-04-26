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

Disconnect intent ("user pressed Disconnect" / "tool returned 401") is *not*
held here — it lives in the DDB-backed `OAuthDisconnectRepository` so it's
visible across replicas. The cache only holds tokens.
"""

from __future__ import annotations

import threading
from typing import Optional


_lock = threading.Lock()
_cache: dict[tuple[str, str], str] = {}


def get(user_id: str, provider_id: str) -> Optional[str]:
    with _lock:
        return _cache.get((user_id, provider_id))


def set(user_id: str, provider_id: str, token: str) -> None:
    with _lock:
        _cache[(user_id, provider_id)] = token


def clear_user_provider(user_id: str, provider_id: str) -> None:
    with _lock:
        _cache.pop((user_id, provider_id), None)


def clear_user(user_id: str) -> int:
    with _lock:
        keys = [k for k in _cache if k[0] == user_id]
        for key in keys:
            del _cache[key]
        return len(keys)
