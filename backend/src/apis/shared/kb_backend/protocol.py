"""The one seam every knowledge base read and write passes through.

Two backends sit behind :class:`KnowledgeBaseBackend`: the legacy Amazon S3
Vectors implementation and, from task 8, Amazon Bedrock Managed Knowledge Base.
Callers never learn which one they got.

Score direction
---------------
This module's single most important decision is the name of one field.

The two backends disagree about which direction is better:

* S3 Vectors returns cosine **distance** — *lower* is more similar.
* Managed KB returns **relevance** — *higher* is more relevant.

Get that backwards and nothing raises. No log line, no alarm, no failing
request: retrieval simply serves the least relevant chunks it can find, and the
only symptom is that answers get worse. There is no error path to catch because
there is no error. That is why the canonical field is named ``relevance`` and
documented here rather than left implicit, why the legacy adapter converts
*inside itself* rather than at some call site, and why
``tests/property/test_pbt_kb_score_direction.py`` exists at all.

Exact round-trip
----------------
:func:`relevance_from_distance` and :func:`distance_from_relevance` are exact
inverses under IEEE-754, not approximate ones, because the facade still emits a
``distance`` key derived from ``relevance`` and
``app_api/assistants/routes.py`` puts that value in an HTTP response body. A
``1.0 - x`` conversion would round-trip ``0.1`` to ``0.09999999999999998`` and
change a value a client already reads. Negation is exact for every finite float,
so the derived value is the same value, not a very close one.

Import boundary
---------------
Module-level imports here are **stdlib only**. ``apis.shared.kb_backend`` is
bundled into size-constrained Lambda images and must not drag in
``apis.shared.assistants`` (whose ``__init__`` imports the embeddings stack) or
``boto3``. Enforced by ``tests/architecture/test_kb_backend_boundary.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

#: Parity contract, Requirement 3.1: both backends are asked for five chunks.
#: Named here, above the seam, so neither adapter can drift from the other.
DEFAULT_TOP_K = 5


def relevance_from_distance(distance: Optional[float]) -> Optional[float]:
    """Convert a cosine **distance** (lower is better) to **relevance**.

    Negation, deliberately, rather than ``1.0 - distance``: it inverts the
    direction — which is the entire job — while being exactly reversible by
    :func:`distance_from_relevance` for every finite float. Nothing compares
    relevance values *across* backends (the dual-read pilot compares rank order,
    not magnitude), so the absolute range is free and exactness is not.

    ``None`` passes through as ``None``. The S3 Vectors query always asks for
    distances, so a missing one means a malformed response; the facade's
    long-standing behaviour is to emit ``distance: None`` rather than invent a
    score, and fabricating ``0.0`` here would promote such a chunk to
    best-in-class.
    """
    if distance is None:
        return None
    return -distance


def distance_from_relevance(relevance: Optional[float]) -> Optional[float]:
    """Recover the original distance from a relevance. Exact inverse of above."""
    if relevance is None:
        return None
    return -relevance


@dataclass(frozen=True)
class Chunk:
    """One retrieved passage, in the shape every backend must produce.

    Frozen because a chunk crosses the seam as a value: an adapter that returned
    something a caller could mutate would let ranking be edited after the
    backend had decided it.
    """

    text: str

    #: Canonical score. **HIGHER IS MORE RELEVANT**, on both backends, always.
    #: The legacy adapter has already converted S3 Vectors' inverted distance by
    #: the time a chunk exists. ``None`` means the backend reported no score
    #: (see :func:`relevance_from_distance`); it is preserved, never defaulted,
    #: because a default would be indistinguishable from a real score.
    relevance: Optional[float]

    #: Platform document id. The status filter joins on this, and on the managed
    #: path it is the ``customDocumentIdentifier`` (task 8.4).
    document_id: str

    metadata: Dict[str, Any] = field(default_factory=dict)

    #: Backend-native identifier: ``{document_id}#{chunk_index}`` on legacy.
    key: str = ""


@dataclass(frozen=True)
class DocumentSource:
    """A document to ingest, in the least-common-denominator form.

    The two backends want different things — legacy needs text already chunked,
    managed needs the source bytes or an S3 location — so both are optional here
    and each adapter validates what it needs. Tasks 8.4 and 9.1 extend this;
    it exists now only so the protocol's ``ingest`` signature is real rather
    than ``Any``.
    """

    document_id: str
    filename: str
    chunks: Optional[List[str]] = None
    s3_key: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class KnowledgeBaseBackend(Protocol):
    """What both backends implement, and all a caller may assume.

    ``kb_ref`` is the application-owned reference to the knowledge base — the
    ``App_KB_Id``, which equals the ``assistant_id`` in this phase. Never an AWS
    ``knowledgeBaseId``: those are replaceable across a dormancy/rehydration
    cycle, so an adapter resolves one internally and no caller holds one.

    ``runtime_checkable`` supports ``isinstance`` structural assertions in the
    tests. It checks method *presence* only, never signatures, so it is a
    guard against a missing method, not a substitute for reading the protocol.
    """

    async def search(self, kb_ref: str, query: str, top_k: int = DEFAULT_TOP_K) -> List[Chunk]:
        """Return up to ``top_k`` chunks, best first, scored by relevance.

        Ordering is the backend's: both underlying APIs return results ranked
        best-first, and an adapter re-sorting them would be inventing a ranking
        rather than reporting one. What an adapter *must* guarantee is that its
        ``relevance`` values agree with the order it returns.
        """
        ...

    async def ingest(self, kb_ref: str, source: DocumentSource) -> None:
        """Index ``source`` into the knowledge base."""
        ...

    async def delete_document(self, kb_ref: str, document_id: str) -> None:
        """Remove every trace of ``document_id`` from the knowledge base."""
        ...
