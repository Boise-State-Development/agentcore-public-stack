"""How an archived turn becomes a document in the conversation-search knowledge base.

One archive object (one turn) is one Managed KB document
(docs/specs/conversation-search.md §4):

* ``customDocumentIdentifier`` is ``conv#{session_id}#{message_index}``. Re-ingesting
  an id replaces the document, so a re-archived turn (a paused turn resumed) or a
  redelivered event is idempotent. The search route parses it back with
  :func:`parse_index_document_id` to jump to the turn.
* The text is the user's words, a blank line, the assistant's answer. Tool text was
  already dropped when the turn was archived.
* The attributes are what retrieval filters on: ``user_id`` (every query carries
  ``user_id equals``), ``project_id`` when scoped, and the rest for the result row.

Everything here is derived from the archive *key* where the key carries it. The key
is the authority on whose turn this is: an object whose body names another user or
session is refused (:func:`turn_matches_key`), so a malformed write can never put
one user's text behind another user's filter.

Pure and stdlib-only, so the consumer Lambda, the backfill and the search route
share one definition of the document.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from apis.shared.conversation_archive.documents import ArchivedTurn

#: Prefix of every conversation document id; also what tells a search hit from
#: any other document.
DOCUMENT_ID_PREFIX = "conv#"


def index_document_id(session_id: str, message_index: int) -> str:
    """``conv#{session_id}#{message_index}`` — unpadded, the index as an integer."""
    if not session_id or "#" in session_id:
        raise ValueError("invalid session_id for a conversation document id")
    if message_index < 0:
        raise ValueError("message_index must be non-negative")
    return f"{DOCUMENT_ID_PREFIX}{session_id}#{message_index}"


def parse_index_document_id(document_id: str) -> Optional[Tuple[str, int]]:
    """``(session_id, message_index)`` from a conversation document id, or None."""
    if not document_id.startswith(DOCUMENT_ID_PREFIX):
        return None
    session_id, sep, index = document_id[len(DOCUMENT_ID_PREFIX) :].rpartition("#")
    if not sep or not session_id or "#" in session_id or not index.isdigit():
        return None
    return session_id, int(index)


def index_text(turn: ArchivedTurn) -> str:
    """The document's text: the user's words, a blank line, the answer.

    An empty side is left out rather than leaving a dangling separator; the
    archive never writes a turn with both sides empty.
    """
    return "\n\n".join(part for part in (turn.user_text, turn.assistant_text) if part)


def index_attributes(turn: ArchivedTurn) -> Dict[str, str]:
    """The filterable attributes, all strings (the KB backend sends every
    attribute as ``STRING``). ``project_id`` and ``assistant_id`` only when set,
    so an unscoped turn never matches a ``project_id equals ""`` filter."""
    attributes = {
        "user_id": turn.user_id,
        "session_id": turn.session_id,
        "message_index": str(turn.message_index),
        "created_at": turn.created_at,
    }
    if turn.project_id:
        attributes["project_id"] = turn.project_id
    if turn.assistant_id:
        attributes["assistant_id"] = turn.assistant_id
    return attributes


def turn_matches_key(turn: ArchivedTurn, user_id: str, session_id: str, message_index: int) -> bool:
    """Whether the object's body agrees with the key it was stored under."""
    return (
        turn.user_id == user_id
        and turn.session_id == session_id
        and turn.message_index == message_index
    )


__all__ = [
    "DOCUMENT_ID_PREFIX",
    "index_attributes",
    "index_document_id",
    "index_text",
    "parse_index_document_id",
    "turn_matches_key",
]
