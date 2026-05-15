"""Cookie writers for the BFF auth routes.

One module so the attribute set is identical wherever cookies are set or
cleared. The `__Host-` prefix is enforced by the browser, but the server
must hold its end up: `Path=/`, `Secure`, no `Domain` attribute. We pin
those here so individual route handlers can't drift.
"""

from __future__ import annotations

from starlette.responses import Response

from apis.shared.sessions_bff.config import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME

# `lax` allows top-level navigation flows (the OAuth redirect chain lands on
# /auth/callback as a GET, which is in-scope for `lax`) while blocking the
# CSRF-relevant cross-site POSTs. `strict` would break the callback redirect
# coming from cognito's domain.
_SAMESITE = "lax"


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
