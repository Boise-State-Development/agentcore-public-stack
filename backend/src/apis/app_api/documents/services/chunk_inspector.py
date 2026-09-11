"""Show an owner the content their knowledge base actually extracted from a document.

Feature: `kb-chunk-inspector`. The tooling half of the task-16.2 decision on
`managed-kb-migration` §5.41.

Why this exists
---------------
The two backends fail differently, and the managed one fails worse:

* **Legacy** failed **silently** — a column-structured flowchart produced 0 chunks,
  so a question about it got no answer. Bad, but visible.
* **Managed** fails **invisibly** — the vision/parse step flattens a 2-D layout at
  ingestion, so a question about "semester 4" or a per-column total gets a
  *confident wrong answer* with no trace of why.

Task 16.2 decided not to fix the parser: it is a managed service, and this is a
self-service platform where owners upload their own documents. The mitigation is
guidance. But guidance is useless if the user cannot see the problem — nobody can act
on "reformat your flowchart as a text table" without first believing their flowchart
came out wrong. This module is what lets them look.

Read-only. No new chunking, parsing or ingestion path (Requirement 5).

Managed-only, and that is a correction to the design
----------------------------------------------------
The design document assumed ``backend.search(..., retrieval_filter=...)`` was part of
the protocol and that the inspector could therefore be engine-agnostic. It is not:
``retrieval_filter`` exists **only** on ``ManagedKbBackend.search``. The legacy
adapter's signature is ``search(kb_ref, query, top_k)``, it accepts no filter, and it
ignores ``top_k`` — it always asks its index for a fixed five results across the
*whole* knowledge base.

So on legacy there is no way to scope a retrieval to one document. Running it anyway
would return the top five chunks of the entire knowledge base, most or all of them
belonging to **other documents** — the user opens "view extracted content" on their
syllabus and reads somebody else's handbook. That is a cross-document leak, which is
precisely what Requirement 4 and ``ISOLATION_SAFE_FILTER_OPERATORS`` exist to
prevent; it is not a cosmetic problem to be tidied up later.

Teaching the legacy adapter to filter was rejected: Requirement 5 says the inspector
must not depend on the legacy pipeline continuing to exist, and that pipeline is being
deprecated. Building new capability into it would be work with a negative lifespan.

So a legacy document returns ``available=False`` with a reason, as a **200 rather than
an error**: the owner asked a reasonable question and "your knowledge base is on the
classic engine, which cannot show this" is an answer, not a fault. The response shape
is identical either way, so the UI never branches on the engine — which is what
Requirement 2 was actually protecting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Chunks requested in one `Retrieve`. Bedrock documents 100 as the ceiling for
#: `numberOfResults`, and the inspector wants as complete a picture as one bounded
#: call can give — unlike a real query, where 5 is the answer-quality choice.
#:
#: One call, not a pagination loop (Requirement 6). `Retrieve` is query-ranked with no
#: cursor, so "page 2" is not a thing it offers; repeating the call with a different
#: query would return an overlapping arbitrary subset and cost another 662–695 ms for
#: no guarantee of new content.
INSPECT_TOP_K = 100

#: Statuses that can be inspected. Only `complete` — the retrieval facade serves only
#: `complete`, so anything else has no chunks in the knowledge base to show.
INSPECTABLE_STATUS = "complete"

#: Why a document cannot be inspected yet, in the owner's language. Keyed on the
#: `DOC#` status. `provisioning` is born-managed's leading status (the knowledge base
#: itself is still being created), and it gets its own sentence because "not found"
#: would be a lie and "still processing" would understate the wait.
_NOT_READY_REASONS = {
    "provisioning": (
        "This assistant's knowledge base is still being created. The extracted "
        "content will be available once the first document has finished processing."
    ),
    "uploading": "This document is still being processed. Check back shortly.",
    "chunking": "This document is still being processed. Check back shortly.",
    "embedding": "This document is still being processed. Check back shortly.",
    "failed": (
        "This document could not be processed, so the knowledge base holds no "
        "content for it. Upload it again, or convert it to a different format."
    ),
}

_NOT_READY_FALLBACK = "This document is not ready to inspect yet."

LEGACY_UNAVAILABLE_REASON = (
    "This assistant uses the classic knowledge base, which cannot list the "
    "extracted content for a single document. Upgrading the assistant's knowledge "
    "base makes this view available."
)


class DocumentNotInspectable(Exception):
    """The document exists but has no content to show yet. Carries owner-safe copy."""

    def __init__(self, reason: str, status: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass
class InspectedChunk:
    """One passage as the knowledge base holds it.

    ``text`` is deliberately **not** truncated. The existing citation trace caps
    excerpts at 500 characters, which is the whole reason it cannot serve this
    purpose: a flattened table's damage is frequently past the cut, and a truncated
    excerpt of a mangled table looks like a fine excerpt of a fine table.
    """

    text: str
    order: int
    score: Optional[float] = None
    page: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class InspectionResult:
    document_id: str
    file_name: str
    engine: str
    available: bool
    chunks: List[InspectedChunk] = field(default_factory=list)
    returned: int = 0
    cap_reached: bool = False
    reason: Optional[str] = None


def document_filter(document_id: str) -> Dict[str, Any]:
    """The retrieval filter scoping results to exactly one document.

    ``equals``, never a prefix or substring operator. This is the same filter the
    ingestion consumer's retrievability probe and the document reconciler use, and it
    is in ``ISOLATION_SAFE_FILTER_OPERATORS`` for a reason worth restating: a prefix
    match for ``DOC-1`` also admits ``DOC-10``, ``DOC-11`` and so on, so the operator
    choice is the isolation boundary rather than a query-tuning detail.
    """
    return {"equals": {"key": "document_id", "value": document_id}}


def _page_of(metadata: Dict[str, Any]) -> Optional[int]:
    """Page number when the backend supplied one, else ``None``.

    Never invented. A fabricated page number would be indistinguishable from a real
    one and would make an unordered result set look authoritatively ordered.
    """
    for key in ("page", "pageNumber", "page_number", "x-amz-bedrock-kb-document-page-number"):
        raw = metadata.get(key)
        if raw is None:
            continue
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            continue
    return None


def _dedupe(chunks: List[Any]) -> List[Any]:
    """Drop repeated passages, preserving first-seen order.

    ``Retrieve`` is query-ranked rather than a cursor over a set, so the same passage
    can legitimately come back more than once. Showing an owner the same mangled
    table three times would make them think it was ingested three times.
    """
    seen = set()
    unique = []
    for chunk in chunks:
        fingerprint = hash(chunk.text)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(chunk)
    return unique


async def inspect_document_chunks(
    assistant_id: str,
    document_id: str,
    *,
    file_name: str,
    status: str,
) -> InspectionResult:
    """The content the knowledge base holds for one document.

    Raises :class:`DocumentNotInspectable` when the document exists but has nothing
    to show; the caller turns that into a 409 with the carried copy.
    """
    from apis.shared.kb_backend import resolver
    from apis.shared.kb_backend.query_guard import clamp_query
    from apis.shared.kb_backend.records import ENGINE_MANAGED

    if status != INSPECTABLE_STATUS:
        raise DocumentNotInspectable(
            _NOT_READY_REASONS.get(status, _NOT_READY_FALLBACK), status
        )

    record = resolver.load_record(assistant_id)
    engine = resolver.resolve_engine_for(assistant_id, record=record)

    if engine != ENGINE_MANAGED:
        # See the module docstring: legacy cannot scope a retrieval to one document,
        # and returning the whole knowledge base's top chunks would leak other
        # documents' content into this view.
        return InspectionResult(
            document_id=document_id,
            file_name=file_name,
            engine=engine,
            available=False,
            reason=LEGACY_UNAVAILABLE_REASON,
        )

    backend = resolver.resolve_backend(assistant_id, record=record)

    # The filter is what scopes the result; this text exists only because `Retrieve`
    # requires a query. The filename is the most document-anchored string available
    # without reading the object, and clamp_query is reused rather than reinvented so
    # an absurd filename cannot become an absurd query.
    query, _ = clamp_query(file_name or document_id)

    chunks = await backend.search(
        assistant_id,
        query,
        INSPECT_TOP_K,
        retrieval_filter=document_filter(document_id),
    )

    # Defence in depth. The filter should make this a no-op, and if it ever is not,
    # the failure must not be "the owner reads another document's content".
    scoped = [chunk for chunk in chunks if chunk.document_id == document_id]
    if len(scoped) != len(chunks):
        logger.error(
            f"chunk inspector: {len(chunks) - len(scoped)} chunk(s) for a document "
            f"other than {document_id} came back through a document-scoped filter; "
            f"dropped them"
        )

    unique = _dedupe(scoped)

    inspected = [
        InspectedChunk(
            text=chunk.text,
            order=index,
            score=chunk.relevance,
            page=_page_of(chunk.metadata or {}),
            metadata={},
        )
        for index, chunk in enumerate(unique)
    ]

    # `capReached` is honesty, not a paging hint (Requirement 3). Bedrock exposes no
    # chunk-enumeration API, so a full result set is never guaranteed and the UI must
    # say "up to N" rather than implying the document has exactly N chunks.
    cap_reached = len(chunks) >= INSPECT_TOP_K

    logger.info(
        f"chunk inspector: assistant={assistant_id} document={document_id} "
        f"engine={engine} returned={len(inspected)} capReached={cap_reached}"
    )

    return InspectionResult(
        document_id=document_id,
        file_name=file_name,
        engine=engine,
        available=True,
        chunks=inspected,
        returned=len(inspected),
        cap_reached=cap_reached,
    )
