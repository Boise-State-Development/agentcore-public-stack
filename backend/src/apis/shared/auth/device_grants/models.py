"""Device-authorization grants for the terminal client.

The CLI cannot receive a redirect: it has no reachable loopback address in the
general case (container, SSH, WSL), and Cognito matches redirect URIs
byte-for-byte so an ephemeral port cannot be registered. So the browser leg and
the terminal leg are decoupled the way RFC 8628 decouples them — the CLI polls
for an outcome it never receives directly.

This is deliberately *not* a Cognito device flow (Cognito has none). The browser
leg is the platform's existing BFF login against the existing app client, and
everything here lives in app-api. Nothing about the Cognito configuration
changes.

Two secrets, with different jobs:

``device_code``
    Long, random, secret. Held only by the CLI process, presented on every poll.
    Stored **hashed** — a leaked grant table must not let a reader claim a
    pending session, exactly as with API keys.

``user_code``
    Short and human-transcribable, shown in the terminal and typed or clicked
    into the browser. Low entropy by necessity, so it is single-use,
    short-lived, and rate-limited. It authorises nothing on its own: it only
    identifies which pending grant a *separately authenticated* browser session
    is approving.

The grant never stores a usable credential. On approval it records the
``session_id`` only; the poll endpoint seals that into a session value at
response time using the cookie codec's key, which lives in Secrets Manager. A
reader of this table therefore gains no session access.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, Field

#: Bytes of entropy in a device code. 32 bytes -> 43 url-safe characters.
DEVICE_CODE_BYTES = 32

#: Characters used for the user code. Excludes vowels (no accidental words) and
#: the pairs that are indistinguishable in common terminal fonts: 0/O, 1/I/L,
#: 2/Z, 5/S, 8/B. What remains survives being read aloud and retyped.
USER_CODE_ALPHABET = "CDFGHJKMNPQRTVWXY34679"

#: Total user-code characters, rendered as two groups of four (``ABCD-EFGH``).
USER_CODE_LENGTH = 8

#: How long a grant may stay pending. Long enough to find the browser and
#: complete a federated sign-in, short enough that an abandoned user code stops
#: being guessable quickly.
GRANT_TTL_SECONDS = 600

#: What the CLI is told to wait between polls.
POLL_INTERVAL_SECONDS = 5

#: Minimum gap the server will tolerate before answering ``slow_down``. Slightly
#: under the advertised interval so ordinary jitter is not punished.
MIN_POLL_GAP_SECONDS = 4


class GrantStatus(StrEnum):
    """Lifecycle of one device-authorization grant."""

    #: Issued, waiting for a browser to approve it.
    PENDING = "pending"
    #: A browser session approved it; ``session_id`` is set and claimable once.
    APPROVED = "approved"
    #: The session value has been handed to the CLI. Terminal.
    CLAIMED = "claimed"
    #: A human explicitly refused. Terminal, and distinct from expiry so the
    #: CLI can say "you declined" rather than "it timed out".
    DENIED = "denied"


def generate_device_code() -> str:
    """A url-safe secret the CLI keeps for the life of the grant."""
    return secrets.token_urlsafe(DEVICE_CODE_BYTES)


def generate_user_code() -> str:
    """A short code a human can read from a terminal and type into a browser."""
    raw = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(USER_CODE_LENGTH))
    midpoint = USER_CODE_LENGTH // 2
    return f"{raw[:midpoint]}-{raw[midpoint:]}"


def normalise_user_code(raw: str) -> str:
    """Canonicalise a user-typed code for lookup.

    People retype these, so accept lower case, missing or extra hyphens, and
    surrounding whitespace. Deliberately does *not* attempt character
    substitution (``0``->``O``): the alphabet already excludes every ambiguous
    pair, so a rejected code means a genuine typo rather than a font problem.
    """
    return "".join(raw.split()).replace("-", "").upper()


def hash_device_code(device_code: str) -> str:
    """SHA-256 of a device code, for storage and lookup.

    Plain SHA-256 rather than a password hash on purpose: the input is 256 bits
    of machine-generated entropy, so there is no dictionary to attack and a slow
    KDF would only add latency to every poll.
    """
    return hashlib.sha256(device_code.encode("utf-8")).hexdigest()


@dataclass
class DeviceGrant:
    """Server-side record of one pending or completed grant."""

    #: SHA-256 of the device code. The code itself is never stored.
    device_code_hash: str
    #: Normalised user code, for the browser-side lookup.
    user_code: str
    status: GrantStatus
    created_at: int
    #: Epoch seconds; also the DynamoDB ``ttl`` attribute.
    expires_at: int
    #: Set on approval. Not a credential on its own — sealing needs the codec key.
    session_id: Optional[str] = None
    #: Who approved it, for the audit trail and for logging.
    user_id: Optional[str] = None
    #: Last poll, for ``slow_down`` enforcement.
    last_polled_at: Optional[int] = None
    #: Total polls, so an abusive client is visible in logs.
    poll_count: int = 0

    def is_expired(self, now: Optional[int] = None) -> bool:
        """True once the grant may no longer be approved or claimed.

        Checked in application code as well as relying on the DynamoDB TTL:
        TTL deletion is asynchronous and documented as taking up to 48 hours,
        so an expired row can still be read long after it should be usable.
        """
        return (now if now is not None else int(time.time())) >= self.expires_at

    def is_claimable(self, now: Optional[int] = None) -> bool:
        """True when a poll should hand over the session value."""
        return self.status is GrantStatus.APPROVED and self.session_id is not None and not self.is_expired(now)

    def is_approvable(self, now: Optional[int] = None) -> bool:
        """True when a browser may still approve this grant."""
        return self.status is GrantStatus.PENDING and not self.is_expired(now)

    def should_slow_down(self, now: Optional[int] = None) -> bool:
        """True when the client is polling faster than it was told to.

        Answering ``slow_down`` rather than the real outcome is what keeps a
        tight poll loop from becoming a user-code guessing amplifier.
        """
        if self.last_polled_at is None:
            return False
        return (now if now is not None else int(time.time())) - self.last_polled_at < MIN_POLL_GAP_SECONDS


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class DeviceAuthorizationResponse(BaseModel):
    """Answer to ``POST /auth/cli/authorize``. Mirrors RFC 8628 field names."""

    device_code: str = Field(..., description="Secret the client keeps and presents when polling")
    user_code: str = Field(..., description="Short code the human enters in the browser")
    verification_uri: str = Field(..., description="Page the human should open")
    verification_uri_complete: str = Field(..., description="Same page with the user code prefilled")
    expires_in: int = Field(..., description="Seconds until the grant expires")
    interval: int = Field(..., description="Seconds the client should wait between polls")


class DeviceTokenRequest(BaseModel):
    """Body of ``POST /auth/cli/token``."""

    device_code: str = Field(..., min_length=16, max_length=256)


class DeviceTokenResponse(BaseModel):
    """A successful claim. The session value is returned exactly once."""

    session: str = Field(..., description="Sealed session value; send as `Authorization: BFF <value>`")
    expires_in: int = Field(..., description="Seconds until the session expires without use")
    user_id: str = Field(..., description="Cognito sub of the authenticated user")
    username: str = Field(..., description="Cognito username of the authenticated user")


class DevicePendingResponse(BaseModel):
    """Not yet approved. ``error`` uses RFC 8628 codes so clients can branch."""

    error: str = Field(
        ...,
        description=(
            "authorization_pending | slow_down | expired_token | access_denied " "| invalid_grant (unknown device code, or one already claimed)"
        ),
    )
    error_description: str = Field(..., description="Human-readable explanation")
