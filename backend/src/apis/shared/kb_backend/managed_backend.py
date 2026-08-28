"""The Amazon Bedrock Managed Knowledge Base backend, behind the common protocol.

Retrieval, direct ingestion and document deletion for a managed knowledge base.
Everything here is reachable only for a KB_Record that names
``retrievalEngine == "managed"``, which happens exactly once per knowledge base,
at promotion.

Scores need no conversion — and must not get one
------------------------------------------------
``Retrieve`` returns ``score`` as **relevance**: higher is more relevant, which is
already the protocol's canonical direction. So unlike
:mod:`~apis.shared.kb_backend.s3vectors_backend`, this adapter passes the score
through untouched. Applying the legacy adapter's ``relevance_from_distance``
negation here would invert the ranking, and an inverted ranking raises nothing,
logs nothing and alarms nothing: retrieval keeps returning five chunks and the
answers quietly get worse. The guard is
``tests/property/test_pbt_kb_score_direction.py``.

``managedSearchConfiguration``, never ``vectorSearchConfiguration``
------------------------------------------------------------------
Requirement 11.1. ``vectorSearchConfiguration`` is a real member of
``KnowledgeBaseRetrievalConfiguration`` in the service model, so it passes
client-side validation and then fails at the service with "not supported for
managed knowledge bases". Every retrieval, including the canary the ingestion
consumer runs, would fail together — loudly, but only after deploy.

Reranking is ``MANAGED``, not ``NONE`` (Requirement 11.2). It measurably separates
scores (0.89/0.38/0.25/0.21/0.19 versus a nearly flat 1.00/0.84/0.78/0.77/0.77
without it), and that separation is what makes the 2,000-character context cap
defensible: with a flat distribution the cap truncates chunks that were barely
distinguishable from the best one.

Hybrid search is not configured, and no attempt is made to (Requirement 11.3).
There is no toggle; it is simply how managed search works.

Document identity: one id, no chunk keys
----------------------------------------
``customDocumentIdentifier`` is the platform ``document_id`` (Requirement 9.4).
That 1:1 mapping is what lets the status filter above the seam join on a
``document_id`` per chunk, and it retires the whole ``{doc_id}#{chunk_index}``
scheme on this path — including ``delete_vector_tail`` and the chunk-shrinkage
stash (Requirement 9.6). Deletion is by document id, so there is no tail to
shrink and nothing to stash.

Two hard limits, both server-enforced
-------------------------------------
* **10 documents per call.** The packaged service model's ``KnowledgeBaseDocuments``
  list carries ``max: 10``, and the same for ``DocumentIdentifiers``. AWS's user
  guide claims 25; that claim does not apply to managed knowledge bases and was
  disproven server-side. A batch of 11 fails the whole call, so the batch size is
  a constant here rather than a caller's choice.
* **10 concurrent document operations per account.** Ingests and deletes share
  that budget, so both go through one semaphore rather than each keeping its own.

``StartIngestionJob`` is never called (Requirement 9.2): it is 0.1 RPS
account-wide and not adjustable, which for a bulk upload means one document every
ten seconds for the entire account.

Import boundary
---------------
Module-level imports are stdlib plus this package's own stdlib-only modules;
``boto3`` is imported inside the client factories. See
``tests/architecture/test_kb_backend_boundary.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import weakref
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from apis.shared.kb_backend.protocol import DEFAULT_TOP_K, Chunk, DocumentSource

logger = logging.getLogger(__name__)

#: Requirement 9.3. Server-enforced; the model's list shape has ``max: 10``.
MAX_DOCUMENTS_PER_CALL = 10

#: Requirement 9.5. Ingest and delete operations share one account-wide budget of
#: 10 concurrent operations, so one semaphore covers both.
MAX_CONCURRENT_DOCUMENT_OPERATIONS = 10

#: Requirement 11.2. ``MANAGED`` uses the service's reranker; ``NONE`` disables
#: reranking and flattens the score distribution.
RERANKING_MODEL_TYPE = "MANAGED"

#: The connector all of this platform's managed documents arrive through.
CONTENT_DATA_SOURCE_TYPE = "CUSTOM"

#: Requirement 11.5. Isolation-critical filters are restricted to exact-match
#: operators. ``startsWith`` and ``stringContains`` are prefix/substring matches:
#: a filter written to isolate ``ast-1`` would also admit ``ast-10``, and the
#: over-match is invisible because the extra results look like ordinary hits.
ISOLATION_SAFE_FILTER_OPERATORS = frozenset({"equals", "in"})

#: Where ``customDocumentIdentifier`` surfaces on a retrieval result, in the order
#: tried. ``location.customDocumentLocation.id`` is the authoritative one for a
#: CUSTOM connector; the metadata key is a documented mirror of it.
CUSTOM_IDENTIFIER_METADATA_KEY = "x-amz-bedrock-kb-custom-document-identifier"


class ManagedKbError(RuntimeError):
    """A managed knowledge base operation could not be performed."""


class ManagedKbNotProvisioned(ManagedKbError):
    """The KB_Record carries no AWS identifiers yet.

    Raised rather than provisioning inline: provisioning takes 47–124 s and this
    may be a retrieval on a user's turn. The caller decides whether to wait.
    """


class UnsafeFilterOperator(ManagedKbError):
    """A filter used an operator that cannot be trusted for isolation.

    Raised rather than silently downgraded to ``equals``, which would change the
    caller's meaning, or passed through, which would widen the boundary.
    """


# ── Clients ──────────────────────────────────────────────────────────────────
def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"


def bedrock_agent_runtime_client():
    """The data-plane client (``Retrieve``). Imported lazily, deliberately."""
    import boto3

    return boto3.client("bedrock-agent-runtime", region_name=_region())


def bedrock_agent_client():
    """The control-plane client (ingest/delete documents). Imported lazily."""
    import boto3

    return boto3.client("bedrock-agent", region_name=_region())


# ── Concurrency bound ────────────────────────────────────────────────────────
#
# Keyed by event loop rather than module-global, because an ``asyncio.Semaphore``
# is bound to the loop it is first awaited on: a single module-level instance
# would break the second test (or the second worker) to use a fresh loop. A weak
# key means a finished loop's semaphore is collected with it.
_SEMAPHORES: "weakref.WeakKeyDictionary[Any, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def document_operation_semaphore() -> asyncio.Semaphore:
    """The shared bound on concurrent ingest/delete operations (Requirement 9.5)."""
    loop = asyncio.get_running_loop()
    semaphore = _SEMAPHORES.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOCUMENT_OPERATIONS)
        _SEMAPHORES[loop] = semaphore
    return semaphore


# ── Payload builders ─────────────────────────────────────────────────────────
def validate_isolation_filter(retrieval_filter: Optional[Mapping[str, Any]]) -> None:
    """Refuse any filter operator that is not exact-match (Requirement 11.5).

    Recurses through ``andAll`` / ``orAll`` because a compound filter is only as
    safe as its least safe leaf, and a ``stringContains`` buried three levels down
    is exactly the kind of thing that survives review.
    """
    if not retrieval_filter:
        return

    for operator, operand in retrieval_filter.items():
        if operator in ("andAll", "orAll"):
            for nested in operand or []:
                validate_isolation_filter(nested)
            continue
        if operator not in ISOLATION_SAFE_FILTER_OPERATORS:
            raise UnsafeFilterOperator(
                f"filter operator {operator!r} is not permitted: an "
                f"isolation-critical filter must use one of "
                f"{sorted(ISOLATION_SAFE_FILTER_OPERATORS)}. Prefix and substring "
                f"operators over-match silently — a filter isolating 'ast-1' also "
                f"admits 'ast-10', and the extra results are indistinguishable "
                f"from legitimate hits."
            )


def retrieval_configuration(
    top_k: int = DEFAULT_TOP_K,
    retrieval_filter: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build ``retrievalConfiguration`` for a managed knowledge base.

    ``managedSearchConfiguration`` only. There is no branch that could produce
    ``vectorSearchConfiguration``, so it cannot be reintroduced by a stray
    condition — only by editing this function, which
    ``tests/shared/test_managed_kb_backend.py`` notices.
    """
    validate_isolation_filter(retrieval_filter)

    managed: Dict[str, Any] = {
        # Requirement 3.1 parity: both backends are asked for the same number.
        "numberOfResults": top_k,
        "rerankingModelType": RERANKING_MODEL_TYPE,
    }
    if retrieval_filter:
        managed["filter"] = dict(retrieval_filter)

    # No hybrid-search key: it is not configurable and not attempted (Req 11.3).
    return {"managedSearchConfiguration": managed}


#: Bedrock caps ``DocumentMetadata.inlineAttributes`` at 50 entries (verified in the
#: packaged service model: ``{'min': 1, 'max': 50}``). Exceeding it fails the whole
#: ``IngestKnowledgeBaseDocuments`` call, so one document with chatty metadata would
#: take its entire batch of ten down with it.
MAX_INLINE_ATTRIBUTES = 50

#: Keys the platform depends on, kept in preference to caller-supplied ones when
#: truncating. ``document_id`` is load-bearing: the facade's status filter joins on
#: it, so a chunk that arrives without it cannot be matched to its document and
#: would be dropped as unverifiable.
_RESERVED_METADATA_KEYS = ("document_id", "filename")


def _inline_attributes(metadata: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Metadata as ``IN_LINE_ATTRIBUTE`` entries, string-valued and bounded.

    Only strings are emitted. Mixed attribute types are a per-key commitment on
    Bedrock's side, and the platform's metadata is loosely typed, so coercing
    everything to a string keeps a stray ``None`` or ``int`` from poisoning a key
    for every future document.

    The list is capped at :data:`MAX_INLINE_ATTRIBUTES`. ``source.metadata`` is
    caller-supplied and unbounded, so without this a caller could fail an entire
    ten-document batch with one over-decorated document. Reserved keys are emitted
    first so truncation cannot drop them — a plain ``sorted()`` would drop by
    alphabet, and ``document_id`` sorts after several plausible caller keys.
    """
    ordered: List[tuple] = []
    seen = set()

    for key in _RESERVED_METADATA_KEYS:
        if key in metadata and metadata[key] is not None:
            ordered.append((key, metadata[key]))
            seen.add(key)

    for key, value in sorted(metadata.items()):
        if key in seen or value is None:
            continue
        ordered.append((key, value))

    if len(ordered) > MAX_INLINE_ATTRIBUTES:
        dropped = len(ordered) - MAX_INLINE_ATTRIBUTES
        logger.warning(
            f"document metadata has {len(ordered)} attributes; keeping the first "
            f"{MAX_INLINE_ATTRIBUTES} and dropping {dropped} "
            f"(Bedrock's inlineAttributes limit)"
        )
        ordered = ordered[:MAX_INLINE_ATTRIBUTES]

    return [
        {"key": key, "value": {"type": "STRING", "stringValue": str(value)}}
        for key, value in ordered
    ]


def document_payload(
    source: DocumentSource,
    *,
    bucket: Optional[str] = None,
) -> Dict[str, Any]:
    """One entry of the ``documents`` array.

    ``customDocumentIdentifier`` is the platform ``document_id`` verbatim
    (Requirement 9.4) — not a derived or prefixed form. It is the join key the
    status filter needs and the handle deletion uses, so any transformation here
    would have to be reversed in two other places.

    Prefers the S3 location when the source has one: the object is already in the
    documents bucket, and Bedrock reading it directly avoids pulling the bytes
    through this process. Falls back to inline text for a source that only has
    chunks, joining them back into a document because managed ingestion does its
    own chunking and pre-chunked input would be re-chunked anyway.
    """
    identifier = {"id": source.document_id}
    custom: Dict[str, Any] = {
        "customDocumentIdentifier": identifier,
    }

    if source.s3_key:
        resolved = bucket or os.environ.get("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME")
        if not resolved:
            raise ManagedKbError(
                f"document {source.document_id} has an S3 key but no bucket: pass "
                f"bucket= or set S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME"
            )
        custom["sourceType"] = "S3_LOCATION"
        custom["s3Location"] = {"uri": f"s3://{resolved}/{source.s3_key}"}
    elif source.chunks:
        custom["sourceType"] = "IN_LINE"
        custom["inlineContent"] = {
            "type": "TEXT",
            "textContent": {"data": "\n\n".join(source.chunks)},
        }
    else:
        raise ManagedKbError(
            f"document {source.document_id} has neither an s3_key nor chunks; "
            f"there is nothing to ingest"
        )

    document: Dict[str, Any] = {
        "content": {"dataSourceType": CONTENT_DATA_SOURCE_TYPE, "custom": custom}
    }

    metadata = {"document_id": source.document_id, "filename": source.filename}
    metadata.update({k: v for k, v in source.metadata.items() if k not in metadata})
    attributes = _inline_attributes(metadata)
    if attributes:
        document["metadata"] = {
            "type": "IN_LINE_ATTRIBUTE",
            "inlineAttributes": attributes,
        }
    return document


def document_identifier(document_id: str) -> Dict[str, Any]:
    """One entry of ``documentIdentifiers`` for a delete.

    Deletion is by the platform document id, full stop. There is no chunk tail to
    enumerate and no shrinkage case to handle, because one document is one
    document (Requirement 9.6).
    """
    return {
        "dataSourceType": CONTENT_DATA_SOURCE_TYPE,
        "custom": {"id": document_id},
    }


def batched(items: Sequence[Any], size: int = MAX_DOCUMENTS_PER_CALL) -> List[List[Any]]:
    """Split ``items`` into batches of at most ``size``.

    ``size`` is validated against the server limit rather than trusted. A caller
    passing 25 — the number AWS's user guide gives, which does not apply to managed
    knowledge bases — would otherwise produce a request that fails as a whole,
    losing the other 24 documents along with the 25th.
    """
    if size < 1:
        raise ValueError("batch size must be at least 1")
    if size > MAX_DOCUMENTS_PER_CALL:
        raise ValueError(
            f"batch size {size} exceeds the server-enforced maximum of "
            f"{MAX_DOCUMENTS_PER_CALL} documents per call. AWS's user guide claims "
            f"25; that does not apply to managed knowledge bases and an "
            f"11-document call fails entirely."
        )
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


# ── The backend ──────────────────────────────────────────────────────────────
class ManagedKbBackend:
    """Retrieval and direct ingestion against a Managed Knowledge Base.

    ``kb_ref`` is the ``App_KB_Id`` (equal to the ``assistant_id`` in this phase),
    never an AWS ``knowledgeBaseId``. The AWS identifiers are resolved internally
    from the KB_Record on each operation, because a dormancy/rehydration cycle
    replaces them and a caller holding one would keep querying a knowledge base
    that no longer exists.

    Clients are injectable so tests can stub them; they are created lazily so
    constructing this class costs nothing at import time.
    """

    def __init__(
        self,
        *,
        runtime_client=None,
        agent_client=None,
        locator=None,
        bucket: Optional[str] = None,
    ) -> None:
        self._runtime_client = runtime_client
        self._agent_client = agent_client
        self._locator = locator
        self._bucket = bucket

    # ── plumbing ────────────────────────────────────────────────────────────
    def _runtime(self):
        if self._runtime_client is None:
            self._runtime_client = bedrock_agent_runtime_client()
        return self._runtime_client

    def _agent(self):
        if self._agent_client is None:
            self._agent_client = bedrock_agent_client()
        return self._agent_client

    def _locate(self, kb_ref: str) -> Tuple[str, str]:
        """Resolve ``kb_ref`` to ``(awsKbId, awsDataSourceId)``."""
        if self._locator is not None:
            located = self._locator(kb_ref)
        else:
            from apis.shared.kb_backend.records import get_kb_record

            # App_KB_Id == assistant_id in this phase, so one value serves both.
            item = get_kb_record(kb_ref, kb_ref)
            located = (
                (item.get("awsKbId"), item.get("awsDataSourceId")) if item else (None, None)
            )

        aws_kb_id, aws_data_source_id = located
        if not aws_kb_id:
            raise ManagedKbNotProvisioned(
                f"knowledge base {kb_ref} has no awsKbId: it is not provisioned yet"
            )
        return aws_kb_id, aws_data_source_id

    async def _locate_async(self, kb_ref: str) -> Tuple[str, str]:
        return await asyncio.to_thread(self._locate, kb_ref)

    # ── retrieval ───────────────────────────────────────────────────────────
    async def search(
        self,
        kb_ref: str,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        retrieval_filter: Optional[Mapping[str, Any]] = None,
    ) -> List[Chunk]:
        """Retrieve up to ``top_k`` chunks, best first.

        Ordering is the service's. ``Retrieve`` returns results ranked best-first
        and this returns them in that order, with the reported ``score`` as the
        canonical ``relevance`` — unconverted, because the directions already
        agree.

        The synchronous ``retrieve`` call runs off the event loop (Requirement
        20.7): it was measured at 662–695 ms p50, which is long enough to matter
        to every other coroutine sharing the loop.
        """
        aws_kb_id, _ = await self._locate_async(kb_ref)
        client = self._runtime()

        payload = {
            "knowledgeBaseId": aws_kb_id,
            "retrievalQuery": {"text": query},
            "retrievalConfiguration": retrieval_configuration(top_k, retrieval_filter),
        }
        response = await asyncio.to_thread(lambda: client.retrieve(**payload))
        return [self._to_chunk(result) for result in response.get("retrievalResults", [])]

    @staticmethod
    def _to_chunk(result: Mapping[str, Any]) -> Chunk:
        """Adapt one ``Retrieve`` result. **No score conversion.**

        ``score`` is relevance already: higher is better, which is the protocol's
        direction. A missing score stays ``None`` rather than becoming ``0.0``,
        for the same reason as in the legacy adapter — a fabricated ``0.0`` would
        be indistinguishable from a real score, and on this backend it would rank
        the chunk *last* while on the other it would rank first.
        """
        metadata = dict(result.get("metadata") or {})
        text = (result.get("content") or {}).get("text", "")
        document_id = ManagedKbBackend._document_id(result, metadata)

        # The status filter and the citation formatter above the seam both read
        # `metadata["text"]`, which is where the legacy path put it.
        metadata.setdefault("text", text)
        metadata.setdefault("document_id", document_id)

        return Chunk(
            text=text,
            relevance=result.get("score"),
            document_id=document_id,
            metadata=metadata,
            key=document_id,
        )

    @staticmethod
    def _document_id(result: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
        """Recover the platform ``document_id`` from a retrieval result.

        Deliberately does **not** fall back to the result's own ``documentId``:
        that is a service-assigned handle for ``GetDocumentContent``, not the
        platform id, and returning it would produce a chunk whose ``document_id``
        looks plausible, joins against no ``DOC#`` record, and is dropped by the
        fail-closed status filter — a disappearing-results bug two layers away
        from its cause.
        """
        location = result.get("location") or {}
        custom = location.get("customDocumentLocation") or {}
        for candidate in (
            custom.get("id"),
            metadata.get(CUSTOM_IDENTIFIER_METADATA_KEY),
            metadata.get("document_id"),
        ):
            if candidate:
                return str(candidate)
        return ""

    # ── ingestion ───────────────────────────────────────────────────────────
    async def ingest(self, kb_ref: str, source: DocumentSource) -> None:
        """Index one document."""
        await self.ingest_documents(kb_ref, [source])

    async def ingest_documents(
        self,
        kb_ref: str,
        sources: Iterable[DocumentSource],
        *,
        batch_size: int = MAX_DOCUMENTS_PER_CALL,
    ) -> None:
        """Index documents with ``IngestKnowledgeBaseDocuments``.

        Batched at 10 and concurrency-bounded at 10 (Requirements 9.3, 9.5).
        ``StartIngestionJob`` is never involved (Requirement 9.2).

        No ``clientToken`` is sent, on purpose. Idempotency here comes from
        ``customDocumentIdentifier`` being 1:1 with the platform document id, so
        re-ingesting a document replaces it. A token derived from the document ids
        would look like extra safety and instead swallow a legitimate re-upload of
        the same document — silently, since a deduplicated request returns
        success.
        """
        documents = list(sources)
        if not documents:
            return

        aws_kb_id, aws_data_source_id = await self._locate_async(kb_ref)
        if not aws_data_source_id:
            raise ManagedKbNotProvisioned(
                f"knowledge base {kb_ref} has no awsDataSourceId: its CUSTOM "
                f"connector is not created yet"
            )

        client = self._agent()
        payloads = [document_payload(source, bucket=self._bucket) for source in documents]

        await self._run_bounded(
            [
                {
                    "knowledgeBaseId": aws_kb_id,
                    "dataSourceId": aws_data_source_id,
                    "documents": batch,
                }
                for batch in batched(payloads, batch_size)
            ],
            client.ingest_knowledge_base_documents,
            what="IngestKnowledgeBaseDocuments",
        )

    # ── deletion ────────────────────────────────────────────────────────────
    async def delete_document(self, kb_ref: str, document_id: str) -> None:
        """Remove one document by its platform id."""
        await self.delete_documents(kb_ref, [document_id])

    async def delete_documents(
        self,
        kb_ref: str,
        document_ids: Iterable[str],
        *,
        batch_size: int = MAX_DOCUMENTS_PER_CALL,
    ) -> None:
        """Remove documents with ``DeleteKnowledgeBaseDocuments``.

        Same batch limit and same shared concurrency budget as ingestion: the
        account's 10-concurrent-operation limit counts both together, so deletes
        issued alongside ingests must not each get their own allowance.
        """
        ids = [document_id for document_id in document_ids if document_id]
        if not ids:
            return

        aws_kb_id, aws_data_source_id = await self._locate_async(kb_ref)
        if not aws_data_source_id:
            raise ManagedKbNotProvisioned(
                f"knowledge base {kb_ref} has no awsDataSourceId; nothing to delete from"
            )

        client = self._agent()
        await self._run_bounded(
            [
                {
                    "knowledgeBaseId": aws_kb_id,
                    "dataSourceId": aws_data_source_id,
                    "documentIdentifiers": batch,
                }
                for batch in batched([document_identifier(i) for i in ids], batch_size)
            ],
            client.delete_knowledge_base_documents,
            what="DeleteKnowledgeBaseDocuments",
        )

    @staticmethod
    async def _run_bounded(payloads: Sequence[Mapping[str, Any]], operation, *, what: str) -> None:
        """Issue each payload off the event loop, at most 10 in flight.

        The semaphore is acquired *around* the ``to_thread`` call so the bound
        counts operations in flight at AWS, not coroutines created here — which is
        the number the account limit is expressed in.
        """
        semaphore = document_operation_semaphore()

        async def _one(payload: Mapping[str, Any]) -> None:
            async with semaphore:
                await asyncio.to_thread(lambda: operation(**payload))

        results = await asyncio.gather(
            *(_one(payload) for payload in payloads), return_exceptions=True
        )
        failures = [outcome for outcome in results if isinstance(outcome, BaseException)]
        if failures:
            logger.error(f"{what}: {len(failures)}/{len(payloads)} batches failed")
            raise failures[0]


__all__ = [
    "CONTENT_DATA_SOURCE_TYPE",
    "ISOLATION_SAFE_FILTER_OPERATORS",
    "MAX_CONCURRENT_DOCUMENT_OPERATIONS",
    "MAX_DOCUMENTS_PER_CALL",
    "RERANKING_MODEL_TYPE",
    "ManagedKbBackend",
    "ManagedKbError",
    "ManagedKbNotProvisioned",
    "UnsafeFilterOperator",
    "batched",
    "document_identifier",
    "document_operation_semaphore",
    "document_payload",
    "retrieval_configuration",
    "validate_isolation_filter",
]
