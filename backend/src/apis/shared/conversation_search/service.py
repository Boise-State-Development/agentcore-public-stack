"""Conversation search: the lexical leg, the text leg, and how they merge.

``docs/specs/conversation-search.md`` §5. Two legs, run concurrently:

* **Lexical** (every keystroke, ``mode=lexical``): the caller's session rows
  whose title or opening prompt contains the query
  (:func:`~apis.shared.conversation_search.session_rows.search_user_sessions_lexical`).
  DynamoDB only.
* **Text** (``mode=all``, on Enter or a pause): the shared ``conversations``
  knowledge base, through the one function allowed to query it
  (:func:`~apis.shared.conversation_search.index_search.search_conversation_index`).
  Each hit is joined to the caller's own session row and dropped when the row
  is missing or deleted, or when the turn is past retention (§3's read-path
  belt). Retrieve is ~670 ms p50, so it never runs per keystroke, and a per-user
  token bucket caps it at :data:`TEXT_SEARCHES_PER_MINUTE`; past that the
  response is lexical with ``textSearchAvailable: false``.

Merge: title matches first, then text matches by reranked score, then
opening-prompt matches; one row per conversation. A conversation that matched
by title *and* by text keeps its title position and carries the text match's
turn and snippet, so it still opens at the passage.

Nothing a search does is persisted. Not on the turn path: app-api only.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

from apis.shared.conversation_search.index_search import IndexHit, search_conversation_index
from apis.shared.conversation_search.models import ConversationSearchResponse, ConversationSearchResult
from apis.shared.conversation_search.session_rows import (
    SessionRow,
    get_owned_session_rows,
    search_user_sessions_lexical,
)
from apis.shared.sessions.search_text import make_snippet, normalize_search_text, parse_timestamp

logger = logging.getLogger(__name__)

#: §5: the backstop against the knowledge base's 600 Retrieve/min quota.
TEXT_SEARCHES_PER_MINUTE = 20

#: A query shorter than this never reaches the text leg (§5: "≥ 3 characters").
TEXT_SEARCH_MIN_CHARS = 3

DEFAULT_LIMIT = 20
MAX_LIMIT = 50

#: More matching turns carried per conversation, beyond the best one.
_ALSO_MATCHED_MAX = 2


class TextSearchThrottle:
    """A token bucket per user: ``capacity`` text searches, refilled over a minute.

    Per process. app-api runs a few tasks behind the load balancer, so a user
    spread across them gets a few times the nominal rate; as a backstop against
    one user draining the shared per-knowledge-base quota that is ample, and it
    costs no write per search. The table of buckets is bounded (least recently
    seen user evicted first), so memory does not grow with the user count.
    """

    def __init__(self, capacity: int = TEXT_SEARCHES_PER_MINUTE, per_seconds: float = 60.0, max_users: int = 10_000, clock=time.monotonic) -> None:
        self._capacity = float(capacity)
        self._refill_per_second = capacity / per_seconds
        self._max_users = max_users
        self._clock = clock
        self._buckets: "OrderedDict[str, Tuple[float, float]]" = OrderedDict()
        self._lock = threading.Lock()

    def try_acquire(self, user_id: str) -> bool:
        now = self._clock()
        with self._lock:
            tokens, updated = self._buckets.pop(user_id, (self._capacity, now))
            tokens = min(self._capacity, tokens + (now - updated) * self._refill_per_second)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            self._buckets[user_id] = (tokens, now)
            while len(self._buckets) > self._max_users:
                self._buckets.popitem(last=False)
            return allowed

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


_throttle = TextSearchThrottle()


def text_search_throttle() -> TextSearchThrottle:
    return _throttle


def _message_id(session_id: str, message_index: int) -> str:
    """The id the messages route gives a message (``msg-{sessionId}-{index}``)."""
    return f"msg-{session_id}-{message_index}"


def _within_retention(created_at: Optional[str], cutoff) -> bool:
    """§3's read-path belt. A turn with no readable timestamp is kept: the row
    join has already shown the conversation exists, and the reconciler, not
    this check, is what removes expired documents."""
    if cutoff is None:
        return True
    moment = parse_timestamp(created_at)
    return moment is None or moment >= cutoff


def _retention_cutoff():
    from apis.shared.conversation_archive.retention import retention_cutoff

    try:
        return retention_cutoff()
    except ValueError:
        # A malformed setting must not take search down; the reconciler raises on it.
        logger.warning("CONVERSATION_RETENTION_DAYS is malformed; search skips its retention check")
        return None


def _collapse_text_hits(
    hits: List[IndexHit], rows: Dict[str, SessionRow], query: str, project_id: Optional[str]
) -> List[ConversationSearchResult]:
    """One result per conversation, keeping its best turn, in the index's order."""
    cutoff = _retention_cutoff()
    by_session: Dict[str, ConversationSearchResult] = {}
    order: List[str] = []
    for hit in hits:
        row = rows.get(hit.session_id)
        if row is None:
            continue
        if project_id and row.project_id != project_id:
            continue
        if not _within_retention(hit.created_at, cutoff):
            continue
        existing = by_session.get(hit.session_id)
        if existing is not None:
            if len(existing.also_matched) < _ALSO_MATCHED_MAX:
                existing.also_matched.append(_message_id(hit.session_id, hit.message_index))
            continue
        by_session[hit.session_id] = ConversationSearchResult(
            session_id=row.session_id,
            title=row.title,
            last_message_at=row.last_message_at,
            project_id=row.project_id,
            assistant_id=row.assistant_id,
            archived=row.archived,
            message_id=_message_id(hit.session_id, hit.message_index),
            match_kind="text",
            snippet=make_snippet(hit.text, query),
            score=hit.score,
        )
        order.append(hit.session_id)
    return [by_session[sid] for sid in order]


def _lexical_results(rows: List[SessionRow], query: str) -> Tuple[List[ConversationSearchResult], List[ConversationSearchResult]]:
    """Split lexical rows into title and opening-prompt matches, newest first."""
    needle = normalize_search_text(query)
    titles: List[ConversationSearchResult] = []
    prompts: List[ConversationSearchResult] = []
    for row in sorted(rows, key=lambda r: (r.last_message_at, r.session_id), reverse=True):
        title_match = bool(needle) and needle in row.title_lower
        result = ConversationSearchResult(
            session_id=row.session_id,
            title=row.title,
            last_message_at=row.last_message_at,
            project_id=row.project_id,
            assistant_id=row.assistant_id,
            archived=row.archived,
            match_kind="title" if title_match else "prompt",
            snippet="" if title_match else make_snippet(row.first_prompt, query),
        )
        (titles if title_match else prompts).append(result)
    return titles, prompts


def merge_results(
    titles: List[ConversationSearchResult],
    texts: List[ConversationSearchResult],
    prompts: List[ConversationSearchResult],
    limit: int,
) -> List[ConversationSearchResult]:
    """Titles, then text matches by score, then prompts; one row per session.

    A title match that also matched by text takes the text match's turn,
    snippet and score, so opening it still jumps to the passage.
    """
    text_by_session = {r.session_id: r for r in texts}
    merged: List[ConversationSearchResult] = []
    seen = set()
    for result in titles:
        text = text_by_session.get(result.session_id)
        if text is not None:
            result = result.model_copy(
                update={
                    "message_id": text.message_id,
                    "also_matched": list(text.also_matched),
                    "snippet": text.snippet,
                    "score": text.score,
                }
            )
        merged.append(result)
        seen.add(result.session_id)
    for result in [*texts, *prompts]:
        if result.session_id in seen:
            continue
        merged.append(result)
        seen.add(result.session_id)
    return merged[:limit]


async def _text_leg(user_id: str, query: str, project_id: Optional[str]) -> List[ConversationSearchResult]:
    hits = await search_conversation_index(user_id, query, project_id=project_id)
    rows = await get_owned_session_rows(user_id, (hit.session_id for hit in hits))
    dropped = len({h.session_id for h in hits} - set(rows))
    if dropped:
        logger.info("conversation search dropped %d hit session(s) with no live row", dropped)
    return _collapse_text_hits(hits, rows, query, project_id)


async def search_conversations(
    user_id: str,
    query: str,
    *,
    mode: str = "lexical",
    limit: int = DEFAULT_LIMIT,
    project_id: Optional[str] = None,
) -> ConversationSearchResponse:
    """Search the caller's conversations. ``user_id`` must come from the session cookie."""
    from apis.shared.feature_flags import conversation_index_enabled

    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("a conversation search requires the caller's user id")
    limit = max(1, min(int(limit), MAX_LIMIT))
    project_id = project_id or None
    index_on = conversation_index_enabled()

    wants_text = mode == "all" and len(query.strip()) >= TEXT_SEARCH_MIN_CHARS
    run_text = wants_text and index_on and text_search_throttle().try_acquire(user_id)

    lexical_task = search_user_sessions_lexical(user_id, query, project_id=project_id)
    if run_text:
        lexical_rows, text_outcome = await asyncio.gather(
            lexical_task, _text_leg(user_id, query, project_id), return_exceptions=True
        )
        if isinstance(lexical_rows, BaseException):
            raise lexical_rows
    else:
        lexical_rows = await lexical_task
        text_outcome = []

    text_available = index_on
    if wants_text and not run_text:
        text_available = False
    if isinstance(text_outcome, BaseException):
        from apis.shared.kb_backend.managed_backend import ManagedKbNotProvisioned

        if isinstance(text_outcome, ManagedKbNotProvisioned):
            # Expected until the first turn is indexed in this environment.
            logger.info("conversation search: the conversations knowledge base is not provisioned yet")
        else:
            logger.warning(
                "conversation search text leg failed; returning lexical matches: %s",
                type(text_outcome).__name__,
                exc_info=text_outcome,
            )
        text_outcome = []
        text_available = False

    titles, prompts = _lexical_results(lexical_rows, query)
    return ConversationSearchResponse(
        results=merge_results(titles, text_outcome, prompts, limit),
        text_search_available=text_available,
    )


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "TEXT_SEARCHES_PER_MINUTE",
    "TEXT_SEARCH_MIN_CHARS",
    "TextSearchThrottle",
    "merge_results",
    "search_conversations",
    "text_search_throttle",
]
