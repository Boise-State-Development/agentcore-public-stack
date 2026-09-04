"""The legacy Amazon S3 Vectors backend, behind the common protocol.

This is the retrieval path every assistant chat has used to date, moved here
unchanged and wrapped in :class:`~apis.shared.kb_backend.protocol.Chunk`. The
only thing this adapter *adds* is the score-direction conversion, and the only
thing it takes away from ``rag_service`` is knowledge of what an S3 Vectors
response looks like.

Why this delegates instead of copying the query
-----------------------------------------------
``apis.shared.embeddings.bedrock_embeddings.search_assistant_knowledgebase``
stays where it is and this adapter calls it. It is a published export of two
packages (``apis.shared.embeddings`` and
``apis.app_api.documents.ingestion.embeddings``), and task 5.2 of this spec
still expects to edit it in place. Re-implementing its ``query_vectors`` call
here would mean two copies of the topK/filter/returnDistance construction, which
is precisely the divergence risk this seam exists to remove. What moves here is
everything ``rag_service`` used to know: the response shape, the score
direction, and the parity ``top_k``.

Score direction — read this before touching :meth:`S3VectorsBackend.search`
---------------------------------------------------------------------------
S3 Vectors returns cosine **distance**: ``0.0`` is a perfect match and larger is
worse. The protocol canonicalizes on **relevance**, where larger is better. The
conversion happens *here*, once, so that nothing above the seam ever has to know
which direction this particular backend counts in.

Inverting it raises nothing and logs nothing. Retrieval keeps returning five
chunks, the request keeps succeeding, and the answers quietly get worse. The
guard is ``tests/property/test_pbt_kb_score_direction.py``.

Ordering is *not* re-sorted here. S3 Vectors already returns results ranked
nearest-first, and today's code passes that order straight through; re-sorting
would be a behaviour change dressed up as a safety measure. The invariant this
adapter owns is that the ``relevance`` values it attaches agree with the order it
returns — descending relevance for ascending distance.

Import boundary
---------------
Module-level imports are **stdlib only**; ``boto3`` and the embeddings stack are
imported inside the methods that use them, so importing this module into a
size-constrained Lambda image costs nothing. See
``tests/architecture/test_kb_backend_boundary.py``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from apis.shared.kb_backend.protocol import (
    DEFAULT_TOP_K,
    Chunk,
    DocumentSource,
    relevance_from_distance,
)

logger = logging.getLogger(__name__)


class S3VectorsBackend:
    """Retrieval and ingestion over the S3 Vectors index.

    ``kb_ref`` is the ``App_KB_Id``, which equals the ``assistant_id`` in this
    phase; the S3 Vectors index is global and partitioned by an ``assistant_id``
    metadata filter, so the reference is used directly as that filter value.

    Stateless, so a shared instance is safe and no client is held across calls.
    """

    async def search(self, kb_ref: str, query: str, top_k: int = DEFAULT_TOP_K) -> List[Chunk]:
        """Query the index and return chunks scored by relevance, best first.

        ``top_k`` is accepted to satisfy the protocol but the underlying query
        has always requested a fixed five results (Requirement 3.1), and
        narrowing happens above the seam *after* the document-status filter has
        run — filtering first and slicing second is what stops a single
        incomplete document from silently shrinking a five-chunk answer to four.
        Slicing here instead would change that, so this returns what the index
        returned.
        """
        from apis.shared.embeddings.bedrock_embeddings import search_assistant_knowledgebase

        response = await search_assistant_knowledgebase(kb_ref, query)
        vectors = response.get("vectors", [])
        return [self._to_chunk(vector) for vector in vectors]

    @staticmethod
    def _to_chunk(vector: Dict[str, Any]) -> Chunk:
        """Adapt one S3 Vectors hit, converting distance into relevance.

        The ``.get`` defaults mirror the formatting this replaced exactly: a
        missing ``text`` or ``key`` became ``""`` and a missing ``distance``
        became ``None``, so they still do.
        """
        metadata = vector.get("metadata", {})
        return Chunk(
            text=metadata.get("text", ""),
            # The conversion. Lower distance ⇒ higher relevance.
            relevance=relevance_from_distance(vector.get("distance")),
            document_id=metadata.get("document_id", ""),
            metadata=metadata,
            key=vector.get("key", ""),
        )

    async def ingest(self, kb_ref: str, source: DocumentSource) -> None:
        """Embed and store ``source``'s chunks, as the current pipeline does.

        Requires pre-chunked text: splitting is the ingestion pipeline's job
        (it owns the tokenizer this package deliberately does not depend on),
        so an unchunked source is a programming error rather than something to
        paper over with a naive split.
        """
        from apis.shared.embeddings.bedrock_embeddings import (
            generate_embeddings,
            store_embeddings_in_s3,
        )

        if not source.chunks:
            raise ValueError(
                f"S3VectorsBackend.ingest requires pre-chunked text for document "
                f"{source.document_id}; chunking belongs to the ingestion pipeline"
            )

        embeddings = await generate_embeddings(source.chunks)
        await store_embeddings_in_s3(
            assistant_id=kb_ref,
            document_id=source.document_id,
            chunks=source.chunks,
            embeddings=embeddings,
            metadata={"filename": source.filename, **source.metadata},
        )

    async def delete_document(self, kb_ref: str, document_id: str) -> None:
        """Delete every vector belonging to ``document_id``."""
        from apis.shared.embeddings.bedrock_embeddings import delete_vectors_for_document

        deleted = await delete_vectors_for_document(document_id)
        logger.info(
            f"S3VectorsBackend: deleted {deleted} vectors for document "
            f"{document_id} (kb {kb_ref})"
        )
