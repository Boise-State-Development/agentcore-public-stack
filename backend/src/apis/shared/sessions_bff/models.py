"""Data models for BFF session storage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SessionRecord:
    """Server-side session row stored in the BFF sessions table.

    The Cognito tokens never leave the server: the browser only ever sees the
    sealed `session_id` carried in the `__Host-bff_session` cookie.
    """

    session_id: str
    user_id: str  # Cognito `sub` claim
    username: str  # Cognito `cognito:username`; required to compute SECRET_HASH on refresh
    cognito_access_token: str
    cognito_refresh_token: str
    id_token: Optional[str]
    access_token_exp: int  # epoch seconds, for refresh leeway comparison
    csrf_secret: str  # bound to this session; double-submit token derives from it
    created_at: int  # epoch seconds
    last_seen_at: int  # epoch seconds
    ttl: int  # epoch seconds; DynamoDB `ttl` attribute drives row expiry

    def needs_refresh(self, now_epoch: int, leeway_seconds: int) -> bool:
        """True iff the access token will expire within `leeway_seconds`.

        The refresh middleware uses this to decide whether to take the
        per-session lock and call Cognito.
        """
        return self.access_token_exp - now_epoch <= leeway_seconds


@dataclass
class CookiePayload:
    """Decoded shape of a sealed cookie value.

    The cookie carries a tiny versioned record so we can rotate the codec
    later without invalidating the world. Only `session_id` is functionally
    required today; `version` lets us route to a future codec if we change
    the seal format.
    """

    session_id: str
    version: int = 1

    # Reserved for future use (e.g. cookie-side expiry hint to skip a DDB
    # lookup on obviously-stale cookies). Keep field present so the encoded
    # shape doesn't change when we add it.
    extras: dict = field(default_factory=dict)
