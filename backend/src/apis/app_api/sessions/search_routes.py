"""``GET /sessions/search``: conversation search (``docs/specs/conversation-search.md`` §5).

Its own router, mounted ahead of the sessions router, so the search surface
stays apart from the session CRUD routes it shares a prefix with.

The caller is whoever the session cookie says (``get_current_user_from_session``).
There is deliberately no user parameter: the user id every lower layer filters
and joins on comes from that dependency and nowhere else (§7, PR-4 isolation
requirements).
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.conversation_search.models import ConversationSearchResponse
from apis.shared.conversation_search.service import DEFAULT_LIMIT, MAX_LIMIT, search_conversations
from apis.shared.feature_flags import conversation_search_enabled

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])

#: Longer than anyone types into a search box; bounds what reaches Bedrock.
MAX_QUERY_CHARS = 200


def _require_conversation_search(_user: User = Depends(get_current_user_from_session)) -> None:
    """404 while ``CONVERSATION_SEARCH_ENABLED`` is off, as if the route did not exist.

    Depends on the session first, so an unauthenticated caller gets 401 and
    learns nothing about which features this deployment runs. FastAPI caches the
    dependency per request, so the session is still resolved once.
    """
    if not conversation_search_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


@router.get(
    "/search",
    response_model=ConversationSearchResponse,
    response_model_by_alias=True,
    dependencies=[Depends(_require_conversation_search)],
)
async def search_sessions_endpoint(
    q: str = Query(..., min_length=1, max_length=MAX_QUERY_CHARS, description="What to look for"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    mode: Literal["lexical", "all"] = Query(
        "lexical",
        description="lexical: titles and opening prompts only (every keystroke). "
        "all: also the full text of every turn (on Enter or a pause).",
    ),
    project_id: Optional[str] = Query(None, alias="projectId", max_length=128),
    current_user: User = Depends(get_current_user_from_session),
) -> ConversationSearchResponse:
    """Search the caller's own conversations by title, opening prompt and full text.

    Results are the caller's sessions only, active or archived, at most one per
    conversation. Text matches carry ``messageId``, the turn to open the
    conversation at. ``textSearchAvailable: false`` means only title and
    opening-prompt matches are in this response although more was asked for.
    """
    try:
        return await search_conversations(
            current_user.user_id,
            q,
            mode=mode,
            limit=limit,
            project_id=project_id,
        )
    except HTTPException:
        raise
    except Exception:
        logger.error("conversation search failed", exc_info=True)
        raise HTTPException(status_code=500, detail="Search failed")


__all__ = ["router"]
