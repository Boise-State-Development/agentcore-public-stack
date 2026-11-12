"""Handoff endpoints (``docs/specs/agent-handoffs-and-consults.md`` §4.2, PR-0.1).

    POST /sessions/{session_id}/handoffs                         render the excerpt, open a handoff
    GET  /sessions/{session_id}/handoffs/{handoff_id}            read one
    POST /sessions/{session_id}/handoffs/{handoff_id}/dismiss    cancel an open one
    POST /sessions/{session_id}/handoffs/{handoff_id}/notifications/read    mark its inbox entries read
    POST /sessions/{session_id}/handoffs/{handoff_id}/notifications/events  count a browser-side fact
    POST /sessions/{session_id}/handoffs/send-back                send a target's answer back (§4.3)
    POST /sessions/{session_id}/handoffs/{handoff_id}/deliver     Send to *{title}* after review
    POST /sessions/{session_id}/deliveries/{delivery_id}/dismiss  remove a pending card (§4.8)

All 404 to a signed-in user while ``HANDOFFS_ENABLED`` is off (401 unauthenticated regardless).

Creating a handoff sends nothing and creates no session. The review sheet needs the excerpt and
the exact prompt the Agent would receive, and the target session is only made by the first send
(PR-0.2), so a cancelled handoff leaves no blank conversation behind.

Rows are owner-scoped by the table's partition key and must belong to the session in the path,
so another user's handoff id, or this user's id under the wrong conversation, is a 404.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from apis.shared.assistants.service import get_assistant_with_access_check
from apis.shared.auth import User, get_current_user_from_session
from apis.shared.feature_flags import handoffs_enabled
from apis.shared.handoff import (
    Handoff,
    HandoffStore,
    HandoffStoreUnavailable,
    TurnsCovered,
    build_handoff_message,
    context_hash,
    get_handoff_store,
    render_excerpt,
    wrap_handoff_context,
)
from apis.shared.handoff.delivery import (
    DELIVERY_MAX_ARTIFACTS,
    DeliveryArtifact,
    DeliveryStore,
    get_delivery_store,
)
from apis.shared.handoff.excerpt import CHARS_PER_TOKEN
from apis.shared.handoff.metrics import emit_handoff_metric
from apis.shared.handoff.models import HANDOFF_REQUEST_MAX_CHARS, HandoffView
from apis.shared.handoff.send_back import (
    DeliveryRefused,
    LinkedPair,
    load_linked_pair,
    offered_artifacts,
    prepare_delivery,
    render_return_excerpt,
    write_delivery,
)
from apis.shared.handoff.notify import HANDOFF_NOTIFICATION_KINDS
from apis.shared.handoff.relay import load_relayed_prompts
from apis.shared.notifications import NotificationService
from apis.shared.sessions.messages import get_messages
from apis.shared.sessions.metadata import get_session_compaction, get_session_metadata
from apis.shared.sessions.rewind import parse_message_index
from apis.shared.timestamps import utc_now_iso

logger = logging.getLogger(__name__)


async def require_handoffs_user(
    user: User = Depends(get_current_user_from_session),
) -> User:
    """The signed-in user, or 404 while the flag is off. Authentication runs first."""
    if not handoffs_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return user


router = APIRouter(prefix="/sessions", tags=["handoffs"])


class CreateHandoffRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    kind: Literal["mention"] = "mention"
    agent_id: str = Field(..., alias="agentId", min_length=1, max_length=256)
    request: str = Field("", max_length=HANDOFF_REQUEST_MAX_CHARS, description="What the user typed, minus the mention")


class CreateHandoffResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    handoff_id: str = Field(..., alias="handoffId")
    excerpt: str = Field(..., description="The context card: a <conversation_excerpt> block, or empty")
    excerpt_hash: str = Field(..., alias="excerptHash")
    excerpt_tokens: int = Field(..., alias="excerptTokens", description="Estimated at 4 characters a token")
    turns_covered: Optional[TurnsCovered] = Field(None, alias="turnsCovered")
    context_block: str = Field(..., alias="contextBlock", description="The excerpt wrapped as the Agent receives it")
    prompt: str = Field(..., description="The full first message the Agent receives: contextBlock, then request")
    request: str
    source_message_index: int = Field(
        ...,
        alias="sourceMessageIndex",
        description="Index of the message the handoff row will sit after; -1 for an empty conversation",
    )
    source_title: str = Field(
        ...,
        alias="sourceTitle",
        description="The title the wrapper names. The review sheet re-wraps an edited card with it, as the server will",
    )


@router.post(
    "/{session_id}/handoffs",
    response_model=CreateHandoffResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
async def create_handoff(
    session_id: str,
    body: CreateHandoffRequest,
    user: User = Depends(require_handoffs_user),
    store: HandoffStore = Depends(get_handoff_store),
) -> CreateHandoffResponse:
    """Open a handoff from this conversation to an Agent, for the user to review.

    Renders the excerpt from the persisted conversation (compaction-aware, bounded), writes an
    ``open`` ``HANDOFF#`` row, and returns the card plus the full prompt the Agent would receive.
    404 when the conversation isn't the caller's, or the Agent doesn't exist or isn't theirs to
    use (one answer for both, so the response never says which ids exist).
    """
    source = await get_session_metadata(session_id, user.user_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    agent, _permission = await get_assistant_with_access_check(
        assistant_id=body.agent_id, user_id=user.user_id, user_email=user.email
    )
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    try:
        listing = await get_messages(session_id=session_id, user_id=user.user_id)
    except FileNotFoundError:
        listing = None
    visible = listing.messages if listing else []
    messages = [m.model_dump(by_alias=True, exclude_none=True) for m in visible]
    # The row anchors to the last message's id, never its position: a rewind
    # hides messages without renumbering, so the two differ once one has run.
    anchor = parse_message_index(session_id, visible[-1].id) if visible else None
    checkpoint, summary = await get_session_compaction(session_id, user.user_id)

    excerpt = render_excerpt(messages, compaction_checkpoint=checkpoint, compaction_summary=summary)
    source_title = source.title or ""
    context_block = wrap_handoff_context(excerpt.text, source_title)
    request = body.request.strip()
    turns = (
        TurnsCovered(
            first=excerpt.first_turn, last=excerpt.last_turn, of=excerpt.total_turns, omitted=excerpt.omitted_turns
        )
        if not excerpt.is_empty
        else None
    )

    now = utc_now_iso()
    row = Handoff(
        handoff_id=str(uuid.uuid4()),
        user_id=user.user_id,
        session_id=session_id,
        kind=body.kind,
        status="open",
        agent_id=body.agent_id,
        agent_name=agent.name or "",
        request=request,
        source_title=source_title,
        source_message_index=anchor if anchor is not None else len(messages) - 1,
        excerpt_hash=context_hash(excerpt.text),
        excerpt_tokens=excerpt.estimated_tokens,
        turns_covered=turns,
        created_at=now,
        updated_at=now,
    )
    try:
        await asyncio.to_thread(store.create, row)
    except HandoffStoreUnavailable:
        logger.error("Handoff store is not configured")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Handoffs are unavailable")

    emit_handoff_metric("HandoffCreated", body.kind, session_id=session_id)
    return CreateHandoffResponse(
        handoff_id=row.handoff_id,
        excerpt=excerpt.text,
        excerpt_hash=row.excerpt_hash or "",
        excerpt_tokens=excerpt.estimated_tokens,
        turns_covered=turns,
        context_block=context_block,
        prompt=build_handoff_message(context_block, request),
        request=request,
        source_message_index=row.source_message_index,
        source_title=source_title,
    )


@router.get(
    "/{session_id}/handoffs/{handoff_id}",
    response_model=HandoffView,
    response_model_by_alias=True,
    response_model_exclude_none=True,
)
async def get_handoff(
    session_id: str,
    handoff_id: str,
    user: User = Depends(require_handoffs_user),
    store: HandoffStore = Depends(get_handoff_store),
) -> HandoffView:
    """One of the caller's handoffs from this conversation.

    A ``needs_input`` handoff carries ``relayedPrompts``, read from its target as ``GET /messages``
    reads them. The SPA reads this row when a target's turn ends, so the relay it shows then comes
    from the same breadcrumbs a reload would, not from prompt state the tab may have cleared.
    """
    row = await _owned_row(store, user, session_id, handoff_id)
    view = HandoffView.from_row(row)
    await load_relayed_prompts([view], user.user_id)
    return view


@router.post("/{session_id}/handoffs/{handoff_id}/dismiss", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_handoff(
    session_id: str,
    handoff_id: str,
    user: User = Depends(require_handoffs_user),
    store: HandoffStore = Depends(get_handoff_store),
) -> Response:
    """Cancel an open handoff (the review sheet's Cancel).

    Idempotent: dismissing a dismissed handoff is 204. A handoff that was already sent is a 409,
    because cancelling the review can no longer stop the Agent's conversation.
    """
    row = await _owned_row(store, user, session_id, handoff_id)
    if row.status == "dismissed":
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    updated = await asyncio.to_thread(store.transition, user.user_id, handoff_id, "dismissed")
    if updated is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This handoff was already sent")
    emit_handoff_metric("HandoffCancelled", row.kind, session_id=session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


HandoffNotificationKind = Literal["handoff_needs_input", "handoff_done", "handoff_stopped"]


class MarkHandoffNotificationsReadRequest(BaseModel):
    kinds: List[HandoffNotificationKind] = Field(
        default_factory=lambda: list(HANDOFF_NOTIFICATION_KINDS.values()), min_length=1, max_length=3
    )


class MarkHandoffNotificationsReadResponse(BaseModel):
    marked: int


_inbox: Optional[NotificationService] = None


def get_inbox() -> NotificationService:
    global _inbox
    if _inbox is None:
        _inbox = NotificationService()
    return _inbox


@router.post(
    "/{session_id}/handoffs/{handoff_id}/notifications/read",
    response_model=MarkHandoffNotificationsReadResponse,
)
async def mark_handoff_notifications_read(
    session_id: str,
    handoff_id: str,
    body: MarkHandoffNotificationsReadRequest,
    user: User = Depends(require_handoffs_user),
    store: HandoffStore = Depends(get_handoff_store),
    inbox: NotificationService = Depends(get_inbox),
) -> MarkHandoffNotificationsReadResponse:
    """Mark this handoff's unread inbox entries of ``kinds`` read (§4.3, PR-0.3b).

    Read state follows the work, not the bell: the SPA calls this when the user opens the target,
    and when a state change lands while they are looking at the source or the target. Only the
    caller's own inbox is touched, and only for a handoff that is theirs from this conversation.
    """
    await _owned_row(store, user, session_id, handoff_id)
    marked = await asyncio.to_thread(
        inbox.mark_read_matching, user.email, kinds=body.kinds, payload={"handoffId": handoff_id}
    )
    return MarkHandoffNotificationsReadResponse(marked=marked)


RelayedPromptKind = Literal["oauth", "tool_approval", "user_question", "browser_login"]


class HandoffNotificationEvent(BaseModel):
    """A browser-side fact the server can't see (§4.7).

    ``outcome`` only for a permission ask. ``interruptKind`` for the three relayed-prompt counts
    (PR-0.3c), and ``surface`` for ``RelayedPromptAnswered``: ``source`` when answered under the
    handoff row, ``target`` when answered in the target conversation itself.
    """

    model_config = ConfigDict(populate_by_name=True)

    metric: Literal[
        "HandoffNotified",
        "HandoffNotificationOpened",
        "HandoffBrowserPermission",
        "RelayedPromptShown",
        "RelayedPromptAnswered",
        "RelayedPromptConflict",
    ]
    channel: Optional[Literal["inbox", "browser"]] = None
    outcome: Optional[Literal["granted", "denied", "dismissed"]] = None
    interrupt_kind: Optional[RelayedPromptKind] = Field(None, alias="interruptKind")
    surface: Optional[Literal["source", "target"]] = None


@router.post(
    "/{session_id}/handoffs/{handoff_id}/notifications/events",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def record_handoff_notification_event(
    session_id: str,
    handoff_id: str,
    body: HandoffNotificationEvent,
    user: User = Depends(require_handoffs_user),
    store: HandoffStore = Depends(get_handoff_store),
) -> Response:
    """Count a browser notification shown or opened, an inbox entry opened, a permission answer,
    or a relayed prompt shown, answered or refused because the other surface answered first.

    A count and nothing else. No row is written, and only one count reads one: an answer given in
    the target is counted only while its handoff is ``needs_input``, because once a handoff is
    ``done`` its target is the user's own conversation and a prompt there is not a relayed one. The
    target calls before it resumes, so the row has not moved yet.
    """
    if body.metric.startswith("RelayedPrompt"):
        if body.interrupt_kind is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="interruptKind is required")
        surface = body.surface if body.metric == "RelayedPromptAnswered" else None
        if body.metric == "RelayedPromptAnswered" and surface is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="surface is required")
        if surface == "target":
            row = await _owned_row(store, user, session_id, handoff_id)
            if row.status != "needs_input":
                return Response(status_code=status.HTTP_204_NO_CONTENT)
        emit_handoff_metric(body.metric, "mention", session_id=session_id, detail=body.interrupt_kind, surface=surface)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    detail = body.outcome if body.metric == "HandoffBrowserPermission" else body.channel
    emit_handoff_metric(body.metric, "mention", session_id=session_id, detail=detail)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Send back (§4.3) and deliveries (§4.8), PR-0.4 ---------------------------------------------


class CreateSendBackRequest(BaseModel):
    """Bring back result (``deliver``) or the start of Send to *{title}* (review first)."""

    model_config = ConfigDict(populate_by_name=True)

    message_index: Optional[int] = Field(
        None, alias="messageIndex", ge=0, description="The assistant message to send back; the last one when absent"
    )
    deliver: bool = Field(False, description="Make the delivery now (Bring back result) instead of opening a review")


class CreateSendBackResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    handoff_id: str = Field(..., alias="handoffId")
    source_session_id: str = Field(..., alias="sourceSessionId", description="Where the result goes")
    source_title: str = Field(..., alias="sourceTitle")
    from_title: str = Field(..., alias="fromTitle", description="This conversation's title, which the block names")
    message_index: int = Field(..., alias="messageIndex")
    excerpt: str = Field(..., description="The return excerpt: the message's text, capped")
    excerpt_hash: str = Field(..., alias="excerptHash")
    artifacts: List[DeliveryArtifact] = Field(
        default_factory=list, description="Artifacts the message produced, offered with it"
    )
    delivery_id: Optional[str] = Field(None, alias="deliveryId", description="Set when the delivery was made")


class DeliverSendBackRequest(BaseModel):
    """Send to *{title}*: the reviewed card and the artifacts left on it."""

    model_config = ConfigDict(populate_by_name=True)

    text: str = Field("", max_length=200_000)
    artifact_ids: List[str] = Field(default_factory=list, alias="artifactIds", max_length=DELIVERY_MAX_ARTIFACTS)


class DeliverSendBackResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    delivery_id: str = Field(..., alias="deliveryId")
    source_session_id: str = Field(..., alias="sourceSessionId")
    source_title: str = Field(..., alias="sourceTitle")


#: How each refusal reads over HTTP. A conversation that isn't the caller's, or isn't linked, is a
#: 404 like every other id here, so the response never says which ids exist.
_REFUSAL_STATUS = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "not_linked": status.HTTP_404_NOT_FOUND,
    "same_conversation": status.HTTP_404_NOT_FOUND,
    "artifact_not_in_sender": status.HTTP_404_NOT_FOUND,
    "project_task": status.HTTP_409_CONFLICT,
    "empty": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "too_many_artifacts": status.HTTP_422_UNPROCESSABLE_ENTITY,
}


def _refused(exc: DeliveryRefused) -> HTTPException:
    detail = {
        "project_task": "Results can't be sent into or out of a shared project",
        "empty": "There is nothing to send",
        "too_many_artifacts": f"A result carries at most {DELIVERY_MAX_ARTIFACTS} artifacts",
    }.get(exc.reason, "Conversation not found")
    return HTTPException(status_code=_REFUSAL_STATUS.get(exc.reason, status.HTTP_404_NOT_FOUND), detail=detail)


@router.post(
    "/{session_id}/handoffs/send-back",
    response_model=CreateSendBackResponse,
    response_model_by_alias=True,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
)
async def create_send_back(
    session_id: str,
    body: CreateSendBackRequest,
    user: User = Depends(require_handoffs_user),
    store: HandoffStore = Depends(get_handoff_store),
) -> CreateSendBackResponse:
    """Offer one of this conversation's answers back to the conversation it was handed off from.

    ``session_id`` is the handoff's **target**; the result goes to its ``spawnedFrom`` source.
    Renders the return excerpt and lists the artifacts the message made. With ``deliver`` (Bring
    back result, on the source's done row) the delivery is made at once; without it (Send to
    *{title}*, in the target) the row stays ``open`` for the review sheet, and
    ``POST …/handoffs/{handoffId}/deliver`` sends what the user kept.
    """
    from apis.shared.sessions.metadata import load_session_meta

    meta = await load_session_meta(session_id, user.user_id)
    spawned = (meta.row or {}).get("spawnedFrom") if meta.row else None
    source_id = spawned.get("sessionId") if isinstance(spawned, dict) else None
    if not isinstance(source_id, str) or not source_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    try:
        pair = await load_linked_pair(user.user_id, session_id, source_id)
    except DeliveryRefused as exc:
        raise _refused(exc)

    message, index = await _send_back_message(session_id, user.user_id, body.message_index)
    excerpt = render_return_excerpt(message)
    artifacts = await asyncio.to_thread(offered_artifacts, user.user_id, session_id, index)
    if not excerpt and not artifacts:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="That answer has nothing to send")

    now = utc_now_iso()
    from_title = str(pair.sender.get("title") or "")
    row = Handoff(
        handoff_id=str(uuid.uuid4()),
        user_id=user.user_id,
        session_id=session_id,
        kind="send_back",
        status="open",
        agent_id=str(pair.sender.get("assistantId") or ""),
        source_title=from_title,
        source_message_index=index,
        excerpt_hash=context_hash(excerpt) if excerpt else None,
        excerpt_tokens=len(excerpt) // CHARS_PER_TOKEN,
        target_session_id=source_id,
        created_at=now,
        updated_at=now,
    )
    try:
        await asyncio.to_thread(store.create, row)
    except HandoffStoreUnavailable:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Handoffs are unavailable")
    emit_handoff_metric("HandoffCreated", "send_back", session_id=session_id, detail="row" if body.deliver else "target")

    delivery_id: Optional[str] = None
    if body.deliver:
        delivery_id = await _deliver(
            store, user, row, pair, excerpt, [a.artifact_id for a in artifacts], origin_surface="row"
        )
    return CreateSendBackResponse(
        handoff_id=row.handoff_id,
        source_session_id=source_id,
        source_title=str(pair.receiver.get("title") or ""),
        from_title=from_title,
        message_index=index,
        excerpt=excerpt,
        excerpt_hash=row.excerpt_hash or "",
        artifacts=artifacts,
        delivery_id=delivery_id,
    )


@router.post(
    "/{session_id}/handoffs/{handoff_id}/deliver",
    response_model=DeliverSendBackResponse,
    response_model_by_alias=True,
)
async def deliver_send_back(
    session_id: str,
    handoff_id: str,
    body: DeliverSendBackRequest,
    user: User = Depends(require_handoffs_user),
    store: HandoffStore = Depends(get_handoff_store),
) -> DeliverSendBackResponse:
    """Send to *{title}*: make the delivery from the reviewed card. Nothing navigates (§4.3).

    409 once the send-back was already delivered or cancelled, so a double click sends once.
    """
    row = await _owned_row(store, user, session_id, handoff_id)
    if row.kind != "send_back" or not row.target_session_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Handoff not found")
    if row.status != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This result was already sent")
    try:
        pair = await load_linked_pair(user.user_id, session_id, row.target_session_id)
    except DeliveryRefused as exc:
        raise _refused(exc)
    delivery_id = await _deliver(store, user, row, pair, body.text, body.artifact_ids, origin_surface="target")
    return DeliverSendBackResponse(
        delivery_id=delivery_id,
        source_session_id=row.target_session_id,
        source_title=str(pair.receiver.get("title") or ""),
    )


@router.post("/{session_id}/deliveries/{delivery_id}/dismiss", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_delivery(
    session_id: str,
    delivery_id: str,
    user: User = Depends(require_handoffs_user),
    deliveries: DeliveryStore = Depends(get_delivery_store),
) -> Response:
    """Remove a pending card from this conversation's composer (§4.8).

    Idempotent for a dismissed card. 409 for one a send already attached; 404 for anything that
    isn't the caller's card on this conversation.
    """
    try:
        dismissed = await asyncio.to_thread(deliveries.dismiss, user.user_id, delivery_id, session_id)
        if dismissed is not None:
            emit_handoff_metric("DeliveryDismissed", "send_back", session_id=session_id, detail=dismissed.origin)
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        current = await asyncio.to_thread(deliveries.get, user.user_id, delivery_id)
    except HandoffStoreUnavailable:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Handoffs are unavailable")
    if current is None or current.to_session_id != session_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery not found")
    if current.status == "dismissed":
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This card was already sent")


async def _deliver(
    store: HandoffStore,
    user: User,
    row: Handoff,
    pair: LinkedPair,
    text: str,
    artifact_ids: List[str],
    *,
    origin_surface: str,
) -> str:
    """Make a send-back's delivery and close its row, once. 409 if another request got there first.

    Checks first, then the row's ``open`` → ``done`` (the exactly-once step), then the write. A
    refusal leaves the row open, so the user can fix the card and send again.
    """
    try:
        delivery = await prepare_delivery(
            user_id=user.user_id,
            from_session_id=row.session_id,
            to_session_id=str(row.target_session_id),
            text=text,
            handoff_id=row.handoff_id,
            artifact_ids=artifact_ids,
            pair=pair,
        )
    except DeliveryRefused as exc:
        raise _refused(exc)
    completed = await asyncio.to_thread(store.complete_send_back, user.user_id, row.handoff_id)
    if completed is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This result was already sent")
    await write_delivery(delivery)
    emit_handoff_metric("SendBack", "send_back", session_id=row.session_id, detail=origin_surface)
    return delivery.delivery_id


async def _send_back_message(session_id: str, user_id: str, message_index: Optional[int]) -> Tuple[Dict[str, Any], int]:
    """The assistant message a send-back carries, and its index. 404 if there is no such answer."""
    try:
        listing = await get_messages(session_id=session_id, user_id=user_id)
    except FileNotFoundError:
        listing = None
    for message in reversed(listing.messages if listing else []):
        if message.role != "assistant":
            continue
        index = parse_message_index(session_id, message.id)
        if index is None or (message_index is not None and index != message_index):
            continue
        return message.model_dump(by_alias=True, exclude_none=True), index
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")


async def _owned_row(store: HandoffStore, user: User, session_id: str, handoff_id: str) -> Handoff:
    try:
        row = await asyncio.to_thread(store.get, user.user_id, handoff_id)
    except HandoffStoreUnavailable:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Handoffs are unavailable")
    if row is None or row.session_id != session_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Handoff not found")
    return row
