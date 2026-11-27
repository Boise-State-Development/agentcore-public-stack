"""Answering a scheduled run's pause from the inbox (docs/specs/answer-from-inbox.md §3.4–3.5).

A needs-user inbox row holds ids and display lines only. The prompts themselves are the paused
session's ``PendingInterrupt`` breadcrumbs, read here under ``(session, caller)``, by the handoff
relay's rule (``relayed_prompts_from_row``): the breadcrumbs and an unexpired ``pausedTurn``.
Another user's session has no ``META`` row for the caller, so a forged id reads nothing.

This module is everything app-api needs that is not the route itself:

- :func:`read_inbox_pause`: is the row's pause still live, gone, or expired, and its prompts;
- :func:`check_responses`: the answers, refused before anything is claimed;
- :func:`settle_unanswered_rows`: rows whose pause was answered in the conversation, or lapsed,
  settled when the inbox is listed, so read state follows the answer (§3.3);
- :func:`invoke_inbox_resume`: the async invoke of the scheduled-runs worker.

Nothing here runs on the turn path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Literal, Optional, Tuple

from apis.shared.handoff.models import RelayedPrompt
from apis.shared.handoff.relay import relayed_prompts_from_row
from apis.shared.notifications.models import Notification, NotificationResolution
from apis.shared.scheduled_prompts.models import is_headless_session
from apis.shared.timestamps import to_iso, utc_now_iso
from apis.shared.scheduled_prompts.needs_user import (
    INBOX_PROMPT_KINDS,
    INBOX_RESUME_KIND,
    NEEDS_USER_KINDS,
    normalize_expiry,
    now_expiry_format,
)

logger = logging.getLogger(__name__)

#: A claim no worker has started, or one whose worker started, this long ago is stale and may be
#: retaken. The worker's timeout is 15 minutes.
STALE_CLAIM = timedelta(minutes=16)

PauseState = Literal["live", "gone", "expired"]
Decision = Literal["approved", "declined", "answered", "skipped"]

MetaLoader = Callable[[str, str], Awaitable[Any]]


@dataclass
class InboxPause:
    """What a needs-user row's pause looks like now."""

    state: PauseState
    prompts: List[RelayedPrompt] = field(default_factory=list)


def is_needs_user(notification: Optional[Notification]) -> bool:
    return notification is not None and notification.kind in NEEDS_USER_KINDS


def row_interrupt_ids(notification: Notification) -> List[str]:
    """The interrupt ids the row lists, in order."""
    prompts = notification.payload.get("prompts") if isinstance(notification.payload, dict) else None
    ids: List[str] = []
    for prompt in prompts or []:
        interrupt_id = prompt.get("interruptId") if isinstance(prompt, dict) else None
        if isinstance(interrupt_id, str) and interrupt_id and interrupt_id not in ids:
            ids.append(interrupt_id)
    return ids


def row_session_id(notification: Notification) -> Optional[str]:
    session_id = notification.payload.get("sessionId") if isinstance(notification.payload, dict) else None
    return session_id if isinstance(session_id, str) and is_headless_session(session_id) else None


def _payload_expired(notification: Notification, now: str) -> bool:
    expires_at = notification.payload.get("expiresAt") if isinstance(notification.payload, dict) else None
    return isinstance(expires_at, str) and expires_at <= now


def read_inbox_pause(
    notification: Notification, meta_row: Optional[Dict[str, Any]], *, now: Optional[datetime] = None
) -> InboxPause:
    """The row's pause from its session's ``META`` row: live with its prompts, gone, or expired.

    Expired when the row's own ``expiresAt`` or the session's ``pausedTurn`` has passed. Gone when
    any prompt the row lists is no longer waiting: every prompt of a pause is answered together,
    so one gone means the pause was answered, or superseded by a later turn.
    """
    current = now or datetime.now(timezone.utc)
    if _payload_expired(notification, now_expiry_format(current)):
        return InboxPause("expired")
    paused = (meta_row or {}).get("pausedTurn")
    snapshot_expiry = normalize_expiry(paused.get("expiresAt")) if isinstance(paused, dict) else None
    if snapshot_expiry is not None and snapshot_expiry <= now_expiry_format(current):
        return InboxPause("expired")
    wanted = row_interrupt_ids(notification)
    live = {p.interrupt_id: p for p in relayed_prompts_from_row(meta_row, kinds=INBOX_PROMPT_KINDS, now=current)}
    if not wanted or any(i not in live for i in wanted):
        return InboxPause("gone")
    return InboxPause("live", [live[i] for i in wanted])


class InvalidResponses(ValueError):
    """The answers don't fit the row's prompts. The message is safe to show."""


_APPROVAL_ANSWERS = ("approved", "declined")
_ELICITATION_ACTIONS = ("accept", "decline", "cancel")


def _decision_for(kind: str, response: Any) -> Decision:
    """One answer's decision, refusing a shape the conversation's own card never sends."""
    if response is None:
        raise InvalidResponses("An answer is empty.")
    if kind == "tool_approval":
        if response not in _APPROVAL_ANSWERS:
            raise InvalidResponses('A tool approval is answered "approved" or "declined".')
        return "approved" if response == "approved" else "declined"
    if kind == "user_question":
        if not isinstance(response, dict):
            raise InvalidResponses("A question is answered with {answers} or {skipped: true}.")
        if response.get("skipped") is True and "answers" not in response:
            return "skipped"
        if not isinstance(response.get("answers"), dict):
            raise InvalidResponses("A question is answered with {answers} or {skipped: true}.")
        return "answered"
    if kind == "mcp_elicitation":
        action = response.get("action") if isinstance(response, dict) else None
        if action not in _ELICITATION_ACTIONS:
            raise InvalidResponses("A form is answered with an action: accept, decline or cancel.")
        if action == "accept":
            if not isinstance(response.get("content", {}), dict):
                raise InvalidResponses("A form's content is an object.")
            return "answered"
        return "skipped"
    raise InvalidResponses("This prompt can only be answered in its conversation.")


def check_responses(
    notification: Notification, prompts: List[RelayedPrompt], responses: Iterable[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], Decision, List[str]]:
    """Check the answers against the row and its live prompts (§3.5 step 3).

    Every response names one of the row's interrupts, every prompt is covered once, none is null,
    and each has its kind's shape. The Runtime still checks a form's content against the server's
    schema on resume. Returns the answers as the resume sends them, the row's one decision, and
    each answer's decision (for the audit row: kinds only, never the answer).

    Raises:
        InvalidResponses: The row can't be answered here, or the answers don't fit.
    """
    if not notification.payload.get("answerable"):
        raise InvalidResponses("This one can only be answered in its conversation.")
    by_id = {p.interrupt_id: p for p in prompts}
    answers: List[Dict[str, Any]] = []
    decisions: List[str] = []
    seen: set[str] = set()
    for entry in responses:
        interrupt_id = entry.get("interruptId") if isinstance(entry, dict) else None
        if not isinstance(interrupt_id, str) or interrupt_id not in by_id:
            raise InvalidResponses("An answer names a prompt this item isn't waiting on.")
        if interrupt_id in seen:
            raise InvalidResponses("A prompt is answered twice.")
        seen.add(interrupt_id)
        response = entry.get("response")
        decisions.append(_decision_for(by_id[interrupt_id].kind, response))
        answers.append({"interruptId": interrupt_id, "response": response})
    if seen != set(by_id):
        raise InvalidResponses("Every prompt needs an answer.")
    return answers, summarize_decisions(decisions), decisions


def summarize_decisions(decisions: List[str]) -> Decision:
    """The row's one decision: approved or declined for approvals only, else answered or skipped."""
    if decisions and set(decisions) <= {"approved", "declined"}:
        return "approved" if all(d == "approved" for d in decisions) else "declined"
    if decisions and all(d in ("skipped", "declined") for d in decisions):
        return "skipped"
    return "answered"


def stale_claim_before(now: Optional[datetime] = None) -> str:
    """The claim time before which an ``applying`` claim may be retaken, as the row stores ``at``."""
    return to_iso((now or datetime.now(timezone.utc)) - STALE_CLAIM)


async def settle_unanswered_rows(
    service: Any,
    email: str,
    user_id: str,
    notifications: List[Notification],
    meta_loader: Optional[MetaLoader] = None,
    *,
    now: Optional[datetime] = None,
) -> int:
    """Settle this page's waiting needs-user rows whose pause went without the inbox (§3.3, §3.6).

    A pause answered in its conversation resolves ``answered_elsewhere``; one that lapsed,
    ``expired``. Both mark the row read, so the badge stops counting it. One ``META`` read per
    waiting row, all at once, and only for rows nothing has settled; the conditional write keeps
    an inbox answer that landed meanwhile. Updates the rows in place and returns how many it
    settled. Never raises.
    """
    from apis.shared.sessions.metadata import load_session_meta

    loader = meta_loader or load_session_meta
    current = now or datetime.now(timezone.utc)
    waiting = [n for n in notifications if is_needs_user(n) and n.resolution is None]

    async def one(n: Notification) -> int:
        session_id = row_session_id(n)
        try:
            if _payload_expired(n, now_expiry_format(current)) or session_id is None:
                state: PauseState = "expired" if session_id else "gone"
            else:
                meta = await loader(session_id, user_id)
                state = read_inbox_pause(n, getattr(meta, "row", None), now=current).state
        except Exception:  # noqa: BLE001 - an unreadable pause leaves the row as it was
            logger.warning("Could not read the pause behind notification %s", n.notification_id, exc_info=True)
            return 0
        if state == "live":
            return 0
        return 1 if await settle(service, email, n, state, user_id=user_id) else 0

    return sum(await asyncio.gather(*(one(n) for n in waiting)))


async def settle(
    service: Any, email: str, notification: Notification, state: PauseState, *, user_id: str
) -> bool:
    """Resolve a row whose pause is ``gone`` (``answered_elsewhere``) or ``expired``, if nothing
    else settled it first. Updates the row in place.

    A pause answered in its conversation also marks the run's history row (§3.8), which nothing
    else would: that resume never reaches the worker. An expired one needs no write, since the
    history row reads as expired from its own ``expiresAt``.
    """
    resolution = NotificationResolution(
        state="answered_elsewhere" if state == "gone" else "expired",
        at=utc_now_iso(),
    )
    written = await asyncio.to_thread(
        service.set_resolution, email, notification.notification_id, resolution, only_if_unresolved=True
    )
    if written:
        notification.resolution = resolution
        notification.read_at = notification.read_at or resolution.at
        if resolution.state == "answered_elsewhere":
            await _mark_run_answered_elsewhere(notification, user_id)
    return written


async def _mark_run_answered_elsewhere(notification: Notification, user_id: str) -> None:
    from apis.shared.scheduled_prompts.runs import record_schedule_run_answered_elsewhere

    payload = notification.payload if isinstance(notification.payload, dict) else {}
    names = [payload.get("scheduleId"), payload.get("runFinishedAt"), payload.get("runId")]
    if not all(isinstance(v, str) and v for v in names):
        return
    schedule_id, run_finished_at, run_id = names
    await asyncio.to_thread(
        record_schedule_run_answered_elsewhere,
        user_id,
        schedule_id,
        run_finished_at=run_finished_at,
        run_id=run_id,
    )


def worker_function_name() -> str:
    return os.environ.get("SCHEDULED_RUNS_WORKER_FUNCTION_NAME", "")


def invoke_inbox_resume(payload: Dict[str, Any]) -> None:
    """Async-invoke the scheduled-runs worker with a resume (``InvocationType=Event``).

    Blocking: call it in a thread. Raises when the function isn't configured or the invoke is
    refused, so the caller can give the buttons back.
    """
    function_name = worker_function_name()
    if not function_name:
        raise RuntimeError("SCHEDULED_RUNS_WORKER_FUNCTION_NAME is not set")
    import boto3

    boto3.client("lambda").invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps({"kind": INBOX_RESUME_KIND, **payload}).encode(),
    )
