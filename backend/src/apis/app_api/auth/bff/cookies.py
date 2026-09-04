"""Cookie writers for the BFF auth routes.

One module so the attribute set is identical wherever cookies are set or
cleared. The `__Host-` prefix is enforced by the browser, but the server
must hold its end up: `Path=/`, `Secure`, no `Domain` attribute. We pin
those here so individual route handlers can't drift.
"""

from __future__ import annotations

from typing import Literal

from starlette.responses import Response

from apis.shared.sessions_bff.config import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME

# `lax` allows top-level navigation flows (the OAuth redirect chain lands on
# /auth/callback as a GET, which is in-scope for `lax`) while blocking the
# CSRF-relevant cross-site POSTs. `strict` would break the callback redirect
# coming from cognito's domain.
#
# Annotated as a `Literal` rather than a bare `str` so it satisfies Starlette's
# `samesite` parameter type — a plain `str` is rejected there.
_SAMESITE: Literal["lax"] = "lax"

# Carries the browser-binding secret for one in-flight OAuth authorization
# request. Its whole job is to make a `state` value worthless to any browser
# other than the one that asked for it: the callback hashes this cookie and
# compares against the digest stored alongside the state.
#
# Attribute notes:
#   - `__Host-` so the browser refuses it from any other host and pins
#     Path=/ + Secure with no Domain — an attacker on a sibling subdomain
#     can't plant one.
#   - `HttpOnly` — the SPA never needs to read it, and script access would
#     hand an XSS the ability to forge a binding.
#   - `SameSite=lax` is *required*, not a compromise: the IdP redirects the
#     user back with a top-level cross-site GET, which is exactly the case
#     `lax` still sends cookies for. `strict` would withhold the cookie and
#     break every login.
OAUTH_STATE_COOKIE_NAME = "__Host-bff_oauth_state"


def set_session_cookies(
    response: Response,
    *,
    sealed_session_value: str,
    csrf_token: str,
    max_age_seconds: int,
) -> None:
    """Write the session + CSRF cookies on a response.

    `sealed_session_value` must come from `CookieCodec.seal(...)` — never
    re-roll the seal at the call site.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=sealed_session_value,
        max_age=max_age_seconds,
        path="/",
        secure=True,
        httponly=True,
        samesite=_SAMESITE,
    )
    # CSRF cookie is intentionally readable by JS — that's how the SPA
    # mirrors it into the X-CSRF-Token header on unsafe requests.
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        max_age=max_age_seconds,
        path="/",
        secure=True,
        httponly=False,
        samesite=_SAMESITE,
    )


def clear_session_cookies(response: Response) -> None:
    """Drop both BFF cookies. Attribute set must match the writers above so
    the browser actually clears the right cookie and not a phantom twin
    that differs only in path or samesite."""
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite=_SAMESITE,
    )
    response.delete_cookie(
        CSRF_COOKIE_NAME,
        path="/",
        secure=True,
        httponly=False,
        samesite=_SAMESITE,
    )


def set_oauth_state_cookie(
    response: Response,
    *,
    binding_secret: str,
    max_age_seconds: int,
) -> None:
    """Hand the browser the binding secret for one in-flight authorize request.

    `binding_secret` must be a fresh high-entropy value from
    `secrets.token_urlsafe(...)`; only its SHA-256 digest is persisted with
    the OAuth state, so this cookie is the sole copy of the plaintext and
    lives only in the browser that initiated the flow.
    """
    response.set_cookie(
        key=OAUTH_STATE_COOKIE_NAME,
        value=binding_secret,
        max_age=max_age_seconds,
        path="/",
        secure=True,
        httponly=True,
        samesite=_SAMESITE,
    )


def clear_oauth_state_cookie(response: Response) -> None:
    """Drop the OAuth binding cookie once the flow terminates, successfully or
    otherwise. Cleared on *every* callback exit so a stale binding can't be
    paired with a freshly minted state on a later attempt."""
    response.delete_cookie(
        OAUTH_STATE_COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite=_SAMESITE,
    )
