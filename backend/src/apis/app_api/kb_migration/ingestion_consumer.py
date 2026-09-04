"""Ingestion consumer for managed knowledge bases.

Triggered by an EventBridge ``ObjectCreated`` event on the RAG documents bucket. Its
whole job is to decide whether a newly uploaded document belongs to a managed
knowledge base and, if so, ingest it directly.

Routing exclusivity is the point (Requirements 10.3-10.5)
---------------------------------------------------------
The legacy pipeline is triggered by its **own**, pre-existing S3 notification on the
same bucket. That notification was deliberately left in place, so for a legacy
document this function's correct behaviour is to **do nothing at all** — the other
Lambda has already got it. Acting here as well would index the same bytes twice: two
sets of vectors, doubled ingestion cost, and duplicate chunks competing in one
result list.

So the routing table is asymmetric, and that asymmetry is intentional:

===============  =========================================================
Engine           This function
===============  =========================================================
legacy (absent)  returns immediately, ingesting nothing
managed          ingests directly and drives ``DOC#`` to a terminal state
===============  =========================================================

The one exception is a deliberate migration or dual-read pilot, which the
Migration_Worker drives and which never routes through an upload event.

Indexed is not retrievable
--------------------------
Bedrock reports a document ``INDEXED`` up to a second before it can actually be
retrieved — measured at 0.75-1.03 s. Marking a document ``complete`` on ``INDEXED``
alone produces the worst kind of bug report: the UI says the upload worked, the user
asks a question straight away, and the answer does not mention their document. So
this polls until a retrieval really returns the document, and records ``indexedAt``
and ``retrievableAt`` separately so the gap stays measurable instead of becoming
folklore.

Import boundary
---------------
Raw DynamoDB table access rather than importing ``apis.shared.assistants``, whose
``__init__`` pulls in the embeddings stack at module scope. Keeping this Lambda's
image small is a deliberate constraint — the same reason
``apis/app_api/kb_sync/records.py`` is written this way. Module-level imports are
stdlib only; everything heavy is function-local.

No in-process orchestration
---------------------------
No ``asyncio.ensure_future`` fan-out (Requirement 10.8). One invocation drives its
documents to terminal or fails and lets the event source redeliver. A background
task in a Lambda is killed when the handler returns, which turns a reported success
into a silently half-finished ingestion.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote_plus

logger = logging.getLogger()
logger.setLevel(logging.INFO)

#: Terminal document states, taken from ``apis/app_api/documents/models.py``'s
#: ``DocumentStatus`` rather than invented: the facade's status filter serves only
#: ``complete``, so any drift here would silently make documents unretrievable.
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"

#: How long to wait for a document to become genuinely retrievable after Bedrock
#: reports it INDEXED. The observed gap is 0.75-1.03 s; the margin is wide because
#: the cost of waiting is a few seconds of Lambda time and the cost of not waiting
#: is telling a user their upload worked when it is not yet usable.
RETRIEVABLE_POLL_TIMEOUT_SECONDS = 30.0
RETRIEVABLE_POLL_INTERVAL_SECONDS = 0.5

#: How long ONE invocation waits for Bedrock to finish indexing.
#:
#: This has to cover the whole indexing time, because redelivery cannot. Lambda's
#: asynchronous retry is capped at **2** attempts — a hard service limit, not a
#: setting we chose — so an event gets 1 + 2 tries spread over a few minutes and
#: then dead-letters. Raising a retry policy on the EventBridge target does not
#: change that: for a Lambda target EventBridge hands the event off and the
#: function's own async retry config takes over.
#:
#: So the budget is sized from measurement, not convenience. The §5.1 benchmark
#: measured PDF ingestion at 37–264 s, and a 1.5 MB PDF in dev reached INDEXED
#: 5 m 30 s after upload — image-heavy files run longer because the vision model
#: runs per page. 10 minutes covers that with headroom and still leaves 5 minutes
#: under the Lambda's 15-minute timeout, so a slow-but-succeeding document is never
#: killed mid-wait.
#:
#: The cost of waiting is real but small: this Lambda is 1024 MB and handles one
#: user upload at a time, so a 6-minute wait is a fraction of a cent. The cost of
#: NOT waiting was a permanently stuck document with a fully retrievable copy in
#: the knowledge base and no writer left to reconcile it.
#:
#: ⚠️ Keep the sum of this and RETRIEVABLE_POLL_TIMEOUT_SECONDS below the Lambda's
#: timeout. `tests/supply_chain/test_kb_migration_env_contract.py` asserts it
#: against the value in the CDK construct.
INDEXED_POLL_TIMEOUT_SECONDS = 600.0

#: 5 s rather than sub-second: over a 10-minute budget this is ~120 control-plane
#: calls instead of ~1,200, and indexing progress is measured in tens of seconds,
#: so a finer interval buys nothing.
INDEXED_POLL_INTERVAL_SECONDS = 5.0

#: Bounded retries on the record update. The event source already redelivers, so
#: this only covers a transient DynamoDB failure inside one invocation; unbounded
#: retries would burn the Lambda timeout and lose the DLQ signal.
MAX_RECORD_UPDATE_ATTEMPTS = 3


class IngestionRoutingError(Exception):
    """The event could not be routed, or the document could not be finished."""


def _table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["DYNAMODB_ASSISTANTS_TABLE_NAME"])


def _now_iso() -> str:
    from apis.shared.timestamps import utc_now_iso

    return utc_now_iso()


def parse_object_key(key: str) -> Tuple[str, str, str]:
    """Split ``assistants/{assistant_id}/documents/{document_id}/{filename}``.

    The layout is the existing one and this feature does not change it: migration is
    a re-ingest of bytes already in place, never a re-upload. Parsing the key rather
    than trusting an event field keeps routing independent of which producer
    delivered the notification.
    """
    parts = unquote_plus(key).split("/")
    if len(parts) < 5 or parts[0] != "assistants" or parts[2] != "documents":
        raise IngestionRoutingError(
            f"object key {key!r} is not an assistant document path; expected "
            f"assistants/{{assistant_id}}/documents/{{document_id}}/{{filename}}"
        )
    return parts[1], parts[3], "/".join(parts[4:])


def extract_records(event: Dict[str, Any]) -> List[Dict[str, str]]:
    """Normalize EventBridge and raw-S3 notification shapes into one list.

    Both are accepted because the bucket carries both producers — EventBridge feeds
    this function, a direct notification feeds the legacy pipeline — so a wiring
    change cannot silently stop ingestion.
    """
    detail = event.get("detail")
    if isinstance(detail, dict) and detail.get("object"):
        return [
            {
                "bucket": (detail.get("bucket") or {}).get("name", ""),
                "key": (detail.get("object") or {}).get("key", ""),
            }
        ]

    out: List[Dict[str, str]] = []
    for record in event.get("Records") or []:
        s3 = record.get("s3", {})
        out.append(
            {
                "bucket": (s3.get("bucket") or {}).get("name", ""),
                "key": (s3.get("object") or {}).get("key", ""),
            }
        )
    return out


def resolve_engine_for(assistant_id: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """The engine serving this assistant's knowledge base, plus its record.

    Delegates to ``records.resolve_engine`` so "absence means legacy" has exactly one
    implementation. A missing record is the overwhelmingly common case today and
    resolves to legacy, which is why this cannot treat it as an error.
    """
    from apis.shared.kb_backend.records import get_kb_record, resolve_engine

    record = get_kb_record(assistant_id, assistant_id)
    return resolve_engine(record), record


def set_document_terminal(
    assistant_id: str,
    document_id: str,
    status: str,
    indexed_at: Optional[str] = None,
    retrievable_at: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Drive the ``DOC#`` record to a terminal state, with bounded retries.

    ``indexedAt`` and ``retrievableAt`` are stored separately on purpose: collapsing
    them would erase the only evidence of the INDEXED-to-retrievable gap, which is
    what makes "my upload finished but the assistant cannot see it" diagnosable
    rather than mysterious.
    """
    from botocore.exceptions import ClientError

    sets = ["#status = :status", "updatedAt = :now"]
    values: Dict[str, Any] = {":status": status, ":now": _now_iso()}

    if indexed_at:
        sets.append("indexedAt = :indexed")
        values[":indexed"] = indexed_at
    if retrievable_at:
        sets.append("retrievableAt = :retrievable")
        values[":retrievable"] = retrievable_at
    if error:
        sets.append("ingestionError = :err")
        values[":err"] = error

    expression = f"SET {', '.join(sets)}"
    last: Optional[Exception] = None

    for attempt in range(1, MAX_RECORD_UPDATE_ATTEMPTS + 1):
        try:
            _table().update_item(
                Key={"PK": f"AST#{assistant_id}", "SK": f"DOC#{document_id}"},
                UpdateExpression=expression,
                # `status` is a DynamoDB reserved keyword.
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=values,
            )
            return
        except ClientError as exc:
            last = exc
            logger.warning(
                f"attempt {attempt}/{MAX_RECORD_UPDATE_ATTEMPTS} to mark "
                f"{document_id} {status} failed: {exc}"
            )
            if attempt < MAX_RECORD_UPDATE_ATTEMPTS:
                time.sleep(0.2 * attempt)

    # Raised rather than swallowed: the record is the durable retry anchor
    # (Requirement 10.7), so a document left non-terminal must surface as a failed
    # invocation and reach the DLQ instead of looking like a success.
    raise IngestionRoutingError(
        f"could not mark document {document_id} as {status} after "
        f"{MAX_RECORD_UPDATE_ATTEMPTS} attempts: {last}"
    )


#: Bedrock's own view of a document, from the packaged service model's
#: ``DocumentStatus`` enum rather than guessed. Read at call time from
#: ``GetKnowledgeBaseDocuments``, which is the only way to know whether indexing
#: has finished — the ``IngestKnowledgeBaseDocuments`` call returns as soon as the
#: request is accepted and says nothing about progress.
DOC_STATUS_INDEXED = "INDEXED"
DOC_STATUS_NOT_FOUND = "NOT_FOUND"

#: Bedrock is still working. Re-ingesting a document in one of these states is
#: pointless at best: the work is already queued, and re-submitting it discards
#: whatever progress has been made and starts the clock again. That is what turned
#: a slow success into a permanent failure in dev — three redeliveries each
#: re-ingested, and the document only reached INDEXED 54 s after the last attempt
#: had already been dead-lettered.
#:
#: ⚠️ ``TEXT_INDEXED`` is **not in the packaged service model's DocumentStatus
#: enum** — the live service returns statuses the SDK does not declare. Observed in
#: dev on a document with image extraction enabled: it reported ``TEXT_INDEXED``
#: (text searchable, media still processing) and later became ``INDEXED``. It is
#: treated as in-flight rather than as done, because marking a document complete at
#: that point would tell the user an image-only page is ready while the vision
#: model has not finished — precisely the "upload worked but the assistant cannot
#: see it" report this module exists to prevent. Do not derive this set from the
#: SDK enum; it is deliberately wider.
DOC_STATUSES_IN_FLIGHT = frozenset(
    {"STARTING", "PENDING", "IN_PROGRESS", "TEXT_INDEXED"}
)

#: Terminal and unusable. Worth failing the document rather than retrying forever.
DOC_STATUSES_FAILED = frozenset({"FAILED", "METADATA_UPDATE_FAILED"})

#: Indexed, but not everything made it. Treated as usable — the document IS
#: retrievable — because the alternative is failing a document the user can see
#: content from. Logged so the partiality is not silent.
DOC_STATUSES_PARTIAL = frozenset({"PARTIALLY_INDEXED", "METADATA_PARTIALLY_INDEXED"})


def document_status(backend: Any, kb_ref: str, document_id: str) -> Tuple[str, Optional[str]]:
    """Ask Bedrock for a document's ingestion status and its own timestamp.

    Returns ``(status, updated_at_iso)``. ``NOT_FOUND`` covers both "Bedrock has
    never heard of it" and "the call failed", because both mean the same thing to
    the caller: there is no evidence the document is already being worked on, so
    ingesting is the right next move. A probe failure must not be mistaken for a
    document failure.

    This exists because the ingest call is fire-and-forget. Without it the consumer
    cannot distinguish "not indexed yet" from "never submitted", so every
    redelivery re-submits — see :data:`DOC_STATUSES_IN_FLIGHT`.
    """
    try:
        # Imported here, not at module scope: this module's module-level imports are
        # stdlib only so the shared Lambda image stays small
        # (tests/architecture/test_kb_backend_boundary.py). Reused rather than
        # redefined so the connector type has one definition.
        from apis.shared.kb_backend.managed_backend import CONTENT_DATA_SOURCE_TYPE

        client = backend._agent()  # noqa: SLF001 - same package, deliberate reuse
        aws_kb_id, data_source_id = backend._locate(kb_ref)  # noqa: SLF001
        response = client.get_knowledge_base_documents(
            knowledgeBaseId=aws_kb_id,
            dataSourceId=data_source_id,
            documentIdentifiers=[
                {"dataSourceType": CONTENT_DATA_SOURCE_TYPE, "custom": {"id": document_id}}
            ],
        )
    except Exception as exc:  # noqa: BLE001 - a probe failure is not a document failure
        logger.warning(f"document status probe for {document_id} failed: {exc}")
        return DOC_STATUS_NOT_FOUND, None

    for detail in response.get("documentDetails") or []:
        status = str(detail.get("status") or DOC_STATUS_NOT_FOUND)
        updated = detail.get("updatedAt")
        return status, (updated.isoformat() if hasattr(updated, "isoformat") else updated)
    return DOC_STATUS_NOT_FOUND, None


def wait_until_indexed(
    backend: Any,
    kb_ref: str,
    document_id: str,
    timeout_seconds: Optional[float] = None,
    interval_seconds: Optional[float] = None,
    sleep: Any = time.sleep,
) -> Tuple[str, Optional[str]]:
    """Poll Bedrock's document status until it settles, or give up.

    Returns the last ``(status, updated_at)`` seen. Giving up is an ordinary
    outcome, not an error: the caller leaves the document non-terminal and lets
    redelivery come back to it, by which time indexing has usually finished.

    Why a *bounded* in-invocation wait rather than pure redelivery: most documents
    index in a few seconds, and making every one of them wait for an EventBridge
    retry would add a minute of latency to the common case for the sake of the rare
    slow one. Why bounded at all: PDF ingestion was measured at 37–264 s and
    image-heavy files run longer, so waiting for the worst case in-invocation would
    hold a concurrency slot for minutes and bill for it.

    Same call-time constant resolution as :func:`wait_until_retrievable`, and for
    the same reason — a default argument is bound once at import and cannot be
    patched by a test.
    """
    if timeout_seconds is None:
        timeout_seconds = INDEXED_POLL_TIMEOUT_SECONDS
    if interval_seconds is None:
        interval_seconds = INDEXED_POLL_INTERVAL_SECONDS

    deadline = time.monotonic() + timeout_seconds
    status, updated_at = document_status(backend, kb_ref, document_id)
    while _still_working(status, document_id) and time.monotonic() < deadline:
        sleep(interval_seconds)
        status, updated_at = document_status(backend, kb_ref, document_id)
    return status, updated_at


def _still_working(status: str, document_id: str) -> bool:
    """Whether to keep waiting on ``status``.

    Anything not recognised counts as still working, deliberately. The live service
    already returns at least one status the packaged model does not declare
    (``TEXT_INDEXED``), so treating unknown values as terminal would dead-letter
    documents the day AWS adds another. Waiting is bounded by the caller's deadline,
    so the cost of guessing wrong here is one poll budget rather than a lost
    document — and the log line names the value so it can be classified properly.
    """
    if status in DOC_STATUSES_IN_FLIGHT:
        return True
    if status in (DOC_STATUS_INDEXED, DOC_STATUS_NOT_FOUND, *DOC_STATUSES_PARTIAL):
        return False
    if status in DOC_STATUSES_FAILED:
        return False
    logger.warning(
        f"document {document_id} reported unrecognised status {status!r}; treating "
        f"it as still indexing. If this is terminal, add it to the appropriate set "
        f"in ingestion_consumer.py"
    )
    return True


def wait_until_retrievable(
    backend: Any,
    kb_ref: str,
    document_id: str,
    timeout_seconds: Optional[float] = None,
    interval_seconds: Optional[float] = None,
    sleep: Any = time.sleep,
) -> Optional[str]:
    """Confirm a retrieval really returns ``document_id``, filtered to that document.

    Returns the timestamp at which it was first confirmed, or ``None`` on timeout.
    A probe that itself errors is treated as "not yet", not as a document failure:
    the document is usually fine and merely slow, and failing it would fail uploads
    that are about to work.

    THE FILTER IS THE WHOLE POINT
    An earlier version searched for the document *id as the query text* and checked
    whether that document appeared in the top 5 results. A document id carries no
    meaning to an embedding model, so the search returned whatever the reranker
    liked best — measured in dev with two documents in the knowledge base, querying
    ``DOC-40e985680a63`` returned five chunks and every one of them belonged to a
    *different* document. The probe reported "not retrievable" for a document that
    was perfectly retrievable.

    That failure scales the wrong way: the more documents a knowledge base holds,
    the less likely the target appears in an unfiltered top-5, so every upload to a
    mature knowledge base would burn its full poll budget and then dead-letter. It
    only ever worked when the knowledge base held a single document, where anything
    returned was necessarily the right thing.

    An ``equals`` filter on ``document_id`` makes the question exact: chunks come
    back only for this document, so a non-empty result *is* proof of retrievability
    and an empty one is a true negative. Verified against dev: each document
    returned 5 of its own chunks, and a fabricated id returned none. ``equals`` is
    in ``ISOLATION_SAFE_FILTER_OPERATORS``, so it passes the adapter's filter
    validation.

    The timeouts default to ``None`` and are resolved from the module constants *at
    call time*, rather than being bound as default arguments. Default arguments are
    evaluated once at import, which makes them unpatchable — the first version of
    this function bound them directly and a test that shortened the window had no
    effect at all, silently waiting the full production timeout instead.
    """
    import asyncio

    if timeout_seconds is None:
        timeout_seconds = RETRIEVABLE_POLL_TIMEOUT_SECONDS
    if interval_seconds is None:
        interval_seconds = RETRIEVABLE_POLL_INTERVAL_SECONDS

    # Exact-match only. A prefix or substring operator would let `DOC-1` match
    # `DOC-10` and confirm the wrong document as retrievable.
    document_filter = {"equals": {"key": "document_id", "value": document_id}}

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            chunks = asyncio.run(
                backend.search(kb_ref, document_id, 5, retrieval_filter=document_filter)
            )
        except Exception as exc:  # noqa: BLE001 - a probe failure is not a document failure
            logger.warning(f"retrievability probe for {document_id} failed: {exc}")
            chunks = []

        # The filter already restricts the result set to this document, so anything
        # coming back is the answer. The per-chunk check stays as a belt-and-braces
        # guard against a filter that is silently ignored by a future API change.
        for chunk in chunks or []:
            metadata = getattr(chunk, "metadata", None) or {}
            if metadata.get("document_id") == document_id:
                return _now_iso()

        sleep(interval_seconds)

    logger.warning(
        f"document {document_id} was not retrievable within {timeout_seconds}s; "
        f"leaving it short of complete rather than claiming success"
    )
    return None


def handle_object(bucket: str, key: str) -> Dict[str, Any]:
    """Route one uploaded object. Returns a summary for logging and tests."""
    from apis.shared.kb_backend.records import ENGINE_MANAGED

    assistant_id, document_id, filename = parse_object_key(key)
    engine, record = resolve_engine_for(assistant_id)

    if engine != ENGINE_MANAGED:
        # The legacy pipeline's own S3 notification already owns this document.
        # Anything done here would index the same bytes a second time.
        logger.info(
            f"document {document_id} belongs to a legacy knowledge base; "
            f"leaving it to the existing pipeline"
        )
        return {"routed": "legacy", "ingested": False, "document_id": document_id}

    aws_kb_id = (record or {}).get("awsKbId")
    data_source_id = (record or {}).get("awsDataSourceId")
    if not aws_kb_id or not data_source_id:
        # Managed engine but no identifiers means provisioning has not finished.
        # Failing loudly is correct: silently falling back to legacy would create
        # exactly the dual-index this function exists to prevent.
        raise IngestionRoutingError(
            f"assistant {assistant_id} resolves to the managed engine but its "
            f"knowledge base is not provisioned (awsKbId={aws_kb_id!r}, "
            f"awsDataSourceId={data_source_id!r})"
        )

    import asyncio

    from apis.shared.kb_backend.managed_backend import ManagedKbBackend
    from apis.shared.kb_backend.protocol import DocumentSource

    # The backend takes the App_KB_Id and resolves the AWS identifiers itself on
    # every operation. Threading them in from here would defeat that: a
    # dormancy/rehydration cycle replaces them, and a caller holding a stale pair
    # would keep addressing a knowledge base that no longer exists. The check above
    # is still worth doing - it fails fast with a precise reason - but it is a
    # precondition, not a value to pass along.
    backend = ManagedKbBackend(bucket=bucket)
    source = DocumentSource(document_id=document_id, filename=filename, s3_key=key)

    # Ask Bedrock what it already knows BEFORE ingesting. The ingest call is
    # fire-and-forget — it returns as soon as the request is accepted — so without
    # this the consumer cannot tell "not indexed yet" from "never submitted", and
    # every redelivery re-submits a document that is already being worked on.
    #
    # That is not merely wasteful. In dev a 1.5 MB PDF was re-ingested on each of
    # three redeliveries and reached INDEXED only 54 s after the final attempt had
    # been dead-lettered, leaving a perfectly retrievable document parked at
    # `uploading` with nothing left to reconcile it.
    status, bedrock_updated_at = document_status(backend, assistant_id, document_id)

    if status in DOC_STATUSES_FAILED:
        # Terminal on Bedrock's side. Retrying cannot help.
        logger.error(f"document {document_id} is {status} in the knowledge base")
        set_document_terminal(
            assistant_id, document_id, STATUS_FAILED,
            error=f"the knowledge base reports this document as {status}",
        )
        return {"routed": "managed", "ingested": False, "document_id": document_id,
                "status": status}

    if status == DOC_STATUS_NOT_FOUND:
        # Bedrock has never heard of it, so this is the first delivery. Submit.
        try:
            asyncio.run(backend.ingest(assistant_id, source))
        except Exception as exc:
            logger.error(f"direct ingestion of {document_id} failed: {exc}", exc_info=True)
            set_document_terminal(assistant_id, document_id, STATUS_FAILED, error=str(exc))
            raise
    else:
        # Already submitted — a redelivery, or a document still being worked on.
        # Do NOT ingest again: re-submitting discards the progress this invocation
        # is about to wait for, which is what turned a slow success into a
        # permanent failure in dev.
        logger.info(
            f"document {document_id} is already {status} in the knowledge base; "
            f"not re-ingesting"
        )

    # One wait, whichever way we arrived. Both "just submitted" and "found it
    # mid-flight" need the same thing: give Bedrock time, bounded by a budget that
    # fits inside this Lambda, because redelivery is capped at 2 retries.
    if _still_working(status, document_id) or status == DOC_STATUS_NOT_FOUND:
        status, bedrock_updated_at = wait_until_indexed(backend, assistant_id, document_id)

    if status in DOC_STATUSES_FAILED:
        logger.error(f"document {document_id} became {status} during indexing")
        set_document_terminal(
            assistant_id, document_id, STATUS_FAILED,
            error=f"the knowledge base reports this document as {status}",
        )
        return {"routed": "managed", "ingested": True, "document_id": document_id,
                "status": status}

    if status in DOC_STATUSES_PARTIAL:
        logger.warning(
            f"document {document_id} is {status}: it is retrievable but some of its "
            f"content or metadata did not index"
        )

    if status not in (DOC_STATUS_INDEXED, *DOC_STATUSES_PARTIAL):
        # Still not done inside our budget. Deliberately NOT marked terminal, and
        # deliberately not given an invented timestamp — a later delivery will find
        # it INDEXED and record Bedrock's own.
        raise IngestionRoutingError(
            f"document {document_id} is {status} after waiting; leaving it for "
            f"redelivery to confirm indexing"
        )

    # INDEXED. `indexedAt` is Bedrock's OWN timestamp, not this process's clock —
    # an earlier version recorded `_now_iso()` immediately after the ingest call
    # returned, which measured when the request was accepted and was reported as
    # the moment indexing completed. The two can be minutes apart.
    indexed_at = bedrock_updated_at or _now_iso()
    retrievable_at = wait_until_retrievable(backend, assistant_id, document_id)

    if retrievable_at is None:
        # INDEXED but not yet queryable. This is the one short, real gap the poll
        # window was always sized for (measured at 0.75–1.03 s); a timeout here is
        # unusual rather than routine, so leave it for redelivery.
        raise IngestionRoutingError(
            f"document {document_id} is INDEXED but was not retrievable within the "
            f"poll window; leaving it for redelivery"
        )

    set_document_terminal(
        assistant_id,
        document_id,
        STATUS_COMPLETE,
        indexed_at=indexed_at,
        retrievable_at=retrievable_at,
    )
    return {
        "routed": "managed",
        "ingested": True,
        "document_id": document_id,
        "indexedAt": indexed_at,
        "retrievableAt": retrievable_at,
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Entry point. One invocation drives its documents to terminal, or fails."""
    records = extract_records(event)
    if not records:
        logger.info("no S3 records in event; nothing to do")
        return {"statusCode": 200, "processed": 0, "results": []}

    results = []
    for record in records:
        bucket, key = record.get("bucket", ""), record.get("key", "")
        if not bucket or not key:
            logger.warning(f"skipping record with missing bucket or key: {record}")
            continue
        results.append(handle_object(bucket, key))

    return {"statusCode": 200, "processed": len(results), "results": results}
