"""PKCE (RFC 7636) parameter generation.

The CLI is a *public* client: it ships no secret, because anything embedded in a
distributed binary is not a secret. PKCE is what makes the authorization-code
grant safe without one — the authorization code is only redeemable by whoever
holds the original verifier, so intercepting the code is not enough.

Cognito supports S256 and it is the only method used here. `plain` is
deliberately not implemented: it offers no protection against interception of
the authorization request itself.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass, field

#: RFC 7636 allows 43-128 characters. 64 random bytes of url-safe base64 lands
#: comfortably inside that and gives 512 bits of entropy.
_VERIFIER_BYTES = 48

CODE_CHALLENGE_METHOD = "S256"


def _b64url(raw: bytes) -> str:
    """Base64url-encode without padding, per RFC 7636 Appendix A."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def generate_verifier() -> str:
    """Return a fresh code verifier."""
    return _b64url(secrets.token_bytes(_VERIFIER_BYTES))


def challenge_for(verifier: str) -> str:
    """Return the S256 challenge for a verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _b64url(digest)


def generate_state() -> str:
    """Return an opaque state value.

    Guards against a forged callback: the loopback listener accepts a redirect
    only when the state echoed back matches the one this process generated.
    """
    return _b64url(secrets.token_bytes(32))


@dataclass(frozen=True, slots=True)
class PkceChallenge:
    """One authorization attempt's PKCE material.

    ``verifier`` never leaves the process except in the token exchange, and is
    excluded from ``repr`` so it cannot land in a log or traceback.
    """

    verifier: str = field(repr=False)
    challenge: str
    state: str
    method: str = CODE_CHALLENGE_METHOD

    @classmethod
    def create(cls) -> PkceChallenge:
        verifier = generate_verifier()
        return cls(
            verifier=verifier,
            challenge=challenge_for(verifier),
            state=generate_state(),
        )
