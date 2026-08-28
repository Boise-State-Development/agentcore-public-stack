"""
Property-based tests for score direction across knowledge base backends.

**Property 2: ranking is backend-independent**

For any list of chunks with distinct scores, both backends return the known-best
chunk first after adapter conversion, and the ``relevance`` values they attach
agree with the order they return.

This is the only test in the suite that can catch a silent ranking inversion.
S3 Vectors reports cosine *distance* (lower is better); Managed KB reports
*relevance* (higher is better). If the legacy adapter forwards distance as
relevance, nothing raises: every request still succeeds, still returns five
chunks, and still logs "Found 5 relevant chunks". The only symptom is that the
worst passages are ranked best and answers quietly degrade. There is no error
path, so there is nothing else to assert on.

Feature: managed-kb-migration
**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 24.1**
"""

from typing import Any, Dict, List
from unittest.mock import patch

from hypothesis import given, settings, strategies as st

from apis.shared.kb_backend.protocol import (
    DEFAULT_TOP_K,
    Chunk,
    KnowledgeBaseBackend,
    distance_from_relevance,
    relevance_from_distance,
)
from apis.shared.kb_backend.s3vectors_backend import S3VectorsBackend

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Cosine distance lives in [0, 2]. Distinct values only: the property is about
# strict ranking, and ties would make "the known-best chunk" ambiguous rather
# than wrong.
st_distances = st.lists(
    st.floats(min_value=0.0, max_value=2.0, allow_nan=False, allow_infinity=False),
    min_size=2,
    max_size=8,
    unique=True,
)

# Bounded so that ``score + 1.0`` is genuinely a larger float. Near the top of
# the double range adding 1.0 is a no-op, which would make the pairwise
# comparison below vacuous rather than false.
st_any_score = st.floats(
    min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False
)


# ---------------------------------------------------------------------------
# A managed backend stand-in
# ---------------------------------------------------------------------------


class FakeManagedBackend:
    """A protocol-conforming backend that reports relevance natively.

    Stands in for ``managed_backend.ManagedKbBackend``, which task 8.3 builds.
    The property under test is about score *direction* — a per-adapter concern
    that is fully determined by whether the adapter converts or passes through —
    so a stand-in that passes relevance through unchanged, exactly as Managed KB
    requires (Requirement 2.3), exercises the property faithfully. Nothing here
    depends on Bedrock's wire format.
    """

    def __init__(self, results: List[Dict[str, Any]]):
        # results: [{"document_id", "relevance", "text", "key"}], best first,
        # which is the order Bedrock's Retrieve returns.
        self._results = results

    async def search(self, kb_ref: str, query: str, top_k: int = DEFAULT_TOP_K) -> List[Chunk]:
        return [
            Chunk(
                text=result["text"],
                # Pass-through. Managed already counts in the canonical direction.
                relevance=result["relevance"],
                document_id=result["document_id"],
                metadata={"document_id": result["document_id"], "text": result["text"]},
                key=result["key"],
            )
            for result in self._results
        ]

    async def ingest(self, kb_ref: str, source) -> None:  # pragma: no cover - unused here
        raise NotImplementedError

    async def delete_document(self, kb_ref: str, document_id: str) -> None:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _s3_vectors_response(distances: List[float]) -> Dict[str, Any]:
    """Build an S3 Vectors query response, nearest-first as the API returns it."""
    return {
        "vectors": [
            {
                "key": f"doc-{index}#0",
                "distance": distance,
                "metadata": {"document_id": f"doc-{index}", "text": f"passage {index}"},
            }
            for index, distance in enumerate(sorted(distances))
        ]
    }


async def _legacy_search(distances: List[float]) -> List[Chunk]:
    response = _s3_vectors_response(distances)
    with patch(
        "apis.shared.embeddings.bedrock_embeddings.search_assistant_knowledgebase",
        return_value=response,
    ):
        return await S3VectorsBackend().search("ast-1", "a query")


def _is_non_increasing(values: List[float]) -> bool:
    return all(earlier >= later for earlier, later in zip(values, values[1:]))


# ---------------------------------------------------------------------------
# Property 2
# ---------------------------------------------------------------------------


@given(distances=st_distances)
@settings(max_examples=200, deadline=None)
def test_legacy_backend_ranks_known_best_chunk_first(distances):
    """
    **Validates: Requirements 2.1, 2.2, 2.4**

    The chunk with the *lowest* S3 Vectors distance is the known-best chunk. After
    conversion it must be first in the returned list and must carry the *highest*
    relevance.

    The relevance-ordering assertion is the one that catches an inversion. The
    positional one does not on its own: the adapter preserves the index's order,
    so a chunk stays first whatever score is stapled to it. Only the claim that
    scores descend can detect that the numbers now disagree with the order.
    """
    import asyncio

    chunks = asyncio.run(_legacy_search(distances))

    best_distance = min(distances)
    best_key = f"doc-{sorted(distances).index(best_distance)}#0"

    assert chunks[0].key == best_key, "known-best chunk is not first"

    relevances = [chunk.relevance for chunk in chunks]
    assert _is_non_increasing(relevances), (
        f"relevance must descend with rank, got {relevances}. "
        f"A rising sequence means distance was forwarded as relevance: the "
        f"ranking is inverted and the worst chunks are being served as the best."
    )

    argmax = max(chunks, key=lambda chunk: chunk.relevance)
    assert argmax.key == best_key, (
        f"highest relevance is {argmax.key}, expected the nearest chunk {best_key}"
    )


@given(distances=st_distances)
@settings(max_examples=200, deadline=None)
def test_managed_backend_ranks_known_best_chunk_first(distances):
    """
    **Validates: Requirements 2.1, 2.3, 2.4**

    The managed backend passes relevance through, so the known-best chunk is the
    one with the highest relevance and it must come back first.
    """
    import asyncio

    # The same logical corpus, expressed in the managed backend's own units.
    scored = sorted(
        (
            {
                "document_id": f"doc-{index}",
                "text": f"passage {index}",
                "key": f"doc-{index}#0",
                "relevance": relevance_from_distance(distance),
            }
            for index, distance in enumerate(sorted(distances))
        ),
        key=lambda result: result["relevance"],
        reverse=True,
    )

    backend = FakeManagedBackend(scored)
    chunks = asyncio.run(backend.search("ast-1", "a query"))

    best_key = scored[0]["key"]

    assert chunks[0].key == best_key, "known-best chunk is not first"

    relevances = [chunk.relevance for chunk in chunks]
    assert _is_non_increasing(relevances), (
        f"relevance must descend with rank, got {relevances}"
    )

    argmax = max(chunks, key=lambda chunk: chunk.relevance)
    assert argmax.key == best_key


@given(distances=st_distances)
@settings(max_examples=200, deadline=None)
def test_both_backends_agree_on_ranking(distances):
    """
    **Validates: Requirement 2.4**

    Given the same corpus and the same relative scores, both backends must return
    the same documents in the same order. This is the parity claim a migration
    rests on: a knowledge base that moves engines must not reorder its answers.
    """
    import asyncio

    legacy_chunks = asyncio.run(_legacy_search(distances))

    managed_results = [
        {
            "document_id": chunk.document_id,
            "text": chunk.text,
            "key": chunk.key,
            "relevance": chunk.relevance,
        }
        for chunk in sorted(legacy_chunks, key=lambda chunk: chunk.relevance, reverse=True)
    ]
    managed_chunks = asyncio.run(FakeManagedBackend(managed_results).search("ast-1", "q"))

    assert [chunk.document_id for chunk in legacy_chunks] == [
        chunk.document_id for chunk in managed_chunks
    ], "the two backends ranked the same corpus differently"

    assert [chunk.relevance for chunk in legacy_chunks] == [
        chunk.relevance for chunk in managed_chunks
    ], "the two backends scored the same corpus differently"


# ---------------------------------------------------------------------------
# The derived distance key must be the same value, not a nearby one
# ---------------------------------------------------------------------------


@given(distance=st.floats(min_value=0.0, max_value=2.0, allow_nan=False))
@settings(max_examples=200, deadline=None)
def test_distance_relevance_round_trip_is_exact(distance):
    """
    **Validates: Requirement 2.2**

    The facade derives the ``distance`` it emits from ``relevance``, and that
    value reaches an HTTP response body. The conversion must therefore be exactly
    reversible, not merely close: a ``1.0 - x`` formulation would turn ``0.1``
    into ``0.09999999999999998`` and change a value clients already read.
    """
    assert distance_from_relevance(relevance_from_distance(distance)) == distance


@given(score=st_any_score)
@settings(max_examples=200, deadline=None)
def test_conversion_inverts_direction_for_every_score(score):
    """
    **Validates: Requirements 2.1, 2.2**

    Direction inversion is the whole contract: for any two distinct distances,
    the smaller one must produce the larger relevance. Asserted pointwise against
    a second score so no clamping, absolute value, or identity mapping can pass.
    """
    other = score + 1.0  # strictly greater distance
    assert relevance_from_distance(score) > relevance_from_distance(other), (
        "a nearer chunk (smaller distance) must receive a higher relevance"
    )


def test_none_score_is_preserved_not_fabricated():
    """
    **Validates: Requirement 2.2**

    A response without a distance yields ``None``, which the facade emits
    verbatim as it always has. Defaulting to ``0.0`` would make an unscored
    chunk the best-ranked chunk in the list.
    """
    assert relevance_from_distance(None) is None
    assert distance_from_relevance(None) is None


def test_backends_satisfy_the_protocol():
    """Both implementations structurally conform to KnowledgeBaseBackend."""
    assert isinstance(S3VectorsBackend(), KnowledgeBaseBackend)
    assert isinstance(FakeManagedBackend([]), KnowledgeBaseBackend)
