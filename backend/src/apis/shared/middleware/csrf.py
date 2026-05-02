"""CSRFMiddleware — guards unsafe-method requests bound to a BFF session.

This middleware only enforces when `request.state.bff_session` is set —
i.e. when `SessionRefreshMiddleware` upstream has already resolved a valid
session cookie. Bearer-token requests (the entire pre-cutover SPA, plus
any direct API consumers like the API key flow) bypass this check entirely.

Pattern: double-submit cookie. The browser stores the CSRF token in
`__Host-bff_csrf` (readable by JS) and echoes it back in `X-CSRF-Token`
on each unsafe request. The middleware confirms both copies match each
other AND match the value derived from the session's secret. This rejects
classic cross-site form posts that ride the session cookie (the attacker
can't read the CSRF cookie value to put into the header) without breaking
same-origin XHR.

Phase 2 is dormant — no router yet sets the session cookie, so this is
effectively code waiting to be exercised by Phases 3 and 6.
"""

from __future__ import annotations

import logging

from fastapi import status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from apis.shared.sessions_bff.config import CSRF_COOKIE_NAME, CSRF_HEADER_NAME
from apis.shared.sessions_bff.csrf import CSRFHelper

logger = logging.getLogger(__name__)

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Endpoints that the BFF itself owns and which (a) initiate the session
# (login/callback), or (b) are explicitly designed to be hit cross-origin
# pre-session. Listed by exact path to avoid a permissive prefix match.
_EXEMPT_PATHS = frozenset(
    {
        "/auth/login",
        "/auth/callback",
        "/auth/logout",  # logout will rotate cookies even on stale CSRF
    }
)


class CSRFMiddleware(BaseHTTPMiddleware):
    """Reject unsafe-method requests that ride the session cookie without
    a matching CSRF token.

    Returns 403 instead of 401 — semantics: the user *is* authenticated,
    but the request is not authorized as user-initiated.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method in _SAFE_METHODS:
            return await call_next(request)

        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        # `state.bff_session` is only set by SessionRefreshMiddleware when a
        # valid cookie was presented. Anything else — Bearer-token requests,
        # anonymous public endpoints — bypasses CSRF entirely.
        session = getattr(request.state, "bff_session", None)
        if session is None:
            return await call_next(request)

        header_token = request.headers.get(CSRF_HEADER_NAME, "")
        cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")

        if not CSRFHelper.validate(
            session.csrf_secret,
            session.session_id,
            header_token,
            cookie_token,
        ):
            logger.warning(
                "CSRF validation failed for %s %s",
                request.method,
                request.url.path,
            )
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "CSRF token missing or invalid."},
            )

        return await call_next(request)
