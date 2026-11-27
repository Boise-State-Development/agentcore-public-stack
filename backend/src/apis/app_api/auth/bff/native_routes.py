"""Native-app sign-in: trade a handoff code for a sealed session.

``POST /auth/native/exchange``
    The second half of the native sign-in (docs/specs/native-app-auth.md D3).
    ``GET /auth/login?client=native`` runs the ordinary BFF login inside the
    system's web-authentication sheet, and the callback's native exit ends on
    ``<redirect_uri>?handoff=<code>``. The app posts that code here with the
    PKCE verifier it kept in memory, and gets back the sealed session it will
    present as ``Authorization: BFF <sealed>`` — the terminal's transport, so
    every user-facing route already accepts it.

**Unauthenticated by necessity**, like ``/auth/cli/token``: the caller holds
no session yet. What it holds instead is the pair the callback bound
together: a 256-bit, single-use, 120-second handoff code, and the verifier
whose S256 digest the login recorded. A custom-scheme redirect can be
delivered to any app that registers the scheme, which is why the code alone
is worthless.

The route answers 404 while ``NATIVE_AUTH_REDIRECT_URIS`` is empty, so a
deployment that has not configured a native app shows no trace of one.

This module imports from ``routes`` and never the other way around.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.rate_limit import RateLimiter
from apis.shared.sessions_bff.csrf import CSRFHelper
from apis.shared.sessions_bff.models import DEVICE_KIND_NATIVE, CookiePayload, DevicePointer

from .cli_routes import _client_key, _enforce
from .config import BFFAuthConfig
from .routes import (
    put_device_pointer_quietly,
    _discard_orphan_session,
    _get_cookie_codec,
    _get_repository,
    _get_state_store,
    _require_ready,
    session_user_payload,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/native", tags=["auth-native"])

#: A person signs in on a phone a few times a day at most. The handoff's
#: entropy is the real control; this caps a scripted loop per address.
_EXCHANGE_WINDOW_SECONDS = 300
_EXCHANGE_MAX_REQUESTS = 20

#: Bounds on the self-reported device facts, which are display-only. Longer
#: values are cut rather than refused: a cosmetic field must never fail a
#: sign-in.
_DEVICE_MODEL_MAX = 64
_APP_VERSION_MAX = 32

_rate_limiter: Optional[RateLimiter] = None


def _get_rate_limiter(config: BFFAuthConfig) -> RateLimiter:
    """Counters live in the sessions table, as the CLI routes' do."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(table_name=config.bff_config.sessions_table_name)
    return _rate_limiter


def _reset_for_tests() -> None:
    global _rate_limiter
    _rate_limiter = None


class NativeExchangeRequest(BaseModel):
    """The app's half of the handoff. Extra fields are ignored."""

    handoff: str = Field(..., min_length=1, max_length=128)
    # RFC 7636 §4.1: 43..128 characters of the unreserved set.
    code_verifier: str = Field(
        ..., min_length=43, max_length=128, pattern=r"^[A-Za-z0-9._~-]+$"
    )
    device_model: Optional[str] = Field(default=None, max_length=1024)
    app_version: Optional[str] = Field(default=None, max_length=1024)


def _s256(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _bounded(value: Optional[str], limit: int) -> Optional[str]:
    """Printable characters only, trimmed and cut to `limit`; None if empty."""
    if value is None:
        return None
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()[:limit]
    return cleaned or None


def _invalid_grant() -> JSONResponse:
    """One answer for every refusal, so a caller learns nothing about which
    check failed. RFC 6749 §5.2's error code, as `/auth/cli/token` uses."""
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "error": "invalid_grant",
            "error_description": "The sign-in could not be completed. Start a new one.",
        },
    )


@router.post("/exchange", summary="Trade a native handoff code for a session")
async def native_exchange(request: Request, body: NativeExchangeRequest) -> JSONResponse:
    """Return `{session, expires_at, user}` for a valid handoff and verifier.

    The handoff row is taken (read and deleted) in one atomic step before any
    check, so a code is spent by its first presentation whether or not the
    verifier matches. A refused exchange that can name its session (expired,
    or the wrong verifier) deletes that row too: the only party who could
    have redeemed it has just failed to, and nobody else ever will.
    """
    config = BFFAuthConfig.from_env()
    if not config.native_auth_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    _require_ready(config)

    await _enforce(
        _get_rate_limiter(config),
        key=f"native-exchange:{_client_key(request)}",
        window=_EXCHANGE_WINDOW_SECONDS,
        limit=_EXCHANGE_MAX_REQUESTS,
    )

    handoff = _get_state_store().take_handoff(body.handoff)
    if handoff is None:
        # Unknown, or already spent. A spent code's session belongs to the
        # exchange that spent it, so there is nothing here to clean up.
        return _invalid_grant()

    now = int(time.time())
    if handoff.expires_at <= now:
        await _discard_orphan_session(handoff.session_id)
        return _invalid_grant()

    if not handoff.code_challenge or not secrets.compare_digest(
        _s256(body.code_verifier), handoff.code_challenge
    ):
        logger.warning("Native exchange refused: verifier does not match the challenge")
        await _discard_orphan_session(handoff.session_id)
        return _invalid_grant()

    repository = _get_repository(config)
    record = await repository.get(handoff.session_id)
    if record is None or not record.is_native:
        await _discard_orphan_session(handoff.session_id)
        return _invalid_grant()

    # Same user payload `GET /auth/session` returns, built by the same
    # dependency, so the access token is validated exactly as on every later
    # request.
    request.state.bff_session = record
    try:
        user = await get_current_user_from_session(request)
    except HTTPException:
        await _discard_orphan_session(handoff.session_id)
        return _invalid_grant()

    expires_at = record.created_at + config.bff_config.absolute_lifetime_seconds
    device_model = _bounded(body.device_model, _DEVICE_MODEL_MAX)
    app_version = _bounded(body.app_version, _APP_VERSION_MAX)
    activated = await repository.activate_native(
        record.session_id,
        ttl=expires_at,
        device_model=device_model,
        app_version=app_version,
    )
    if not activated:
        # The row vanished between the read and the write (a delete raced us).
        return _invalid_grant()

    # So the user can find and sign out this phone (`/auth/devices`).
    await put_device_pointer_quietly(
        repository,
        DevicePointer.for_session(
            record,
            kind=DEVICE_KIND_NATIVE,
            ttl=expires_at,
            device_model=device_model,
            app_version=app_version,
        ),
    )

    sealed = _get_cookie_codec(config).seal(CookiePayload(session_id=record.session_id))
    csrf_token = CSRFHelper.derive_token(record.csrf_secret, record.session_id)
    logger.info("Native session handed to the app for user %s", record.user_id)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "session": sealed,
            "expires_at": expires_at,
            "user": session_user_payload(user, csrf_token),
        },
        # The body is a credential.
        headers={"Cache-Control": "no-store"},
    )
