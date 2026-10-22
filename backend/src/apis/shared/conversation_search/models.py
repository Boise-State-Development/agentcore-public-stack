"""Response shapes for ``GET /sessions/search`` (``docs/specs/conversation-search.md`` §5).

The SPA's ``ConversationSearchResponse`` interface mirrors these field for field.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

MatchKind = Literal["title", "prompt", "text"]


class ConversationSearchResult(BaseModel):
    """One conversation that matched, at most once per session."""

    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(..., alias="sessionId")
    title: str
    last_message_at: str = Field(..., alias="lastMessageAt")
    project_id: Optional[str] = Field(None, alias="projectId")
    assistant_id: Optional[str] = Field(None, alias="assistantId")
    archived: bool = False
    #: The best-matching turn's user message, ``msg-{sessionId}-{index}``: the
    #: anchor the conversation opens scrolled to. Text matches only.
    message_id: Optional[str] = Field(None, alias="messageId")
    #: Up to two more matching turns in the same conversation, best first.
    also_matched: List[str] = Field(default_factory=list, alias="alsoMatched")
    match_kind: MatchKind = Field(..., alias="matchKind")
    #: Plain text, at most 240 characters, around the match. Empty for a title match
    #: with no text match behind it, since the title is already the row.
    snippet: str = ""
    #: The index's reranked relevance (higher is better). Text matches only.
    score: Optional[float] = None


class ConversationSearchResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    results: List[ConversationSearchResult]
    #: False when this response holds lexical matches only although full text was
    #: asked for: the per-user rate was spent, the index is off or not built yet,
    #: or the knowledge base failed. The SPA says "Showing title matches only".
    text_search_available: bool = Field(..., alias="textSearchAvailable")


__all__ = ["ConversationSearchResponse", "ConversationSearchResult", "MatchKind"]
