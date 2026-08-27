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


def wait_until_retrievable(
    backend: Any,
    kb_ref: str,
    document_id: str,
    timeout_seconds: Optional[float] = None,
    interval_seconds: Optional[float] = None,
    sleep: Any = time.sleep,
) -> Optional[str]:
    """Poll until a retrieval actually returns ``document_id``.

    Returns the timestamp at which it first became retrievable, or ``None`` on
    timeout. A probe that itself errors is treated as "not yet", not as a document
    failure: the document is usually fine and merely slow, and failing it would fail
    uploads that are about to work.

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

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            chunks = asyncio.run(backend.search(kb_ref, document_id, 5))
        except Exception as exc:  # noqa: BLE001 - a probe failure is not a document failure
            logger.warning(f"retrievability probe for {document_id} failed: {exc}")
            chunks = []

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

    try:
        asyncio.run(backend.ingest(assistant_id, source))
    except Exception as exc:
        logger.error(f"direct ingestion of {document_id} failed: {exc}", exc_info=True)
        set_document_terminal(assistant_id, document_id, STATUS_FAILED, error=str(exc))
        raise

    indexed_at = _now_iso()
    retrievable_at = wait_until_retrievable(backend, assistant_id, document_id)

    if retrievable_at is None:
        # Ingested but not confirmed retrievable. Left non-terminal deliberately so
        # the event source redelivers, rather than the record claiming a success the
        # user cannot yet observe.
        raise IngestionRoutingError(
            f"document {document_id} was ingested but not retrievable within the "
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
