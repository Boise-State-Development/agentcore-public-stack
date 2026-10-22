"""The only code that queries the shared ``conversations`` knowledge base.

Every user's turns live in **one** Managed Knowledge Base, told apart only by
the ``user_id`` attribute each document carries
(``docs/specs/conversation-search.md`` §4). Isolation is therefore ours to
enforce, and it is enforced here, in one place (§7, PR-4 isolation requirements):

1. :func:`search_conversation_index` takes the caller's user id as a required
   argument and builds the ``user_id equals`` filter itself (``andAll`` with
   ``project_id equals`` when scoped). It accepts no filter from its caller, so
   no caller can widen, replace or forget it. The route passes the id it got
   from ``get_current_user_from_session``, never one from the request.
2. Nothing else calls ``Retrieve`` with the conversations KB id.
   ``tests/architecture/test_conversation_index_retrieve_boundary.py`` fails the
   build if another module does.
3. A hit is dropped here unless **both** the user its document id names
   (``conv#{user_id}#{session_id}#{message_index}``) and its ``user_id``
   attribute are the caller's, and every surviving hit is joined to its session
   row under ``USER#{caller}`` by the service above (``service.py``), which
   drops it when that row is missing or deleted. Any one of the three would
   stop a leak through a lost filter. A legacy two-part id
   (``conv#{session_id}#{message_index}``, from before the user was put in the
   id because session ids are not unique across users) names no user, so it is
   never a hit; the reconciler removes those documents.

A missing or empty user id raises before anything is sent: an empty ``equals``
value makes Bedrock raise ``ValidationException`` (measured on dev 2026-10-07),
and an empty id is in any case never a caller anyone should be searching as.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from apis.shared.conversation_archive.index_documents import parse_index_document_id
from apis.shared.kb_backend.reserved import CONVERSATIONS_KB_ID

logger = logging.getLogger(__name__)

#: How many turns one text search asks for (§5). Collapsing to one result per
#: session happens above, so a handful of conversations can fill it.
DEFAULT_TOP_K = 20


@dataclass(frozen=True)
class IndexHit:
    """One retrieved turn, as the index described it."""

    session_id: str
    message_index: int
    text: str
    score: Optional[float]
    created_at: Optional[str]
    project_id: Optional[str]


def conversation_filter(user_id: str, project_id: Optional[str] = None) -> Dict[str, Any]:
    """The retrieval filter for one caller: ``user_id equals``, plus the project.

    Exact-match operators only, which ``validate_isolation_filter`` also insists
    on. Public so the isolation tests can assert the exact shape that is sent.
    """
    user_clause = {"equals": {"key": "user_id", "value": user_id}}
    if not project_id:
        return user_clause
    return {"andAll": [user_clause, {"equals": {"key": "project_id", "value": project_id}}]}


def _require_user_id(user_id: Any) -> str:
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("a conversation index search requires the caller's user id")
    return user_id


async def search_conversation_index(
    user_id: str,
    query: str,
    *,
    project_id: Optional[str] = None,
    top_k: int = DEFAULT_TOP_K,
    backend: Any = None,
) -> List[IndexHit]:
    """Search one user's archived turns, best first.

    ``backend`` is injectable for tests (a ``ManagedKbBackend`` by default); it
    is a client, never a filter. Raises ``ManagedKbNotProvisioned`` when the
    knowledge base has not been created yet (no turn has been indexed in this
    environment) and lets any Bedrock error through; the caller decides how to
    degrade.
    """
    caller = _require_user_id(user_id)
    if project_id is not None and not project_id.strip():
        project_id = None
    if not query or not query.strip():
        return []

    if backend is None:
        from apis.shared.kb_backend.managed_backend import ManagedKbBackend

        backend = ManagedKbBackend()

    chunks = await backend.search(
        CONVERSATIONS_KB_ID,
        query.strip(),
        top_k,
        conversation_filter(caller, project_id),
    )

    hits: List[IndexHit] = []
    foreign = 0
    for chunk in chunks:
        ref = parse_index_document_id(chunk.document_id or "")
        if ref is None:
            continue
        metadata = chunk.metadata or {}
        # Fail closed: a hit whose id or attribute does not name the caller is
        # never theirs, and one with no owner attribute cannot be shown to be.
        if ref.user_id != caller or metadata.get("user_id") != caller:
            foreign += 1
            continue
        if project_id and metadata.get("project_id") != project_id:
            foreign += 1
            continue
        hits.append(
            IndexHit(
                session_id=ref.session_id,
                message_index=ref.message_index,
                text=chunk.text or "",
                score=chunk.relevance,
                created_at=metadata.get("created_at"),
                project_id=metadata.get("project_id"),
            )
        )

    if foreign:
        # The filter should make this impossible. Count it, never show it.
        logger.warning("conversation search dropped %d hit(s) outside the caller's filter", foreign)
    return hits


__all__ = ["DEFAULT_TOP_K", "IndexHit", "conversation_filter", "search_conversation_index"]
