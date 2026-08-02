"""Admin API routes for reading the administrative audit trail.

**Non-delegable — these routes keep bare ``require_admin`` on purpose.** The
audit log records what admins do to roles, including attempts the write-through
guard refused. Handing that view out would mean an admin can be granted the
ability to watch other admins, and — more to the point — a delegated admin whose
own denied escalation attempts are recorded has an obvious interest in reading
(and noticing) that trail. The matching registry entry is ``admin.audit``, marked
``delegable=False`` in ``apis/shared/rbac/admin_scopes.py``.

Read-only by design. There is no delete or edit endpoint and there should never
be one; records age out via the table's TTL (`RETENTION_DAYS`) and by no other
means.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from apis.shared.audit import ALL_ACTIONS, TARGET_APP_ROLE, AuditRepository
from apis.shared.audit.repository import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from apis.shared.auth import User, require_admin
from apis.shared.security.log_sanitize import scrub_log
from apis.shared.timestamps import utc_now_iso

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audit", tags=["admin-audit"])

_repository: Optional[AuditRepository] = None


def get_audit_repository() -> AuditRepository:
    global _repository
    if _repository is None:
        _repository = AuditRepository()
    return _repository


def _encode_cursor(key: Optional[Dict[str, Any]]) -> Optional[str]:
    """DynamoDB's LastEvaluatedKey as an opaque string.

    Base64 rather than raw JSON so nothing downstream is tempted to read or
    construct one — the shape is an implementation detail of the index being
    queried, and it changes if the keys ever do.
    """
    if not key:
        return None
    return base64.urlsafe_b64encode(json.dumps(key).encode()).decode()


def _decode_cursor(cursor: Optional[str]) -> Optional[Dict[str, Any]]:
    if not cursor:
        return None
    try:
        decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid cursor.",
        )
    if not isinstance(decoded, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid cursor.",
        )
    return decoded


def _page(records, next_key) -> Dict[str, Any]:
    return {
        "records": [r.to_response() for r in records],
        "nextCursor": _encode_cursor(next_key),
    }


@router.get("/actions")
async def list_actions(admin: User = Depends(require_admin)) -> Dict[str, Any]:
    """The closed set of audited action ids, for the console's filter control."""
    return {"actions": sorted(ALL_ACTIONS)}


@router.get("/")
async def list_recent(
    month: Optional[str] = Query(
        None,
        pattern=r"^\d{4}-\d{2}$",
        description="Month to read, YYYY-MM. Defaults to the current month.",
    ),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: Optional[str] = Query(None),
    admin: User = Depends(require_admin),
) -> Dict[str, Any]:
    """Recent administrative activity, newest first.

    Paginates within one month — the partition is month-sharded, so exhausting a
    month means asking for the previous one rather than following a cursor. The
    response carries `month` so the console knows which bucket it is in without
    tracking the default it did not send.
    """
    target_month = month or utc_now_iso()[:7]
    # Decoded outside the try: `_decode_cursor` raises a deliberate 400, and
    # HTTPException is an Exception — inside the block the handler below would
    # swallow it and report a bad request as a service outage.
    start_key = _decode_cursor(cursor)
    try:
        records, next_key = get_audit_repository().list_recent(
            target_month, limit=limit, cursor=start_key
        )
    except Exception:
        logger.exception("Failed to read recent audit records")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Audit log is unavailable.",
        )
    return {**_page(records, next_key), "month": target_month}


@router.get("/targets/{target_id}")
async def list_for_target(
    target_id: str,
    target_type: str = Query(TARGET_APP_ROLE),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: Optional[str] = Query(None),
    admin: User = Depends(require_admin),
) -> Dict[str, Any]:
    """Full history for one target — "what has happened to this role?"."""
    start_key = _decode_cursor(cursor)  # outside the try — see `list_recent`
    try:
        records, next_key = get_audit_repository().list_for_target(
            target_type, target_id, limit=limit, cursor=start_key
        )
    except Exception:
        logger.exception(f"Failed to read audit history for {scrub_log(target_id)}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Audit log is unavailable.",
        )
    return _page(records, next_key)


@router.get("/actors/{actor_user_id}")
async def list_for_actor(
    actor_user_id: str,
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    cursor: Optional[str] = Query(None),
    admin: User = Depends(require_admin),
) -> Dict[str, Any]:
    """Everything one admin did — "what has this person been doing?"."""
    start_key = _decode_cursor(cursor)  # outside the try — see `list_recent`
    try:
        records, next_key = get_audit_repository().list_for_actor(
            actor_user_id, limit=limit, cursor=start_key
        )
    except Exception:
        logger.exception(f"Failed to read audit history for actor {scrub_log(actor_user_id)}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Audit log is unavailable.",
        )
    return _page(records, next_key)
