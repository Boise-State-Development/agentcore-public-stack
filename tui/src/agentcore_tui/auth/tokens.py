"""Token handling and storage.

What lives where, and why:

* The **refresh token** is the long-lived secret and the only thing persisted.
  It goes to the OS keyring, the same place `aws` and `gh` keep theirs.
* The **access token** is short-lived (an hour by default from Cognito) and is
  kept in memory only. Persisting it would widen the exposure window for no
  benefit, since it can always be re-minted from the refresh token.
* The **id token** is not stored at all. Nothing here needs it: identity comes
  from the access token's claims, and app-api re-validates that on every call.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from typing import Any

from .. import keyring_store
from ..errors import ConfigError

KEYRING_SERVICE = keyring_store.SSO_SERVICE

#: Refresh a little early rather than discovering expiry mid-request.
EXPIRY_SKEW_SECONDS = 120


@dataclass(frozen=True, slots=True)
class TokenSet:
    """An access token and the refresh token that can renew it."""

    access_token: str = field(repr=False)
    expires_at: float
    refresh_token: str | None = field(default=None, repr=False)
    token_type: str = "Bearer"
    scope: str | None = None

    @property
    def expired(self) -> bool:
        """True when the access token is expired or close enough to it."""
        return time.time() >= (self.expires_at - EXPIRY_SKEW_SECONDS)

    @property
    def seconds_remaining(self) -> int:
        return max(0, int(self.expires_at - time.time()))

    def authorization_header(self) -> str:
        return f"{self.token_type} {self.access_token}"

    def with_refresh_token(self, refresh_token: str | None) -> TokenSet:
        """Carry a refresh token forward.

        Cognito's refresh response omits ``refresh_token``, so a naive parse
        would drop it and force a full re-login on the next expiry.
        """
        return replace(self, refresh_token=refresh_token or self.refresh_token)

    @classmethod
    def from_token_response(cls, payload: dict[str, Any], *, now: float | None = None) -> TokenSet:
        """Build from an OAuth token endpoint response body."""
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ConfigError(
                "Token response contained no access_token",
                hint="The identity provider returned an unexpected response shape.",
            )

        raw_expires = payload.get("expires_in")
        expires_in = raw_expires if isinstance(raw_expires, int) and not isinstance(raw_expires, bool) else 3600
        refresh_token = payload.get("refresh_token")
        token_type = payload.get("token_type")
        scope = payload.get("scope")

        return cls(
            access_token=access_token,
            expires_at=(now if now is not None else time.time()) + expires_in,
            refresh_token=refresh_token if isinstance(refresh_token, str) and refresh_token else None,
            token_type=token_type if isinstance(token_type, str) and token_type else "Bearer",
            scope=scope if isinstance(scope, str) else None,
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
#
# Keyed by base URL so one machine can hold sessions for several deployments,
# and stored under a service distinct from API keys so revoking one credential
# cannot disturb the other. The degradation behaviour lives in
# :mod:`agentcore_tui.keyring_store`, shared with the API-key store.


def save_refresh_token(base_url: str, refresh_token: str) -> None:
    """Persist a refresh token, or raise ConfigError with a usable hint."""
    keyring_store.store(
        KEYRING_SERVICE,
        base_url,
        refresh_token,
        hint="Without a keyring the session cannot be remembered; you will need to log in each time.",
    )


def load_refresh_token(base_url: str) -> tuple[str | None, str | None]:
    """Return ``(refresh_token, unavailable_reason)``."""
    return keyring_store.load(KEYRING_SERVICE, base_url)


def delete_refresh_token(base_url: str) -> bool:
    """Remove a stored refresh token. False when there was nothing to remove."""
    return keyring_store.delete(KEYRING_SERVICE, base_url)


def describe_stored_session(base_url: str) -> str:
    """A one-line, secret-free summary for `status` output."""
    token, reason = load_refresh_token(base_url)
    if reason:
        return f"keyring unavailable ({reason})"
    if not token:
        return "no stored session"
    return f"stored ({len(token)} chars)"


def decode_claims(access_token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying the signature.

    For display only — "who am I logged in as", token expiry, which client
    minted it. Never for an access decision: the server validates the
    signature, and an unverified local decode proves nothing.
    """
    parts = access_token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        import base64

        raw = base64.urlsafe_b64decode(payload + padding)
        decoded = json.loads(raw)
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}
