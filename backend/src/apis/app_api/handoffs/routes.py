"""Handoff endpoints (``docs/specs/agent-handoffs-and-consults.md`` §4.2, PR-0.1).

    POST /sessions/{session_id}/handoffs                         render the excerpt, open a handoff
    GET  /sessions/{session_id}/handoffs/{handoff_id}            read one
    POST /sessions/{session_id}/handoffs/{handoff_id}/dismiss    cancel an open one

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
from typing import Literal, Optional

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
from apis.shared.handoff.metrics import emit_handoff_metric
from apis.shared.handoff.models import HANDOFF_REQUEST_MAX_CHARS, EffectiveHandoffStatus
from apis.shared.sessions.messages import get_messages
from apis.shared.sessions.metadata import get_session_compaction, get_session_metadata
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


class HandoffView(BaseModel):
    """A row as the SPA reads it: the stored status, with ``abandoned`` derived."""

    model_config = ConfigDict(populate_by_name=True)

    handoff_id: str = Field(..., alias="handoffId")
    session_id: str = Field(..., alias="sessionId")
    kind: str
    status: EffectiveHandoffStatus
    agent_id: str = Field(..., alias="agentId")
    agent_name: str = Field(..., alias="agentName")
    request: str
    source_message_index: int = Field(..., alias="sourceMessageIndex")
    excerpt_tokens: int = Field(..., alias="excerptTokens")
    turns_covered: Optional[TurnsCovered] = Field(None, alias="turnsCovered")
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")
    target_session_id: Optional[str] = Field(None, alias="targetSessionId")
    status_line: Optional[str] = Field(None, alias="statusLine")
    waiting_on: Optional[str] = Field(None, alias="waitingOn")

    @classmethod
    def from_row(cls, row: Handoff) -> "HandoffView":
        return cls(
            handoff_id=row.handoff_id,
            session_id=row.session_id,
            kind=row.kind,
            status=row.effective_status(),
            agent_id=row.agent_id,
            agent_name=row.agent_name,
            request=row.request,
            source_message_index=row.source_message_index,
            excerpt_tokens=row.excerpt_tokens,
            turns_covered=row.turns_covered,
            created_at=row.created_at,
            updated_at=row.updated_at,
            target_session_id=row.target_session_id,
            status_line=row.status_line,
            waiting_on=row.waiting_on,
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
    messages = [m.model_dump(by_alias=True, exclude_none=True) for m in (listing.messages if listing else [])]
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
        source_message_index=len(messages) - 1,
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
    )


@router.get(
    "/{session_id}/handoffs/{handoff_id}",
    response_model=HandoffView,
    response_model_by_alias=True,
)
async def get_handoff(
    session_id: str,
    handoff_id: str,
    user: User = Depends(require_handoffs_user),
    store: HandoffStore = Depends(get_handoff_store),
) -> HandoffView:
    """One of the caller's handoffs from this conversation."""
    row = await _owned_row(store, user, session_id, handoff_id)
    return HandoffView.from_row(row)


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


async def _owned_row(store: HandoffStore, user: User, session_id: str, handoff_id: str) -> Handoff:
    try:
        row = await asyncio.to_thread(store.get, user.user_id, handoff_id)
    except HandoffStoreUnavailable:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Handoffs are unavailable")
    if row is None or row.session_id != session_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Handoff not found")
    return row
