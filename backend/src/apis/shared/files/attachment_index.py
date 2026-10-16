"""The shared ``attachments`` managed knowledge base (large-document spec §4E, Tier 2).

A prepared reference-class document (``is_reference_document`` and
``large_document.is_prepared``) is ingested into one knowledge base per
environment, by the S3 location of its **original** (§9 D2: the managed parser does
its own OCR and layout, which is why Tier 2 exists; we never send our text layer).
Tier 1 never depends on any of this: ``document_read``'s pattern, text and page
modes keep reading the ``_derived/`` sidecars whatever the index says.

Where it runs
-------------
In the ``large-document-prepare`` Lambda, off its own queue (spec deviation,
decided 2026-10-13 against a dev PlatformStack at 492/500 resources: the separate
index queue, consumer and reconciler would have cost ~17). The queue carries four
message kinds, all handled by :func:`handle_message`'s caller:

* after a successful preparation, :func:`sync_upload` runs in the same invocation;
* ``{"action": "index"}`` re-runs it (a retry, or a re-index after eviction);
* ``{"action": "check"}``, sent with a delay, polls the document's status until it
  is indexed (ingestion is accepted asynchronously and measured 68–264 s);
* an S3 *Object Deleted* event for an original (EventBridge rule) re-runs the sync,
  which finds the row or the object gone and removes the document;
* ``{"action": "reconcile"}``, daily (schedule rule), runs :func:`reconcile`.

Every message means "make the index match the row, as it is now"
----------------------------------------------------------------
:func:`sync_upload` reads the row and the object. Eligible and not yet indexed →
reserve its bytes and ingest (re-ingesting an id replaces the document). Not
eligible (deleted, not prepared, object gone) → delete the document and release its
bytes. Event order and redelivery do not matter, as for the conversations consumer.

Document id and attributes
--------------------------
``att#{user_id}#{session_id}#{upload_id}``, with inline attributes ``user_id``,
``session_id``, ``upload_id`` and ``filename`` (the backend adds ``document_id`` and
``filename``). PR-4's search filters on ``user_id`` *and* ``session_id`` and drops a
hit whose id and attributes do not both name the caller and the session.

Bounding storage cost ($5.00/GB-month of raw source bytes)
----------------------------------------------------------
* **Per-user cap**, ``ATTACHMENT_KB_PER_USER_MAX_BYTES`` (500 MB): an atomic
  accumulator ``AST#attachments / OWNER#{user_id}`` plus one marker per document
  (``BYTES#{user_id}#{upload_id}``), written together in one transaction so a
  reservation can be neither double-counted nor lost, and released exactly once.
  The assistants table holds it beside the knowledge base's own record; the
  ``BYTES#`` prefix keeps it out of every ``KB#`` / ``DOC#`` reader there.
* **Eviction after inactivity**, ``ATTACHMENT_KB_RETENTION_DAYS`` (30): inactive
  means no turn in its session since then. The session's ``lastMessageAt`` is
  already maintained by every turn, and a ``document_read`` call is always inside
  one, so no new write is needed anywhere (in particular not on the turn path).
  An eviction or re-index time counts as activity too, so a just re-indexed
  document is not evicted the next morning.
* **Re-index on open**: app-api re-queues a conversation's evicted documents when
  the conversation is opened (:func:`evicted_upload_ids`), on page load, never on
  the turn path.

Content-free
------------
Rows, markers and metrics carry ids, sizes and statuses. Logs carry no filename.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from apis.shared.files import large_document
from apis.shared.files.models import FileMetadata, FileStatus, is_reference_document

logger = logging.getLogger(__name__)

KB_ID = "attachments"  # == apis.shared.kb_backend.reserved.ATTACHMENTS_KB_ID (pinned by a test)

#: Written beside the tag contract's keys so the KB's storage line is attributable.
KB_PURPOSE_TAGS = {"purpose": "attachment-search"}

DOCUMENT_ID_PREFIX = "att#"

STATUS_QUEUED = "queued"
STATUS_INDEXING = "indexing"
STATUS_INDEXED = "indexed"
STATUS_FAILED = "failed"
STATUS_SKIPPED_CAP = "skipped_cap"
STATUS_EVICTED = "evicted"
STATUS_REMOVED = "removed"

#: Bedrock document statuses (see ``kb_migration/ingestion_consumer.py`` for why
#: ``TEXT_INDEXED`` is in flight, not done).
DOC_INDEXED = frozenset({"INDEXED", "PARTIALLY_INDEXED", "METADATA_PARTIALLY_INDEXED"})
DOC_IN_FLIGHT = frozenset({"STARTING", "PENDING", "IN_PROGRESS", "TEXT_INDEXED"})
DOC_FAILED = frozenset({"FAILED", "METADATA_UPDATE_FAILED"})
DOC_GONE = frozenset({"NOT_FOUND", "DELETING", "DELETE_IN_PROGRESS"})

#: In flight, but the text is already retrievable; only image extraction (OCR,
#: figure descriptions) is still running. Measured on dev 2026-10-13 on a 43.8 MB,
#: 500-page PDF: TEXT_INDEXED at +80 s, INDEXED at +10 m 47 s. It stays in
#: ``DOC_IN_FLIGHT`` (``indexed`` means the whole document), and the row records it
#: as ``textIndexedAt`` so search can start ~10 minutes earlier.
DOC_TEXT_INDEXED = "TEXT_INDEXED"

#: Delays between status checks, in seconds (SQS caps one delay at 900). About an
#: hour and a quarter in all; a document still in flight after that is left to
#: the daily reconcile.
CHECK_DELAYS_SECONDS = (120, 180, 300, 600, 900, 900, 900, 900)

_LIST_PAGE_SIZE = 100
_BYTES_PK = f"AST#{KB_ID}"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def per_user_cap_bytes() -> int:
    return _env_int("ATTACHMENT_KB_PER_USER_MAX_BYTES", 500 * 1024 * 1024)


def retention_days() -> int:
    return _env_int("ATTACHMENT_KB_RETENTION_DAYS", 30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Ids and keys
# ---------------------------------------------------------------------------


def _clean(part: str) -> bool:
    return bool(part) and "#" not in part and "/" not in part


def document_id(user_id: str, session_id: str, upload_id: str) -> str:
    if not (_clean(user_id) and _clean(session_id) and _clean(upload_id)):
        raise ValueError("attachment document id parts must be non-empty and contain no '#' or '/'")
    return f"{DOCUMENT_ID_PREFIX}{user_id}#{session_id}#{upload_id}"


def parse_document_id(value: str) -> Optional[Tuple[str, str, str]]:
    if not isinstance(value, str) or not value.startswith(DOCUMENT_ID_PREFIX):
        return None
    parts = value[len(DOCUMENT_ID_PREFIX):].split("#")
    if len(parts) != 3 or not all(_clean(p) for p in parts):
        return None
    return parts[0], parts[1], parts[2]


def parse_original_key(key: str) -> Optional[Tuple[str, str, str]]:
    """``(user_id, session_id, upload_id)`` for an upload's original object.

    ``user-files/{user}/{session}/{upload_id}/{filename}`` exactly; sidecars
    (``_derived/…``, one level deeper) and the ``_thumb.png`` sibling are not
    originals.
    """
    if not isinstance(key, str):
        return None
    parts = key.split("/")
    if len(parts) != 5 or parts[0] != "user-files" or not parts[4] or parts[4] == "_thumb.png":
        return None
    user_id, session_id, upload_id = parts[1], parts[2], parts[3]
    if not (_clean(user_id) and _clean(session_id) and _clean(upload_id)):
        return None
    return user_id, session_id, upload_id


def kb_index_record(status: str, previous: Optional[dict] = None, **extra: Any) -> Dict[str, Any]:
    """The ``kbIndex`` map: status, timestamps and an error class name only."""
    record: Dict[str, Any] = {"status": status, "updatedAt": _iso(_now())}
    if previous and previous.get("indexedAt") and status in (STATUS_INDEXING, STATUS_INDEXED):
        record["indexedAt"] = previous["indexedAt"]
    record.update({k: v for k, v in extra.items() if v is not None})
    return record


def is_text_indexed(meta: Optional[FileMetadata]) -> bool:
    """Indexing, with its text already searchable (Bedrock reported TEXT_INDEXED)."""
    kb = getattr(meta, "kb_index", None)
    return isinstance(kb, dict) and kb.get("status") == STATUS_INDEXING and bool(kb.get("textIndexedAt"))


def kb_status(meta: Optional[FileMetadata]) -> Optional[str]:
    kb = getattr(meta, "kb_index", None)
    status = kb.get("status") if isinstance(kb, dict) else None
    return status if isinstance(status, str) else None


#: Whether a document can be searched, as ``document_read``'s query mode and the
#: SPA's chip (``GET /files/{id}/search-status``) describe it. Derived from the
#: row alone, so neither asks Bedrock.
SEARCH_OFF = "off"
SEARCH_PREPARING = "preparing"
SEARCH_INDEXING = "indexing"
SEARCH_SEARCHABLE = "searchable"
SEARCH_EVICTED = "evicted"
SEARCH_UNAVAILABLE = "unavailable"


def search_status(meta: Optional[FileMetadata], *, kb_enabled: bool) -> str:
    """One document's searchability.

    A document whose text is indexed while its images are still being processed
    (``textIndexedAt``) is already ``searchable``: text search works from then on,
    and ``document_read``'s query mode says image content is still pending.

    ``off`` when the deployment has no attachments KB or the file is not
    reference-class (it is never indexed). ``preparing`` until its sidecars are
    built, because indexing follows preparation. A prepared document with no
    ``kbIndex`` yet is ``indexing``: the same invocation that finished the
    preparation is about to ingest it. ``unavailable`` covers a failed
    preparation or ingest, the per-user cap and a removed document; Tier 1
    reads are unaffected by any of them. So is a document that was never queued
    for preparation (``complete_upload`` writes ``pending`` before it returns, so
    a missing preparation is not a race).
    """
    if not kb_enabled or meta is None or not is_reference_document(meta):
        return SEARCH_OFF
    preparation = large_document.preparation_status(meta)
    if preparation in (None, large_document.STATUS_FAILED):
        return SEARCH_UNAVAILABLE
    if not large_document.is_prepared(meta):
        return SEARCH_PREPARING
    status = kb_status(meta)
    if status == STATUS_INDEXED or is_text_indexed(meta):
        return SEARCH_SEARCHABLE
    if status in (None, STATUS_QUEUED, STATUS_INDEXING):
        return SEARCH_INDEXING
    if status == STATUS_EVICTED:
        return SEARCH_EVICTED
    return SEARCH_UNAVAILABLE


def is_eligible(meta: Optional[FileMetadata]) -> bool:
    """A READY, reference-class, prepared row. The object's presence is checked separately."""
    if meta is None:
        return False
    status = meta.status if isinstance(meta.status, str) else meta.status.value
    return status == FileStatus.READY.value and is_reference_document(meta) and large_document.is_prepared(meta)


# ---------------------------------------------------------------------------
# Per-user byte cap (AST#attachments in the assistants table)
# ---------------------------------------------------------------------------


def _assistants_table():
    import boto3

    return boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION") or None).Table(
        os.environ["DYNAMODB_ASSISTANTS_TABLE_NAME"]
    )


def _owner_key(user_id: str) -> Dict[str, str]:
    return {"PK": _BYTES_PK, "SK": f"OWNER#{user_id}"}


def _marker_key(user_id: str, upload_id: str) -> Dict[str, str]:
    return {"PK": _BYTES_PK, "SK": f"BYTES#{user_id}#{upload_id}"}


def reserve_bytes(user_id: str, upload_id: str, n_bytes: int, cap: Optional[int] = None, *, table: Any = None) -> bool:
    """Count ``n_bytes`` against the user's cap, once per document. False when over the cap.

    One transaction: the marker (only if absent) and the accumulator (only while
    ``totalBytes <= cap - n``, DynamoDB conditions cannot add). A document already
    reserved returns True without counting again.
    """
    from botocore.exceptions import ClientError

    cap = per_user_cap_bytes() if cap is None else cap
    if n_bytes > cap:
        return False
    table = table or _assistants_table()
    client = table.meta.client
    try:
        client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": table.name,
                        "Item": {**_marker_key(user_id, upload_id), "bytes": Decimal(n_bytes), "reservedAt": _iso(_now())},
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                },
                {
                    "Update": {
                        "TableName": table.name,
                        "Key": _owner_key(user_id),
                        "UpdateExpression": "ADD totalBytes :n",
                        "ConditionExpression": "attribute_not_exists(totalBytes) OR totalBytes <= :max_before",
                        "ExpressionAttributeValues": {":n": Decimal(n_bytes), ":max_before": Decimal(cap - n_bytes)},
                    }
                },
            ]
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        reasons = exc.response.get("CancellationReasons") or []
        codes = [str((r or {}).get("Code") or "") for r in reasons]
        if codes and codes[0] == "ConditionalCheckFailed":
            return True  # already reserved for this document
        if len(codes) > 1 and codes[1] == "ConditionalCheckFailed":
            return False  # over the cap
        raise


def release_bytes(user_id: str, upload_id: str, *, table: Any = None) -> int:
    """Return a document's reserved bytes, exactly once. Returns the amount released."""
    from botocore.exceptions import ClientError

    table = table or _assistants_table()
    marker = table.get_item(Key=_marker_key(user_id, upload_id)).get("Item")
    if not marker:
        return 0
    n_bytes = int(marker.get("bytes") or 0)
    try:
        table.meta.client.transact_write_items(
            TransactItems=[
                {
                    "Delete": {
                        "TableName": table.name,
                        "Key": _marker_key(user_id, upload_id),
                        "ConditionExpression": "attribute_exists(PK)",
                    }
                },
                {
                    "Update": {
                        "TableName": table.name,
                        "Key": _owner_key(user_id),
                        "UpdateExpression": "ADD totalBytes :neg",
                        "ExpressionAttributeValues": {":neg": Decimal(-n_bytes)},
                    }
                },
            ]
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
            return 0  # another release won
        raise
    return n_bytes


# ---------------------------------------------------------------------------
# AWS plumbing (patched in tests)
# ---------------------------------------------------------------------------


def bedrock_agent_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "bedrock-agent",
        region_name=os.environ.get("AWS_REGION") or None,
        config=Config(retries={"mode": "adaptive", "max_attempts": 8}),
    )


def _is_missing(exc: BaseException) -> bool:
    response = getattr(exc, "response", None) or {}
    code = str((response.get("Error") or {}).get("Code") or "")
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return code in ("NoSuchKey", "NotFound", "404") or status == 404


def object_exists(s3: Any, bucket: str, key: str) -> bool:
    """Only a real 404 means gone: a 403 is a grant regression, not a delete."""
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 - classified below
        if _is_missing(exc):
            return False
        raise
    return True


class Indexer:
    """Everything one invocation needs, injectable for tests."""

    def __init__(
        self,
        *,
        repository: Any = None,
        s3: Any = None,
        agent_client: Any = None,
        backend: Any = None,
        ensure_kb: Any = None,
        bytes_table: Any = None,
        send_message: Any = None,
        session_activity: Any = None,
    ) -> None:
        from apis.shared.files.repository import get_file_upload_repository

        self.repository = repository or get_file_upload_repository()
        self._s3 = s3
        self._agent_client = agent_client
        self._backend = backend
        self._ensure_kb = ensure_kb
        self._bytes_table = bytes_table
        self._send_message = send_message
        self._session_activity = session_activity

    # lazily built AWS pieces -------------------------------------------------
    @property
    def s3(self):
        if self._s3 is None:
            from apis.shared.aws_clients import get_client

            self._s3 = get_client("s3", os.environ.get("AWS_REGION") or None)
        return self._s3

    @property
    def agent_client(self):
        if self._agent_client is None:
            self._agent_client = bedrock_agent_client()
        return self._agent_client

    @property
    def backend(self):
        if self._backend is None:
            from apis.shared.kb_backend.managed_backend import ManagedKbBackend

            self._backend = ManagedKbBackend(
                agent_client=self.agent_client, bucket=os.environ.get("S3_USER_FILES_BUCKET_NAME") or None
            )
        return self._backend

    @property
    def bytes_table(self):
        if self._bytes_table is None:
            self._bytes_table = _assistants_table()
        return self._bytes_table

    async def ensure_kb(self) -> None:
        if self._ensure_kb is not None:
            await self._ensure_kb()
            return
        from apis.shared.kb_backend.reserved_kb import ensure_reserved_kb

        await ensure_reserved_kb(KB_ID, self.agent_client, purpose_tags=KB_PURPOSE_TAGS)

    def send(self, body: Dict[str, Any], delay_seconds: int = 0) -> None:
        if self._send_message is not None:
            self._send_message(body, delay_seconds)
            return
        from apis.shared.aws_clients import get_client

        queue_url = os.environ.get("LARGE_DOCUMENT_PREPARE_QUEUE_URL", "").strip()
        if not queue_url:
            raise RuntimeError("LARGE_DOCUMENT_PREPARE_QUEUE_URL is not set")
        get_client("sqs", os.environ.get("AWS_REGION") or None).send_message(
            QueueUrl=queue_url, MessageBody=json.dumps(body), DelaySeconds=max(0, min(int(delay_seconds), 900))
        )

    def last_session_activity(self, user_id: str, session_id: str) -> Optional[datetime]:
        if self._session_activity is not None:
            return self._session_activity(user_id, session_id)
        return session_last_message_at(user_id, session_id)

    # the document's KB status ------------------------------------------------
    def document_status(self, doc_id: str) -> str:
        from apis.shared.kb_backend.managed_backend import CONTENT_DATA_SOURCE_TYPE

        aws_kb_id, data_source_id = self.backend._locate(KB_ID)  # noqa: SLF001 - same package family
        response = self.agent_client.get_knowledge_base_documents(
            knowledgeBaseId=aws_kb_id,
            dataSourceId=data_source_id,
            documentIdentifiers=[{"dataSourceType": CONTENT_DATA_SOURCE_TYPE, "custom": {"id": doc_id}}],
        )
        for detail in response.get("documentDetails") or []:
            return str(detail.get("status") or "NOT_FOUND")
        return "NOT_FOUND"

    async def delete_document(self, doc_id: str) -> None:
        from apis.shared.kb_backend.managed_backend import ManagedKbNotProvisioned

        try:
            await self.backend.delete_documents(KB_ID, [doc_id])
        except ManagedKbNotProvisioned:
            pass  # no knowledge base, so nothing was ever indexed; never provision to delete


def session_last_message_at(user_id: str, session_id: str) -> Optional[datetime]:
    """The session's ``lastMessageAt`` (SessionLookupIndex), or None when the row is gone."""
    import boto3
    from boto3.dynamodb.conditions import Key

    table_name = os.environ.get("DYNAMODB_SESSIONS_METADATA_TABLE_NAME", "").strip()
    if not table_name:
        return None
    table = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION") or None).Table(table_name)
    items = table.query(
        IndexName="SessionLookupIndex",
        KeyConditionExpression=Key("GSI_PK").eq(f"SESSION#{session_id}") & Key("GSI_SK").eq("META"),
    ).get("Items") or []
    for item in items:
        if item.get("PK") == f"USER#{user_id}" or item.get("userId") == user_id:
            return _parse_iso(item.get("lastMessageAt"))
    return None


# ---------------------------------------------------------------------------
# Sync, check, evict
# ---------------------------------------------------------------------------


async def _set_status(indexer: Indexer, meta: FileMetadata, status: str, **extra: Any) -> None:
    await indexer.repository.update_file_kb_index(
        meta.user_id, meta.upload_id, kb_index_record(status, meta.kb_index, **extra)
    )


async def remove_upload(indexer: Indexer, user_id: str, session_id: str, upload_id: str, *, status: str = STATUS_REMOVED) -> str:
    """Delete the document, release its bytes, and record ``status`` on the row if it exists."""
    await indexer.delete_document(document_id(user_id, session_id, upload_id))
    await asyncio.to_thread(release_bytes, user_id, upload_id, table=indexer.bytes_table)
    meta = await indexer.repository.get_file(user_id, upload_id)
    if meta is not None and kb_status(meta) not in (None, status):
        await _set_status(indexer, meta, status)
    return status


async def sync_upload(indexer: Indexer, user_id: str, upload_id: str, *, session_id: Optional[str] = None, force: bool = False) -> str:
    """Make the index match the row for one upload. Returns the outcome.

    ``force`` re-ingests a document already indexed or in flight (a re-index).
    Raises on AWS errors so the message is retried.
    """
    meta = await indexer.repository.get_file(user_id, upload_id)
    if meta is None:
        if session_id is None:
            return "missing"
        return await remove_upload(indexer, user_id, session_id, upload_id)

    present = is_eligible(meta) and await asyncio.to_thread(object_exists, indexer.s3, meta.s3_bucket, meta.s3_key)
    if not present:
        if kb_status(meta) in (None, STATUS_REMOVED):
            return "not_eligible"
        return await remove_upload(indexer, user_id, meta.session_id, upload_id)

    if not force and kb_status(meta) in (STATUS_INDEXING, STATUS_INDEXED):
        return "already_indexed"

    reserved = await asyncio.to_thread(
        reserve_bytes, user_id, upload_id, meta.size_bytes, table=indexer.bytes_table
    )
    if not reserved:
        await _set_status(indexer, meta, STATUS_SKIPPED_CAP)
        record_index_outcome("skipped_cap")
        return STATUS_SKIPPED_CAP

    from apis.shared.kb_backend.protocol import DocumentSource

    doc_id = document_id(user_id, meta.session_id, upload_id)
    await indexer.ensure_kb()
    await indexer.backend.ingest_documents(
        KB_ID,
        [
            DocumentSource(
                document_id=doc_id,
                filename=meta.filename,
                s3_key=meta.s3_key,
                metadata={"user_id": user_id, "session_id": meta.session_id, "upload_id": upload_id},
            )
        ],
    )
    # A delete that landed between the read above and the ingest returning
    # would otherwise leave the document indexed for good.
    if not await asyncio.to_thread(object_exists, indexer.s3, meta.s3_bucket, meta.s3_key):
        return await remove_upload(indexer, user_id, meta.session_id, upload_id)
    stored = await indexer.repository.update_file_kb_index(
        user_id, upload_id, kb_index_record(STATUS_INDEXING, meta.kb_index, submittedAt=_iso(_now()))
    )
    if stored is None:
        return await remove_upload(indexer, user_id, meta.session_id, upload_id)
    indexer.send({"action": "check", "user_id": user_id, "upload_id": upload_id, "attempt": 1}, CHECK_DELAYS_SECONDS[0])
    return STATUS_INDEXING


async def check_upload(indexer: Indexer, user_id: str, upload_id: str, attempt: int = 1) -> str:
    """Poll one in-flight document; re-schedules itself while Bedrock is still working."""
    meta = await indexer.repository.get_file(user_id, upload_id)
    if meta is None or kb_status(meta) != STATUS_INDEXING:
        return "not_indexing"
    status = await asyncio.to_thread(indexer.document_status, document_id(user_id, meta.session_id, upload_id))
    if status in DOC_INDEXED:
        await _set_status(indexer, meta, STATUS_INDEXED, indexedAt=_iso(_now()))
        record_index_outcome("indexed", attempt=attempt)
        return STATUS_INDEXED
    if status == DOC_TEXT_INDEXED and not is_text_indexed(meta):
        await _set_status(indexer, meta, STATUS_INDEXING, textIndexedAt=_iso(_now()))
    if status in DOC_FAILED:
        await indexer.delete_document(document_id(user_id, meta.session_id, upload_id))
        await asyncio.to_thread(release_bytes, user_id, upload_id, table=indexer.bytes_table)
        await _set_status(indexer, meta, STATUS_FAILED, error=status)
        record_index_outcome("failed", attempt=attempt)
        return STATUS_FAILED
    if attempt >= len(CHECK_DELAYS_SECONDS):
        logger.info("attachment index: document still %s after %d checks; left to the reconcile", status, attempt)
        return "still_in_flight"
    indexer.send(
        {"action": "check", "user_id": user_id, "upload_id": upload_id, "attempt": attempt + 1},
        CHECK_DELAYS_SECONDS[attempt],
    )
    return "rescheduled"


def _last_activity(indexer: Indexer, meta: FileMetadata) -> datetime:
    """The latest of: the session's last turn, the last (re-)index, the row's creation."""
    candidates: List[datetime] = []
    activity = indexer.last_session_activity(meta.user_id, meta.session_id)
    if activity:
        candidates.append(activity)
    kb = meta.kb_index or {}
    for key in ("indexedAt", "submittedAt"):
        parsed = _parse_iso(kb.get(key))
        if parsed:
            candidates.append(parsed)
    created = meta.created_at if meta.created_at.tzinfo else meta.created_at.replace(tzinfo=timezone.utc)
    candidates.append(created)
    return max(candidates)


async def evict_if_inactive(indexer: Indexer, meta: FileMetadata, *, now: Optional[datetime] = None) -> bool:
    now = now or _now()
    if now - _last_activity(indexer, meta) < timedelta(days=retention_days()):
        return False
    await remove_upload(indexer, meta.user_id, meta.session_id, meta.upload_id, status=STATUS_EVICTED)
    record_index_outcome("evicted")
    return True


# ---------------------------------------------------------------------------
# The daily reconcile
# ---------------------------------------------------------------------------


def list_documents(indexer: Indexer) -> List[Tuple[str, str]]:
    """``(document_id, status)`` for every attachment document in the knowledge base."""
    from apis.shared.kb_backend.managed_backend import ManagedKbNotProvisioned

    try:
        aws_kb_id, data_source_id = indexer.backend._locate(KB_ID)  # noqa: SLF001
    except ManagedKbNotProvisioned:
        return []
    out: List[Tuple[str, str]] = []
    token: Optional[str] = None
    while True:
        params: Dict[str, Any] = {"knowledgeBaseId": aws_kb_id, "dataSourceId": data_source_id, "maxResults": _LIST_PAGE_SIZE}
        if token:
            params["nextToken"] = token
        page = indexer.agent_client.list_knowledge_base_documents(**params)
        for detail in page.get("documentDetails") or []:
            doc_id = ((detail.get("identifier") or {}).get("custom") or {}).get("id") or ""
            if parse_document_id(doc_id) is not None:
                out.append((doc_id, str(detail.get("status") or "")))
        token = page.get("nextToken")
        if not token:
            return out


async def reconcile(indexer: Indexer, *, now: Optional[datetime] = None) -> Dict[str, int]:
    """Walk the knowledge base: remove orphans, settle statuses, evict inactive documents.

    Every document in the knowledge base is checked against its row:

    * no row, or no longer eligible → removed (covers a lost delete event and
      lifecycle expiry of the original);
    * failed at Bedrock → removed, row ``failed``;
    * indexed but the row still says ``indexing`` (the checks ran out) → ``indexed``;
    * inactive past the retention → evicted.
    """
    counts = {"documents": 0, "removed": 0, "evicted": 0, "settled": 0, "failed": 0}
    for doc_id, status in await asyncio.to_thread(list_documents, indexer):
        counts["documents"] += 1
        if status in DOC_GONE:
            continue
        user_id, session_id, upload_id = parse_document_id(doc_id)  # type: ignore[misc]
        try:
            meta = await indexer.repository.get_file(user_id, upload_id)
            eligible = is_eligible(meta) and meta.session_id == session_id and await asyncio.to_thread(
                object_exists, indexer.s3, meta.s3_bucket, meta.s3_key
            )
            if not eligible:
                await remove_upload(indexer, user_id, session_id, upload_id)
                counts["removed"] += 1
                continue
            if status in DOC_FAILED:
                await remove_upload(indexer, user_id, session_id, upload_id, status=STATUS_FAILED)
                counts["failed"] += 1
                continue
            if status in DOC_INDEXED and kb_status(meta) != STATUS_INDEXED:
                await _set_status(indexer, meta, STATUS_INDEXED, indexedAt=_iso(now or _now()))
                meta = await indexer.repository.get_file(user_id, upload_id) or meta
                counts["settled"] += 1
            elif status == DOC_TEXT_INDEXED and kb_status(meta) == STATUS_INDEXING and not is_text_indexed(meta):
                await _set_status(indexer, meta, STATUS_INDEXING, textIndexedAt=_iso(now or _now()))
                meta = await indexer.repository.get_file(user_id, upload_id) or meta
                counts["settled"] += 1
            if await evict_if_inactive(indexer, meta, now=now):
                counts["evicted"] += 1
        except Exception:  # noqa: BLE001 - one document never sinks the sweep
            logger.warning("attachment reconcile: one document skipped", exc_info=True)
    logger.info("attachment reconcile: %s", counts)
    return counts


# ---------------------------------------------------------------------------
# Re-index on open (app-api)
# ---------------------------------------------------------------------------


def evicted_upload_ids(files: Iterable[FileMetadata]) -> List[str]:
    """The session's documents that were evicted and are still eligible, in order."""
    return [f.upload_id for f in files if kb_status(f) == STATUS_EVICTED and is_eligible(f)]


# ---------------------------------------------------------------------------
# Metric
# ---------------------------------------------------------------------------


def record_index_outcome(outcome: str, *, attempt: int = 0) -> None:
    """One content-free ``AttachmentKbIndexed`` record (spec §6). Never raises."""
    logger.info("attachment_kb: outcome=%s checks=%d", outcome, attempt)
    try:
        from apis.shared.observability.emf import emit_emf_metrics
        from apis.shared.observability.prompt_cache import prompt_cache_observability_enabled

        if not prompt_cache_observability_enabled():
            return
        emit_emf_metrics(
            "AgentCoreStack/Compaction",
            metrics={"AttachmentKbIndexed": 1},
            properties={"outcome": outcome, "checks": attempt},
            units={"AttachmentKbIndexed": "Count"},
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("AttachmentKbIndexed EMF skipped: %s", e)
