"""RAG service for assistant knowledge base search and prompt augmentation

This module is the **facade** over the knowledge base seam. It resolves which
backend serves an assistant's knowledge base, delegates the search, and then
applies the properties that must hold identically on every backend. It contains
no retrieval logic of its own: what an S3 Vectors response looks like now lives
in ``apis.shared.kb_backend.s3vectors_backend``.

What lives here, and why here
-----------------------------
Four rules sit above the seam rather than inside either adapter, because a rule
implemented twice is a rule that will eventually differ (Requirement 3):

* **The access check.** No backend is contacted until the invoking user's grant
  has been resolved (Requirement 25.1). It is above the seam because the answer
  does not depend on the engine, and because Bedrock's own isolation features are
  not trusted to be the authority — see ``kb_access``.
* **The document-status filter.** Dropped chunks whose parent document is not
  ``complete``. Kept on both backends during parity even though managed
  ingestion makes it largely redundant — removing it in the same change that
  swaps the engine would make any difference in results unattributable.
* **``top_k`` narrowing.** Applied *after* the status filter, which is the order
  the legacy path has always used: filter-then-slice, so an incomplete document
  cannot silently shrink a five-chunk answer.
* **The 2,000-character context cap.** ``augment_prompt_with_context``'s
  default. Held constant deliberately: the evaluation measured no correctness
  change between 2,000 and 20,000 characters, so raising it here would add a
  variable to a change whose whole purpose is to hold every variable but one.

The dual-read pilot
-------------------
Also above the seam, and for the same reason: comparing two backends is not a
thing either backend can do. The facade starts the observational managed read
before awaiting legacy and detaches the comparison afterwards, so a piloted turn
waits exactly as long as an unpiloted one (Requirement 18.5). Legacy is always
what is served. See ``kb_backend.dual_read``.

Score direction
---------------The seam speaks **relevance** (higher is better). This facade still emits a
``distance`` key (lower is better), derived by exact negation, because
``app_api/assistants/routes.py`` puts that value in an HTTP response body that a
client already reads. The rename stops at the seam; no caller has to change.
"""

import logging
import os
import time
from typing import Any, Dict, List, Optional, Set

import boto3

from apis.shared.assistants.kb_access import KbAccess
from apis.shared.kb_backend.dual_read import schedule_observation, start_managed_read
from apis.shared.kb_backend.metrics import (
    METRIC_ACCESS_DENIED,
    METRIC_STATUS_FILTER_FAIL_CLOSED,
    emit_count,
)
from apis.shared.kb_backend.protocol import DEFAULT_TOP_K, Chunk, distance_from_relevance
from apis.shared.kb_backend.query_guard import clamp_query
from apis.shared.kb_backend.resolver import load_record, resolve_backend

logger = logging.getLogger(__name__)

#: Parity contract (Requirement 3.2): the cap is 2,000 characters on every
#: backend, unchanged from the value the legacy path has always used. Named so
#: that a change to it is a visible change to a constant rather than an edit to a
#: default argument.
MAX_CONTEXT_CHARS = 2000


async def search_assistant_knowledgebase_with_formatting(
    assistant_id: str,
    query: str,
    top_k: int = DEFAULT_TOP_K,
    *,
    access: Optional[KbAccess],
) -> List[Dict[str, Any]]:
    """
    Search assistant knowledge base and return formatted results

    Resolves the knowledge base's backend, delegates the search across the seam,
    then applies the parity rules that must hold on every backend: the document
    status filter, and ``top_k`` narrowing after it.

    Args:
        assistant_id: Assistant identifier to filter vectors
        query: User query text
        top_k: Number of top results to return (default: 5)
        access: The invoking user's resolved grant, or ``None`` if they have none.
            Required and keyword-only (Requirement 25.1): a caller that forgets it
            fails loudly at the call site, while a caller that genuinely has no
            grant passes ``None`` and gets nothing. Build one with
            ``kb_access.granted`` if the permission is already in hand, or
            ``kb_access.resolve_kb_access`` if it is not.

    Returns:
        List of dictionaries containing:
        - text: Chunk text content
        - distance: Similarity distance (lower = more similar)
        - metadata: Original metadata from vector store
        - key: Vector key/ID

        Empty when the caller has no grant — no backend is contacted at all.
    """
    # Authorization first, before the backend resolution, the query clamp, and
    # any AWS call (Requirement 25.1). Ordering is the requirement: a check that
    # runs after retrieval has already read the corpus is an audit log, not an
    # access control.
    if access is None or not access.may_read:
        logger.error(
            f"refusing knowledge base retrieval for assistant {assistant_id}: "
            f"no resolved access grant"
        )
        emit_count(METRIC_ACCESS_DENIED, dimensions={"reason": "no_grant"})
        return []

    if access.assistant_id != assistant_id:
        # A grant for a different assistant is not a grant for this one. This is
        # the shape a copy-paste bug takes when a route resolves permission for
        # one id and retrieves with another, and while the 1:1 binding holds it is
        # the only way the two could disagree.
        logger.error(
            f"refusing knowledge base retrieval: grant is for assistant "
            f"{access.assistant_id}, not {assistant_id}"
        )
        emit_count(METRIC_ACCESS_DENIED, dimensions={"reason": "grant_mismatch"})
        return []

    managed_task = None
    try:
        # One record read serves both questions: which backend to use, and whether
        # this knowledge base is in the dual-read pilot. Reading it here rather
        # than letting the resolver read it internally is what keeps the pilot
        # from costing an extra DynamoDB round trip on every turn.
        record = load_record(assistant_id)
        backend = resolve_backend(assistant_id, record=record)

        # Clamp before dispatch, so both backends receive an identically-shaped
        # query (Requirement 4.2). Managed KB rejects anything over 10,000
        # characters outright and the quota is not adjustable, so clamping only
        # the managed path would make the two backends answer different
        # questions and invalidate the dual-read comparison.
        query, _ = clamp_query(query)

        # Start the observational read *before* awaiting legacy (Requirement
        # 18.5). Nothing is awaited here, so a piloted turn does the same waiting
        # as an unpiloted one; managed Retrieve measured 662–695 ms p50 against
        # legacy's 257 ms, so awaiting both would nearly triple this leg.
        # ``None`` whenever there is no comparison to make.
        managed_task = start_managed_read(record, assistant_id, query, top_k)

        started = time.perf_counter()
        chunks = await backend.search(assistant_id, query, top_k)
        legacy_ms = (time.perf_counter() - started) * 1000.0

        # Detach the comparison. Legacy is what gets served either way — including
        # when it is empty, which is a finding rather than a reason to reach for
        # the other engine's answer (Requirement 18.2).
        schedule_observation(assistant_id, query, top_k, list(chunks), legacy_ms, managed_task)
        managed_task = None

        if not chunks:
            logger.info(f"No vectors found for assistant {assistant_id} with query: {query[:50]}...")
            return []

        # Filter out chunks from documents that are not in "complete" status
        chunks = _filter_chunks_by_document_status(chunks, assistant_id)

        # Format results - return document_id for on-demand download URL generation
        formatted_results = []
        for chunk in chunks[:top_k]:
            formatted_results.append(
                {
                    "text": chunk.text,
                    # Derived from relevance by exact negation, so the value a
                    # caller reads is the one it has always read.
                    "distance": distance_from_relevance(chunk.relevance),
                    "metadata": chunk.metadata,
                    "key": chunk.key,
                }
            )

        logger.info(f"Found {len(formatted_results)} relevant chunks for assistant {assistant_id}")
        return formatted_results

    except Exception as e:
        logger.error(f"Error searching knowledge base for assistant {assistant_id}: {e}", exc_info=True)
        if managed_task is not None:
            # The legacy search raised before the comparison was detached, so
            # nothing will ever await this task. Left alone it would run to
            # completion, pay for a Retrieve, and be reported as a task whose
            # exception was never retrieved.
            managed_task.cancel()
        # Return empty list on error (graceful degradation)
        return []


def _filter_chunks_by_document_status(chunks: List[Chunk], assistant_id: str) -> List[Chunk]:
    """
    Apply the document status filter to protocol chunks, on any backend.

    Delegates to :func:`_filter_vectors_by_document_status` rather than
    reimplementing the lookup, so both backends share one set of DynamoDB
    semantics — including its fallback behaviour, which task group 6 changes in
    exactly one place.

    Each chunk is presented to the filter as a minimal view carrying only what
    the filter reads (``metadata.document_id``) plus its index, and survivors are
    mapped back by that index. Order and duplicates are preserved.

    Args:
        chunks: Chunks returned by a backend, in backend ranking order
        assistant_id: Assistant identifier for DynamoDB key construction

    Returns:
        The subset of chunks whose parent document is 'complete'
    """
    views = [{"metadata": chunk.metadata, "_chunk_index": index} for index, chunk in enumerate(chunks)]
    surviving = _filter_vectors_by_document_status(views, assistant_id)
    return [chunks[view["_chunk_index"]] for view in surviving]


def _filter_vectors_by_document_status(vectors: List[Dict[str, Any]], assistant_id: str) -> List[Dict[str, Any]]:
    """
    Filter vector results to only include chunks from documents with status='complete'.

    Extracts unique document_ids from vector metadata, looks up each document's status
    in DynamoDB, and removes chunks from documents that are not 'complete' or don't exist.

    Fails CLOSED (Requirement 5): if status cannot be confirmed — no table
    configured, or the lookup errors — every chunk is dropped and an empty list is
    returned. This deliberately supersedes `reliable-document-deletion`
    Requirement 3.4, which specified the opposite. The reasoning changed because
    the fail-open path was measured: 936 retrievals in a trailing 30-day window had
    chunks dropped by this filter, so the documents it guards against are real, and
    serving a user content they believe they deleted is worse than serving nothing.
    A per-document lookup failure still skips only that document.

    Args:
        vectors: List of vector results from S3 Vectors search
        assistant_id: Assistant identifier for DynamoDB key construction

    Returns:
        Filtered list of vectors from complete documents only
    """
    # Extract unique document_ids from vector metadata
    doc_ids: Set[str] = set()
    for vector in vectors:
        doc_id = vector.get("metadata", {}).get("document_id")
        if doc_id:
            doc_ids.add(doc_id)

    if not doc_ids:
        return vectors

    # Look up document status in DynamoDB
    valid_doc_ids: Set[str] = set()
    try:
        table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
        if table_name:
            region = os.environ.get("AWS_REGION", "us-west-2")
            dynamodb = boto3.resource("dynamodb", region_name=region)
            table = dynamodb.Table(table_name)
            for doc_id in doc_ids:
                try:
                    response = table.get_item(
                        Key={"PK": f"AST#{assistant_id}", "SK": f"DOC#{doc_id}"}
                    )
                    item = response.get("Item")
                    if item and item.get("status") == "complete":
                        valid_doc_ids.add(doc_id)
                    else:
                        logger.info(
                            f"Filtering out doc {doc_id}: "
                            f"status={item.get('status') if item else 'NOT_FOUND'}"
                        )
                except Exception as e:
                    logger.warning(f"Failed to look up document {doc_id}: {e}")
                    # Skip individual lookup failures
        else:
            # FAIL CLOSED (Requirement 5.2). Previously this returned everything
            # unfiltered. Without a table there is no way to confirm that a
            # document is still `complete`, and the chunks in question may belong
            # to documents a user has deleted. Serving unverifiable content is a
            # worse outcome than serving none: the user sees material they believe
            # they removed, and nothing in the response signals that the check was
            # skipped.
            logger.error(
                "DYNAMODB_ASSISTANTS_TABLE_NAME not configured; dropping all "
                "chunks because document status cannot be confirmed"
            )
            emit_count(METRIC_STATUS_FILTER_FAIL_CLOSED)
            return []
    except Exception as e:
        # FAIL CLOSED (Requirement 5.1). Same reasoning as above. Logged at ERROR,
        # not WARNING: an empty result from this path is a degradation, and it must
        # be distinguishable from the ordinary "corpus had no match" case, which is
        # logged at INFO below.
        logger.error(
            f"Document status lookup failed; dropping all chunks because status "
            f"cannot be confirmed: {e}",
            exc_info=True,
        )
        emit_count(METRIC_STATUS_FILTER_FAIL_CLOSED)
        return []

    # Filter vectors to only include chunks from valid documents
    filtered = [v for v in vectors if v.get("metadata", {}).get("document_id") in valid_doc_ids]
    if len(filtered) < len(vectors):
        logger.info(
            f"Document status filter: {len(vectors)} vectors → {len(filtered)} "
            f"(removed {len(vectors) - len(filtered)} from non-complete docs)"
        )
    return filtered


def augment_prompt_with_context(user_message: str, context_chunks: List[Dict[str, Any]], max_context_length: int = MAX_CONTEXT_CHARS) -> str:
    """
    Augment user message with retrieved context chunks

    The context is prepended to the user message with clear delimiters.
    This allows the LLM to use the retrieved knowledge when generating responses.

    Applies on both backends: the cap lives here, above the seam, so neither
    adapter can widen it independently.

    Args:
        user_message: Original user message
        context_chunks: List of context chunks from vector search
        max_context_length: Maximum total length of context to include (chars)

    Returns:
        Augmented message string with context prepended
    """
    if not context_chunks:
        # No context available, return original message
        return user_message

    # Build context section
    context_parts = []
    total_length = 0

    for i, chunk in enumerate(context_chunks, 1):
        chunk_text = chunk.get("text", "").strip()
        if not chunk_text:
            continue

        # Check if adding this chunk would exceed max length
        chunk_with_header = f"[Context {i}]\n{chunk_text}\n"
        if total_length + len(chunk_with_header) > max_context_length:
            # Truncate this chunk if needed
            remaining = max_context_length - total_length - len(f"[Context {i}]\n\n")
            if remaining > 0:
                chunk_text = chunk_text[:remaining] + "..."
                context_parts.append(f"[Context {i}]\n{chunk_text}\n")
            break

        context_parts.append(chunk_with_header)
        total_length += len(chunk_with_header)

    if not context_parts:
        # No valid context chunks, return original message
        return user_message

    # Combine context and user message
    context_section = "\n".join(context_parts)
    augmented_message = f"""The following context is retrieved from the assistant's knowledge base. Use this information to answer the user's question accurately and comprehensively.

{context_section}
---
User Question: {user_message}"""

    return augmented_message
