"""``/notifications`` — the signed-in user's in-app inbox (shared-projects §5, PR-1.7).

Addressed by the caller's email, the same identity project membership uses, so
an invitation sent before someone ever signed in is waiting when they do. Who
did it is stored as an email and named on read (``actorName``, from the
directory), so a name is never older than the directory's.
Not behind ``PROJECTS_ENABLED``: the inbox is generic, and while projects are
off it is simply empty rather than a 404 the SPA's badge would have to handle.

A scheduled run that paused on the user leaves an ``agent_needs_*`` row, which
can be answered here without opening its conversation
(docs/specs/answer-from-inbox.md §3.4–3.5): ``GET /{id}/prompts`` reads the
live prompts and ``POST /{id}/respond`` claims the row, renews the headless
grant, audits, and async-invokes the scheduled-runs worker, which resumes the
run. Both 404 while ``PROACTIVE_RUNS_ENABLED`` is off. Listing settles rows
whose pause went another way (answered in the conversation, or lapsed), so a
wait stops counting toward the badge once it is over.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from apis.app_api.runs import routes as runs_routes
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.directory import display_names
from apis.shared.feature_flags import proactive_runs_enabled
from apis.shared.handoff.models import RelayedPrompt
from apis.shared.harness import GovernanceFloor, RunResult
from apis.shared.notifications import Notification, NotificationService
from apis.shared.notifications.models import NotificationResolution
from apis.shared.scheduled_prompts import inbox_answer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])

_service: Optional[NotificationService] = None


def _svc() -> NotificationService:
    global _service
    if _service is None:
        _service = NotificationService()
    return _service


class NotificationResponse(Notification):
    actor_name: Optional[str] = Field(None, alias="actorName", description="Null when the directory has no name")


class NotificationsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    notifications: List[NotificationResponse] = Field(..., description="Newest first")
    unread_count: int = Field(..., alias="unreadCount")
    next_cursor: Optional[str] = Field(None, alias="nextCursor")


class MarkAllReadResponse(BaseModel):
    marked: int


@router.get("", response_model=NotificationsResponse, response_model_by_alias=True)
async def list_notifications(
    limit: int = Query(20, ge=1, le=100),
    cursor: Optional[str] = Query(None, max_length=64),
    unread_only: bool = Query(False, alias="unreadOnly"),
    user: User = Depends(get_current_user_from_session),
) -> NotificationsResponse:
    svc = _svc()
    try:
        items, next_cursor = await asyncio.to_thread(
            svc.list, user.email, limit=limit, cursor=cursor, unread_only=unread_only
        )
        # A wait past its expiry reads "Expired" from here on (answer-from-inbox §3.8).
        await asyncio.to_thread(svc.expire_lapsed, user.email, items)
        if proactive_runs_enabled():
            # A wait answered in its conversation settles here, so the badge stops counting it
            # (§3.3): one META read per row still waiting on this page, usually none.
            await inbox_answer.settle_unanswered_rows(svc, user.email, user.user_id, items)
        unread = await asyncio.to_thread(svc.unread_count, user.email)
    except Exception:
        logger.exception("Failed to read notifications")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Notifications are unavailable.")
    names = display_names({n.actor_email for n in items if n.actor_email})
    return NotificationsResponse(
        notifications=[
            NotificationResponse(**n.model_dump(), actor_name=names.get(n.actor_email or "")) for n in items
        ],
        unread_count=unread,
        next_cursor=next_cursor,
    )


@router.post("/read-all", response_model=MarkAllReadResponse)
def mark_all_read(user: User = Depends(get_current_user_from_session)) -> MarkAllReadResponse:
    return MarkAllReadResponse(marked=_svc().mark_all_read(user.email))


@router.post("/{notification_id}/read", status_code=status.HTTP_204_NO_CONTENT)
def mark_read(notification_id: str, user: User = Depends(get_current_user_from_session)) -> None:
    """Mark one notification read. Idempotent; 404 if it is not in the caller's inbox."""
    if not _svc().mark_read(user.email, notification_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")


# ─── Answering a scheduled run's pause (answer-from-inbox §3.4–3.5) ─────────

_NOTIFICATION_ID = Path(..., min_length=1, max_length=64, pattern=r"^[0-9]{13}-[0-9a-f]{12}$")


class InboxPromptsResponse(BaseModel):
    """The live prompts behind a needs-user row, in the relay's wire shape."""

    model_config = ConfigDict(populate_by_name=True)

    notification_id: str = Field(..., alias="notificationId")
    session_id: str = Field(..., alias="sessionId")
    prompts: List[RelayedPrompt]


class InboxAnswer(BaseModel):
    """One answer, in the shape the conversation's own card posts."""

    model_config = ConfigDict(populate_by_name=True)

    interrupt_id: str = Field(..., alias="interruptId", min_length=1, max_length=512)
    response: Any = None


class RespondRequest(BaseModel):
    responses: List[InboxAnswer] = Field(..., min_length=1, max_length=5)


class RespondResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    resume_run_id: str = Field(..., alias="resumeRunId", description="Poll GET /runs/{runId} for the outcome")
    resolution: NotificationResolution


def _refused(code: int, reason: str, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail={"code": reason, "message": message})


def _already_answered() -> HTTPException:
    return _refused(status.HTTP_409_CONFLICT, "already_answered", "This was already answered.")


def _expired() -> HTTPException:
    return _refused(status.HTTP_410_GONE, "expired", "This expired before it was answered.")


async def _waiting_row(notification_id: str, user: User) -> Notification:
    """The caller's own needs-user row, still waiting (or given back to ``retry``), else the refusal.

    404 while the flag is off (after authentication), for another user's id, and for any other
    kind. A row being applied, or settled, is ``already_answered``; an expired one, 410.
    """
    if not proactive_runs_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    notification = await asyncio.to_thread(_svc().get, user.email, notification_id)
    if not inbox_answer.is_needs_user(notification) or inbox_answer.row_session_id(notification) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    resolution = notification.resolution
    if resolution is not None and resolution.state == "expired":
        raise _expired()
    if resolution is not None and resolution.state not in ("retry", "applying"):
        raise _already_answered()
    return notification


async def _live_pause(notification: Notification, user: User) -> inbox_answer.InboxPause:
    """The row's pause under ``(session, caller)``; a gone or lapsed one settles the row and refuses."""
    from apis.shared.sessions.metadata import load_session_meta

    session_id = inbox_answer.row_session_id(notification) or ""
    meta = await load_session_meta(session_id, user.user_id)
    pause = inbox_answer.read_inbox_pause(notification, meta.row)
    if pause.state == "live":
        return pause
    if notification.resolution is None:
        await inbox_answer.settle(_svc(), user.email, notification, pause.state, user_id=user.user_id)
    raise _expired() if pause.state == "expired" else _already_answered()


@router.get("/{notification_id}/prompts", response_model=InboxPromptsResponse, response_model_by_alias=True)
async def get_inbox_prompts(
    notification_id: str = _NOTIFICATION_ID,
    user: User = Depends(get_current_user_from_session),
) -> InboxPromptsResponse:
    """The prompts a paused scheduled run waits on, with the full tool input behind Inspect (§3.4).

    Read from the paused session's breadcrumbs on every call, never from the row, and only while
    the pause can still be answered.
    """
    notification = await _waiting_row(notification_id, user)
    if notification.resolution is not None and notification.resolution.state == "applying":
        raise _already_answered()
    pause = await _live_pause(notification, user)
    return InboxPromptsResponse(
        notification_id=notification.notification_id,
        session_id=inbox_answer.row_session_id(notification) or "",
        prompts=pause.prompts,
    )


async def _give_back(user: User, notification_id: str, resume_run_id: str) -> None:
    """Release a claim this request made, so the buttons come back at once."""
    await asyncio.to_thread(
        _svc().set_resolution,
        user.email,
        notification_id,
        NotificationResolution(state="retry", at=_now_iso(), resume_run_id=resume_run_id),
        claimed_by=resume_run_id,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@router.post(
    "/{notification_id}/respond",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RespondResponse,
    response_model_by_alias=True,
)
async def respond_to_inbox_item(
    body: RespondRequest,
    request: Request,
    notification_id: str = _NOTIFICATION_ID,
    user: User = Depends(get_current_user_from_session),
) -> RespondResponse:
    """Answer a paused scheduled run; the scheduled-runs worker resumes it (§3.5).

    Before the ``202``: the row and its live pause, the answers, the claim (the race guard: a
    second answer gets 409), the headless grant renewed from the caller's session, the start
    audit row, and the async invoke. Any failure after the claim gives the row back to ``retry``.
    Poll ``GET /runs/{resumeRunId}`` for the outcome.
    """
    notification = await _waiting_row(notification_id, user)
    pause = await _live_pause(notification, user)
    try:
        answers, decision, decisions = inbox_answer.check_responses(
            notification, pause.prompts, [a.model_dump(by_alias=True) for a in body.responses]
        )
    except inbox_answer.InvalidResponses as exc:
        raise _refused(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_answers", str(exc))

    resume_run_id = f"run-{uuid.uuid4().hex[:12]}"
    claim = await asyncio.to_thread(
        _svc().claim_resolution,
        user.email,
        notification_id,
        resume_run_id=resume_run_id,
        decision=decision,
        stale_before=inbox_answer.stale_claim_before(),
    )
    if claim is None:
        raise _already_answered()

    session_id = inbox_answer.row_session_id(notification) or ""
    payload: Dict[str, Any] = notification.payload
    try:
        # The resume mints from the headless grant; answering renews it, as Run now does.
        await runs_routes._resolve_grant(request, user)
    except HTTPException:
        await _give_back(user, notification_id, resume_run_id)
        raise

    governance = GovernanceFloor(audit=runs_routes.get_run_audit_recorder())
    try:
        await governance.on_run_start(
            run_id=resume_run_id,
            user_id=user.user_id,
            session_id=session_id,
            trigger="inbox_respond",
            prompt="",
            title=payload.get("label") or None,
            extra={
                "resumesRunId": payload.get("runId") or "",
                "notificationId": notification_id,
                "decisions": decisions,
            },
        )
    except Exception:
        logger.exception("Inbox answer %s: could not write its audit row", resume_run_id)
        await _give_back(user, notification_id, resume_run_id)
        raise _refused(status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable", "Couldn't send the answer. Try again.")

    try:
        await asyncio.to_thread(
            inbox_answer.invoke_inbox_resume,
            {
                "userId": user.user_id,
                "email": user.email,
                "notificationId": notification_id,
                "resumeRunId": resume_run_id,
                "sessionId": session_id,
                "scheduleId": payload.get("scheduleId") or "",
                "responses": answers,
            },
        )
    except Exception:
        logger.exception("Inbox answer %s: the worker could not be invoked", resume_run_id)
        await _give_back(user, notification_id, resume_run_id)
        await governance.on_run_end(
            result=RunResult(
                run_id=resume_run_id,
                session_id=session_id,
                user_id=user.user_id,
                status="error",
                error="The answer could not be sent.",
                finished_at=_now_iso(),
            )
        )
        raise _refused(status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable", "Couldn't send the answer. Try again.")

    logger.info("Inbox answer %s started for notification %s", resume_run_id, notification_id)
    return RespondResponse(resume_run_id=resume_run_id, resolution=claim)
