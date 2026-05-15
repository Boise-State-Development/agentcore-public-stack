"""CSRF helpers (double-submit cookie pattern).

Each session row carries a per-session `csrf_secret`. The current CSRF token
is `HMAC_SHA256(csrf_secret, session_id)` truncated to 32 hex chars and is
mirrored in two places:

    1. The `__Host-bff_csrf` cookie (readable by JS — that's the point)
    2. The `X-CSRF-Token` request header on every unsafe-method request

The middleware compares the two with `hmac.compare_digest`. A cross-origin
attacker can ride the `__Host-bff_session` cookie (browsers send it) but
cannot read the `__Host-bff_csrf` cookie value to mirror it into the header,
because same-origin policy blocks reads of other-origin script responses.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets


def generate_secret() -> str:
    """A fresh per-session CSRF secret. Stored on the session record."""
    return secrets.token_urlsafe(32)


def derive_token(secret: str, session_id: str) -> str:
    """Stable token a client mirrors between cookie and header.

    Re-derived on every request from the session's secret, so rotation is
    free: rotating the secret invalidates outstanding tokens immediately.
    """
    digest = hmac.new(
        secret.encode("utf-8"),
        session_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:32]


def tokens_match(a: str, b: str) -> bool:
    """Constant-time equality once both sides are non-empty.

    The empty/None short-circuit is *not* constant-time — it leaks "one
    input was empty" via timing. That's fine here: an attacker who omits
    the header or cookie already knows they did, so there's nothing to
    learn. Once both inputs have content, `hmac.compare_digest` covers
    the cryptographically meaningful comparison.
    """
    if not a or not b:
        return False
    return hmac.compare_digest(a, b)


class CSRFHelper:
    """Thin OO wrapper used by the middleware.

    Kept as a class so the middleware is easy to inject in tests, but the
    stateless functions above are the actual primitives.
    """

    @staticmethod
    def generate_secret() -> str:
        return generate_secret()

    @staticmethod
    def derive_token(secret: str, session_id: str) -> str:
        return derive_token(secret, session_id)

    @staticmethod
    def validate(secret: str, session_id: str, header_token: str, cookie_token: str) -> bool:
        if not tokens_match(header_token, cookie_token):
            return False
        expected = derive_token(secret, session_id)
        return tokens_match(header_token, expected)
