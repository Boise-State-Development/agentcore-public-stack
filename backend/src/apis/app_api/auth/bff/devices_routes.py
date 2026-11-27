"""``/auth/devices`` — the caller's signed-in apps and terminals (docs/specs/native-app-auth.md D9).

A device is a session handed to a native app (``POST /auth/native/exchange``) or
to the terminal client (``/auth/cli/*``). Browser sessions are not listed: the
web app signs itself out with ``/auth/logout``.

The list reads the user's device pointers (``SessionRepository.list_device_pointers``)
and then each pointer's session row, which is the truth: a pointer whose row is
gone (signed out, expired, revoked) is dropped as it is read. A client only ever
sees a derived ``deviceId``, never a session id.

Signing a device out ends its session exactly as ``POST /auth/logout`` would for
that client (``end_device_session``):

- ``SessionRepository.revoke``: the row is deleted and the revocation published,
  so every app-api task stops serving a cached copy within one poll;
- the refresh token is revoked with Cognito, **unless the user's headless grant
  holds that token** (``grant_keeps_token``). Then the token stays alive for the
  grant's scheduled runs, and the answer says so (``scheduledRunsContinue``), as
  logout's does. The grant itself is never touched here; it is revoked on
  ``/schedules``.

This module imports from ``routes`` and never the other way around.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional

import re

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.sessions_bff.cache import get_default_cache
from apis.shared.sessions_bff.models import DevicePointer, SessionRecord
from apis.shared.sessions_bff.repository import SessionRepository

from .config import BFFAuthConfig
from .grant_tokens import grant_keeps_token
# Through the module, so the factories the logout tests patch are the ones used here.
from . import routes as bff_routes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/devices", tags=["auth-devices"])

#: Checked inside the route, after authentication, so an unauthenticated caller
#: gets a 401 whatever id it sends.
_DEVICE_ID = re.compile(r"^[0-9a-f]{32}$")


class DeviceView(BaseModel):
    """One signed-in app or terminal."""

    model_config = ConfigDict(populate_by_name=True)

    device_id: str = Field(..., alias="deviceId")
    kind: str = Field(..., description="native or terminal")
    device_model: Optional[str] = Field(None, alias="deviceModel", description="As the app reported it")
    app_version: Optional[str] = Field(None, alias="appVersion")
    signed_in_at: str = Field(..., alias="signedInAt")
    last_used_at: Optional[str] = Field(None, alias="lastUsedAt")
    expires_at: Optional[str] = Field(None, alias="expiresAt")
    current: bool = Field(False, description="The session making this request")


class DevicesResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    devices: List[DeviceView] = Field(..., description="Most recently used first")


class DeviceRevokedResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    revoked: int = Field(..., description="Sessions signed out")
    scheduled_runs_continue: bool = Field(
        False,
        alias="scheduledRunsContinue",
        description="A signed-out session's token is still held by the user's headless grant",
    )


def _iso(epoch: Optional[int]) -> Optional[str]:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _repository() -> SessionRepository:
    return bff_routes._get_repository(BFFAuthConfig.from_env())


async def _live_devices(repository: SessionRepository, user_id: str) -> List[tuple[DevicePointer, SessionRecord]]:
    """The user's pointers whose session row is still there, dropping the rest."""
    pointers = await repository.list_device_pointers(user_id)
    rows = await asyncio.gather(*(repository.get(p.session_id) for p in pointers), return_exceptions=True)
    live: List[tuple[DevicePointer, SessionRecord]] = []
    for pointer, row in zip(pointers, rows):
        if isinstance(row, BaseException):
            # Unreadable now: keep it listed rather than hide a live session.
            logger.warning("Could not read a device's session row: %s", row)
            continue
        if row is None or row.user_id != user_id:
            try:
                await repository.delete_device_pointer(user_id, pointer.device_id)
            except Exception:
                logger.debug("Could not drop a stale device pointer", exc_info=True)
            continue
        live.append((pointer, row))
    return live


async def list_devices(user_id: str, *, current_session_id: Optional[str] = None) -> List[DeviceView]:
    live = await _live_devices(_repository(), user_id)
    views = [
        DeviceView(
            device_id=pointer.device_id,
            kind=pointer.kind,
            device_model=pointer.device_model,
            app_version=pointer.app_version,
            signed_in_at=_iso(row.created_at) or "",
            last_used_at=_iso(row.last_seen_at),
            expires_at=_iso(row.ttl),
            current=row.session_id == current_session_id,
        )
        for pointer, row in live
    ]
    views.sort(key=lambda view: view.last_used_at or view.signed_in_at, reverse=True)
    return views


async def end_device_session(repository: SessionRepository, pointer: DevicePointer) -> bool:
    """Sign one device out. Returns whether the user's headless grant kept its token."""
    record: Optional[SessionRecord] = None
    try:
        record = await repository.get(pointer.session_id)
    except Exception:
        logger.warning("Could not read a device's session row before revoking it", exc_info=True)
    if record is not None and record.user_id != pointer.user_id:
        # Never end a session the pointer's user doesn't own.
        logger.error("Device pointer names another user's session; refusing to revoke it")
        return False

    # The row and the revocation feed, then this task's cache at once.
    await repository.revoke(pointer.session_id)
    try:
        get_default_cache().invalidate(pointer.session_id)
    except Exception:
        logger.debug("Local cache invalidation skipped for a revoked device")
    try:
        await repository.delete_device_pointer(pointer.user_id, pointer.device_id)
    except Exception:
        logger.debug("Could not delete a revoked device's pointer; it is dropped on the next read", exc_info=True)

    if record is None:
        return False
    if await grant_keeps_token(record, bff_routes._get_grant_service()):
        logger.info("A revoked device's refresh token is kept: the user's headless grant holds it")
        return True
    await bff_routes._revoke_refresh_token(record.cognito_refresh_token, session_id=pointer.session_id)
    return False


async def revoke_device(user_id: str, device_id: str) -> Optional[DeviceRevokedResponse]:
    """Sign out one of the user's devices; None when the user has no such device."""
    repository = _repository()
    pointer = await repository.get_device_pointer(user_id, device_id)
    if pointer is None:
        return None
    kept = await end_device_session(repository, pointer)
    logger.info("Device signed out by its owner", extra={"event": "device_revoked", "kind": pointer.kind})
    return DeviceRevokedResponse(revoked=1, scheduled_runs_continue=kept)


async def revoke_all_devices(user_id: str) -> DeviceRevokedResponse:
    """Sign out every app and terminal the user has (the admin route)."""
    repository = _repository()
    live = await _live_devices(repository, user_id)
    kept = False
    for pointer, _ in live:
        kept = await end_device_session(repository, pointer) or kept
    return DeviceRevokedResponse(revoked=len(live), scheduled_runs_continue=kept)


@router.get("", response_model=DevicesResponse, response_model_by_alias=True)
async def get_devices(
    request: Request,
    user: User = Depends(get_current_user_from_session),
) -> DevicesResponse:
    """The caller's signed-in apps and terminals, most recently used first."""
    current = getattr(request.state, "bff_session", None)
    devices = await list_devices(
        user.user_id, current_session_id=current.session_id if current is not None else None
    )
    return DevicesResponse(devices=devices)


@router.delete("/{device_id}", response_model=DeviceRevokedResponse, response_model_by_alias=True)
async def delete_device(
    device_id: str,
    user: User = Depends(get_current_user_from_session),
) -> DeviceRevokedResponse:
    """Sign out one of the caller's apps or terminals. 404 for anything else."""
    result = await revoke_device(user.user_id, device_id) if _DEVICE_ID.match(device_id) else None
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    return result
