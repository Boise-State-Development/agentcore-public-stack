"""Dictation ticket + WebSocket routes.

Same two-step transport as voice mode (``app_api/voice/routes.py``), for the
same reason: the browser WebSocket rides no middleware, so the upgrade is
gated by a single-use ticket minted on a cookie-authenticated, CSRF-checked
POST. The ticket carries ``purpose="dictation"`` and is bound to the user
alone — dictation into a brand-new conversation happens before any session
exists.

Past the gates, app-api presigns a Transcribe Streaming WebSocket URL with its
own task-role credentials and relays audio up and transcripts down
(``relay.py``). The browser never talks to AWS and never holds a credential.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import boto3
from botocore.credentials import ReadOnlyCredentials
from fastapi import APIRouter, Depends, HTTPException, WebSocket, status
from pydantic import BaseModel

from apis.app_api.voice.routes import authenticate_ticketed_websocket
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.feature_flags import dictation_enabled
from apis.shared.voice_ticket import PURPOSE_DICTATION, get_default_service

from .relay import relay_dictation_stream
from .transcribe import presign_stream_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dictation", tags=["dictation"])

_DEFAULT_LANGUAGES = ["en-US"]

# One dictation is capped so a forgotten open mic cannot bill indefinitely.
# Transcribe itself allows four hours per stream.
_DEFAULT_MAX_SECONDS = 300.0

_boto_session: Optional[boto3.Session] = None


def dictation_languages() -> list[str]:
    """Transcribe language codes from ``DICTATION_LANGUAGES`` (default ``en-US``).

    One code pins the language; two or more turn on language identification
    (see ``transcribe.language_query_params``).
    """
    raw = os.environ.get("DICTATION_LANGUAGES", "")
    languages = [code.strip() for code in raw.split(",") if code.strip()]
    return languages or list(_DEFAULT_LANGUAGES)


def dictation_max_seconds() -> float:
    raw = os.environ.get("DICTATION_MAX_SECONDS", "").strip()
    try:
        value = float(raw) if raw else _DEFAULT_MAX_SECONDS
    except ValueError:
        logger.warning("Ignoring non-numeric DICTATION_MAX_SECONDS=%r", raw)
        return _DEFAULT_MAX_SECONDS
    return value if value > 0 else _DEFAULT_MAX_SECONDS


def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"


def _credentials() -> ReadOnlyCredentials:
    """The task role's current credentials, frozen for one presign.

    A process-wide session so the container credential provider's refresh is
    shared across connections instead of re-resolved per dictation.
    """
    global _boto_session
    if _boto_session is None:
        _boto_session = boto3.Session()
    credentials = _boto_session.get_credentials()
    if credentials is None:
        raise RuntimeError("no AWS credentials available for Transcribe")
    return credentials.get_frozen_credentials()


def _require_enabled() -> None:
    if not dictation_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


# ─── POST /dictation/ticket ────────────────────────────────────────────


class DictationTicketResponse(BaseModel):
    ticket: str
    expires_in: int


@router.post(
    "/ticket",
    response_model=DictationTicketResponse,
    summary="Mint a single-use ticket for the dictation WebSocket upgrade",
    responses={
        401: {"description": "No active BFF session"},
        403: {"description": "CSRF token missing or invalid"},
        404: {"description": "Dictation is disabled in this environment"},
        503: {"description": "Ticket service is not configured"},
    },
)
async def issue_dictation_ticket(
    user: User = Depends(get_current_user_from_session),
) -> DictationTicketResponse:
    _require_enabled()
    try:
        service = get_default_service()
    except RuntimeError as exc:
        logger.error("Dictation ticket service not configured: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dictation is not configured.",
        )
    ticket, claims = service.issue(
        user_id=user.user_id,
        session_id="",
        purpose=PURPOSE_DICTATION,
    )
    return DictationTicketResponse(ticket=ticket, expires_in=claims.exp - claims.iat)


# ─── WebSocket /dictation/stream ───────────────────────────────────────


@router.websocket("/stream")
async def dictation_stream(websocket: WebSocket, ticket: Optional[str] = None) -> None:
    """Ticket-gated WebSocket; relays PCM to Transcribe and transcripts back."""
    if not dictation_enabled():
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="not found")
        return

    session_record = await authenticate_ticketed_websocket(
        websocket, ticket, purpose=PURPOSE_DICTATION
    )
    if session_record is None:
        return

    try:
        upstream_url = presign_stream_url(
            credentials=_credentials(),
            region=_region(),
            languages=dictation_languages(),
        )
    except Exception as exc:
        logger.error("Dictation presign failed: %s", exc, exc_info=True)
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR, reason="server error")
        return

    await websocket.accept()
    try:
        await relay_dictation_stream(
            client_ws=websocket,
            upstream_url=upstream_url,
            user_id=session_record.user_id,
            max_seconds=dictation_max_seconds(),
        )
    finally:
        try:
            await websocket.close()
        except Exception as exc:
            logger.debug("Dictation WS close failed during cleanup: %s", exc, exc_info=True)
