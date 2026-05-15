"""Voice ticket + WebSocket-proxy routes.

The two endpoints work as a pair:

* ``POST /voice/ticket`` runs through the standard BFF middleware stack
  (``SessionRefreshMiddleware`` → ``CSRFMiddleware``) so the call is
  cookie-authenticated and CSRF-checked. It mints a ticket bound to the
  caller's user_id and chosen ``session_id`` and returns it to the SPA.

* ``WebSocket /voice/stream`` does *not* benefit from the HTTP-only middleware
  stack — Starlette's ``BaseHTTPMiddleware`` and the CSRF middleware skip
  WebSocket scope. So the WS route re-implements the auth checks inline:
  read + unseal the session cookie, look up the session row, verify the
  ticket signature/expiry, mark the ticket consumed, confirm the ticket's
  bound session matches the cookie's session, and confirm the request's
  Origin is allowlisted (the browser-WS CSRF defense).

Past those checks, the route hands off to ``proxy.relay_voice_stream``, which
opens the upstream WS to the AgentCore Runtime using the BFF-stored Cognito
access token and pumps frames in both directions.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, status
from pydantic import BaseModel, Field

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.sessions_bff.config import SESSION_COOKIE_NAME
from apis.shared.sessions_bff.cookie import CookieDecodeError, get_default_codec
from apis.shared.sessions_bff.repository import SessionRepository
from apis.shared.voice_ticket import VoiceTicketError, get_default_service

from .proxy import relay_voice_stream

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/voice", tags=["voice"])


# ─── POST /voice/ticket ────────────────────────────────────────────────


class VoiceTicketRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=128)


class VoiceTicketResponse(BaseModel):
    ticket: str
    expires_in: int


@router.post(
    "/ticket",
    response_model=VoiceTicketResponse,
    summary="Mint a single-use ticket for the voice WebSocket upgrade",
    responses={
        401: {"description": "No active BFF session"},
        403: {"description": "CSRF token missing or invalid"},
        503: {"description": "Voice ticket service is not configured"},
    },
)
async def issue_voice_ticket(
    body: VoiceTicketRequest,
    user: User = Depends(get_current_user_from_session),
) -> VoiceTicketResponse:
    """Issue an HMAC-signed ticket bound to ``{user_id, session_id}``.

    Caller has already passed cookie auth (``get_current_user_from_session``)
    and CSRF (``CSRFMiddleware``). The ticket TTL is deliberately short — 60
    seconds is enough to hand off to the WebSocket upgrade and not much more.
    The SPA fetches a fresh ticket per voice connection; reusing a ticket is
    rejected as replay by the WS endpoint.
    """
    try:
        service = get_default_service()
    except RuntimeError as exc:
        logger.error("Voice ticket service not configured: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Voice service is not configured.",
        )

    ticket, claims = service.issue(
        user_id=user.user_id,
        session_id=body.session_id,
    )
    return VoiceTicketResponse(ticket=ticket, expires_in=claims.exp - claims.iat)


# ─── WebSocket /voice/stream ───────────────────────────────────────────


def _allowed_origins() -> set[str]:
    raw = os.environ.get("CORS_ORIGINS", "")
    return {o.strip() for o in raw.split(",") if o.strip()}


def _sanitize_for_log(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return value.replace("\r", "").replace("\n", "")


def _is_origin_allowed(origin: Optional[str]) -> bool:
    """Browser-WS CSRF defense: the Origin header is set by the browser and
    cannot be forged by JS. Allowlist must be non-empty in production — an
    empty allowlist returns False so a misconfigured deploy fails closed.
    """
    allowed = _allowed_origins()
    if not allowed:
        return False
    return origin is not None and origin in allowed


_session_repository: Optional[SessionRepository] = None


def _get_session_repository() -> SessionRepository:
    global _session_repository
    if _session_repository is None:
        _session_repository = SessionRepository()
    return _session_repository


@router.websocket("/stream")
async def voice_stream(websocket: WebSocket, ticket: Optional[str] = None) -> None:
    """Cookie + ticket gated WebSocket; relays to the AgentCore Runtime.

    Auth flow runs *before* the ``accept`` so a rejected connection closes
    cleanly without the SPA seeing a half-open socket. After acceptance,
    everything is plumbing — the relay handles its own teardown.
    """
    # Browser-WS CSRF defense — Origin is set by the browser, not JS.
    origin = websocket.headers.get("origin")
    if not _is_origin_allowed(origin):
        logger.warning("Voice WS rejected: origin %r not in CORS_ORIGINS", _sanitize_for_log(origin))
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="origin not allowed")
        return

    if not ticket:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="ticket required")
        return

    # Resolve the BFF session from the cookie. WebSockets don't run
    # SessionRefreshMiddleware, so we replicate the cookie unseal + DDB
    # lookup here. Refresh-on-near-expiry is intentionally skipped: the
    # ticket is short-lived and the WS connection is bounded by the access
    # token's remaining lifetime — let the next REST hit refresh.
    sealed_cookie = websocket.cookies.get(SESSION_COOKIE_NAME)
    if not sealed_cookie:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="no session")
        return

    try:
        codec = get_default_codec()
        cookie_payload = codec.unseal(sealed_cookie)
    except CookieDecodeError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="bad session")
        return
    except Exception as exc:
        logger.error("Voice WS cookie unseal error: %s", exc, exc_info=True)
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="server error")
        return

    repository = _get_session_repository()
    if not repository.enabled:
        logger.error("Voice WS rejected: BFF session repository not configured")
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="server error")
        return
    try:
        session_record = await repository.get(cookie_payload.session_id)
    except Exception as exc:
        logger.error("Voice WS session lookup error: %s", exc, exc_info=True)
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="server error")
        return
    if session_record is None:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="session expired")
        return

    # Verify + consume the ticket. Replay attempts surface as VoiceTicketError.
    try:
        service = get_default_service()
    except RuntimeError as exc:
        logger.error("Voice WS rejected: ticket service not configured: %s", exc)
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="server error")
        return

    try:
        claims = await service.verify_and_consume(ticket)
    except VoiceTicketError as exc:
        logger.info("Voice WS ticket rejected: %s", exc)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid ticket")
        return
    except Exception as exc:
        logger.error("Voice WS ticket verify error: %s", exc, exc_info=True)
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="server error")
        return

    if claims.user_id != session_record.user_id:
        # Defense in depth: a leaked ticket from a different user can't be
        # used even if presented with this user's cookie. The cookie's
        # session_id is the authoritative identity for the WS connection.
        logger.warning(
            "Voice WS rejected: ticket user_id %s does not match session user_id",
            claims.user_id,
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="ticket mismatch")
        return

    await websocket.accept()

    try:
        await relay_voice_stream(
            client_ws=websocket,
            cognito_access_token=session_record.cognito_access_token,
            user_id=session_record.user_id,
        )
    finally:
        try:
            await websocket.close()
        except Exception as exc:
            logger.debug("Voice WS close failed during cleanup: %s", exc, exc_info=True)


def _reset_for_tests() -> None:
    global _session_repository
    _session_repository = None
