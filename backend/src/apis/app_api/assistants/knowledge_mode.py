"""``GET /assistants/{id}/knowledge-base/mode``: which mode an Agent's knowledge base runs in.

The Agent Designer's knowledge-base card shows its author one read-only line:
"Sent to the model whole (N documents, ~X tokens)" or "Searched: the model sees
the passages that match each message", with the reason. Authors wrote Loader
instructions ("read Process.txt in your Knowledge in full") that only work in
full-context mode, and nothing told them when their knowledge base had left it
(2026-11 review).

Exact, not estimated. The answer comes from
:func:`apis.shared.assistants.kb_full_context.assess_full_context`, the function
the Runtime renders the block from, under the same flag and budget (CDK writes
both to app-api). An estimate from file sizes was the cheaper option and the
wrong one: a PDF's size says little about its text (a 1.75 MB PDF can hold 5K
tokens or 400K), so it would tell an author "sent whole" about a knowledge base
the Runtime searches, or the reverse. The cost is bounded the way the Runtime's
is: a knowledge base whose text documents are over ``MAX_RAW_BYTES`` (16 MB)
costs one Query and nothing is downloaded; otherwise each text document is
fetched and parsed once per process, then served from the per-document text
cache (``kb_documents``), keyed by what changes when its bytes do. PDF parsing
takes the process-wide PDFium lock like every other PDF path on app-api.

Owner and editor only, like the rest of the Designer's knowledge-base surface;
404 to anyone who cannot see the Agent. A failure answers ``mode: "unknown"``,
which renders nothing: the line is decoration on a working page.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from apis.shared.assistants.service import resolve_assistant_permission
from apis.shared.auth import User, get_current_user_from_session
from apis.shared.security.log_sanitize import scrub_log

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assistants/{assistant_id}/knowledge-base/mode", tags=["assistants"])

_EDIT_PERMISSIONS = ("owner", "editor")

#: ``whole``: the text documents go to the model in the system prompt.
#: ``searched``: each message retrieves the passages that match it.
#: ``empty``: no document is ready yet. ``unknown``: could not be worked out.
KnowledgeMode = Literal["whole", "searched", "empty", "unknown"]

#: Why ``searched``. ``full_context_off`` is the deployment (flag off or a zero
#: budget); the rest are :mod:`kb_full_context`'s ``REASON_*`` values.
SearchedReason = Literal[
    "full_context_off", "no_text_documents", "raw_bytes", "no_text_layer", "document_over_budget", "over_budget"
]


class KnowledgeModeResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mode: KnowledgeMode
    reason: Optional[SearchedReason] = None
    #: Documents whose text is (or would be) sent whole.
    document_count: int = Field(0, alias="documentCount")
    #: Their text in estimated tokens (characters / 4), as the budget counts it.
    #: For ``document_over_budget``, the one oversized document's alone.
    #: 0 when the text was never read (``raw_bytes``, ``full_context_off``).
    estimated_tokens: int = Field(0, alias="estimatedTokens")
    budget_tokens: int = Field(0, alias="budgetTokens")
    #: Spreadsheets and decks, which stay searched in either mode.
    skipped_documents: List[str] = Field(default_factory=list, alias="skippedDocuments")


async def knowledge_mode(assistant_id: str) -> KnowledgeModeResponse:
    """The mode, as the Runtime would decide it now. Access is the caller's job."""
    from apis.shared.assistants import kb_full_context as fc
    from apis.shared.feature_flags import assistant_kb_full_context_enabled

    budget = fc.full_context_max_tokens()
    if not assistant_kb_full_context_enabled() or budget <= 0:
        return KnowledgeModeResponse(mode="searched", reason="full_context_off", budgetTokens=budget)
    found = await asyncio.to_thread(fc.assess_full_context, assistant_id, budget)
    if found.reason == fc.REASON_EMPTY:
        return KnowledgeModeResponse(mode="empty", budgetTokens=budget)
    return KnowledgeModeResponse(
        mode="whole" if found.eligible else "searched",
        reason=None if found.eligible else found.reason,
        documentCount=found.document_count,
        estimatedTokens=found.estimated_tokens,
        budgetTokens=budget,
        skippedDocuments=list(found.skipped),
    )


@router.get("", response_model=KnowledgeModeResponse, response_model_by_alias=True)
async def read_knowledge_mode(
    assistant_id: str,
    current_user: User = Depends(get_current_user_from_session),
) -> KnowledgeModeResponse:
    """Which mode this Agent's knowledge base runs in, and why."""
    assistant, permission = await resolve_assistant_permission(
        assistant_id=assistant_id, user_id=current_user.user_id, user_email=current_user.email
    )
    if not assistant or not permission:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Assistant not found: {assistant_id}")
    if permission not in _EDIT_PERMISSIONS:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the Agent's editors can see this")
    try:
        return await knowledge_mode(assistant_id)
    except Exception as exc:  # noqa: BLE001 — decoration on a working page
        logger.error(
            f"kb {scrub_log(assistant_id)}: could not work out the knowledge-base mode: {scrub_log(exc)}",
            exc_info=True,
        )
        return KnowledgeModeResponse(mode="unknown")
