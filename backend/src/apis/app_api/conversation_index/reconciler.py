"""Daily reconciler: the archive past retention goes, and the index follows the archive.

docs/specs/conversation-search.md §3 ("Daily reconciler, as braces") and §7 PR-2c.
The consumer keeps the shared ``conversations`` knowledge base in step with the
archive one S3 event at a time. This pass catches what events miss:

* **Archive objects past retention.** The bucket's lifecycle rule normally
  expires them, asynchronously and up to about a day late. This deletes any it
  has not got to yet, so retention holds to the day.
* **KB documents with no archive object.** A dropped event, a batch that
  dead-lettered, or a session deleted while ``CONVERSATION_INDEX_ENABLED`` was
  off (the consumer then acknowledges events without acting, §4 "Turning the
  index off"). Their turns are gone from the archive, so they must be gone from
  search too. Documents under the first id format (``conv#{session_id}#{index}``,
  without the user) are always in this set: nothing writes that format any more,
  and the same turns are indexed again under the current one.

Document deletes run **whatever the flag says**: this is the cleanup path for
the time the flag was off, and deleting a document makes nothing new.

Order matters: documents first, then the archive
------------------------------------------------
A document is only ever ingested from an object that already exists. So when
the knowledge base is listed *before* the archive, every listed document's
object either shows up in the archive listing or was deleted, and "no object"
really means gone. Listed the other way round, a turn archived and ingested
between the two listings would look orphaned and be deleted.

Quotas
------
``DeleteKnowledgeBaseDocuments`` takes at most ten documents, and the document
quotas (10 deletes/s, 10 concurrent operations) are **per account and shared
with every agent's document uploads**. So this pass issues one call at a time
with a pause between calls (one call a second by default) and caps the
documents it deletes per run; a backlog drains over several days rather than
crowding out users uploading documents. Expired objects deleted here also
raise *Object Deleted* for the consumer; their documents are deleted in the
same pass anyway, so that path holds with the flag off too.

Safety
------
Any listing failure aborts the run before it deletes anything: a partial
listing is indistinguishable from a complete one and would make present
documents look orphaned. An archive that lists empty while the knowledge base
holds documents is treated the same way (a wrong bucket name, or a grant that
regressed, would otherwise empty the index). An invocation event of
``{"dryRun": true}`` reports without deleting; the event can make a run safer,
never less safe.

Import boundary
---------------
Same image and closure as the consumer: ``kb_backend``, the archive's pure
document modules, and stdlib. ``boto3`` is imported inside functions.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from apis.shared.conversation_archive.documents import ARCHIVE_PREFIX, parse_archive_key
from apis.shared.conversation_archive.index_documents import (
    index_document_id,
    is_legacy_index_document_id,
    parse_index_document_id,
)
from apis.shared.conversation_archive.retention import retention_cutoff, retention_days
from apis.shared.kb_backend.reserved import CONVERSATIONS_KB_ID

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BUCKET_ENV_VAR = "CONVERSATION_ARCHIVE_BUCKET_NAME"

# ── Tunables, read at call time so tests can override them ───────────────────

#: Documents deleted per run at most. 2,000 is 200 calls, a few minutes at the
#: default pace; a larger backlog (the first run after a long flag-off period)
#: drains over the following days.
MAX_DOCUMENT_DELETES_PER_RUN = 2000

#: Archive objects deleted per run at most. Lifecycle expiry does the bulk of
#: this; the pass is a backstop, so the cap is generous.
MAX_OBJECT_DELETES_PER_RUN = 20000

#: Seconds between ``DeleteKnowledgeBaseDocuments`` calls. One call in flight
#: at a time, one a second: a tenth of the account's delete quota.
DELETE_PAUSE_SECONDS = 1.0

#: Bounds the document listing. A listing cut off here is incomplete, and an
#: incomplete listing deletes nothing.
MAX_DOCUMENTS_LISTED = 1_000_000

#: Statuses that mean a delete is already under way.
_ALREADY_DELETING = frozenset({"DELETING", "DELETE_IN_PROGRESS"})

#: A deleted document stays in the listing as ``NOT_FOUND`` (observed on dev
#: 2026-10-07: every turn the consumer had deleted). It is already gone;
#: deleting it again would repeat every day and count it as an orphan forever.
_ALREADY_DELETED = frozenset({"NOT_FOUND"})

#: ``ListKnowledgeBaseDocuments`` page size. The SDK model allows 1,000, but a
#: managed knowledge base rejects anything above 100 with a ValidationException
#: (observed on dev 2026-10-07, the reconciler's first run).
_LIST_PAGE_SIZE = 100

#: ``DeleteObjects`` takes at most this many keys.
_S3_DELETE_BATCH = 1000

#: ``DeleteKnowledgeBaseDocuments`` takes at most this many documents.
_KB_DELETE_BATCH = 10


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"ignoring non-integer {name}={raw!r}")
        return default
    return value if value >= 0 else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning(f"ignoring non-numeric {name}={raw!r}")
        return default
    return value if value >= 0 else default


def max_document_deletes() -> int:
    return _env_int("CONVERSATION_RECONCILER_MAX_DOCUMENT_DELETES", MAX_DOCUMENT_DELETES_PER_RUN)


def max_object_deletes() -> int:
    return _env_int("CONVERSATION_RECONCILER_MAX_OBJECT_DELETES", MAX_OBJECT_DELETES_PER_RUN)


def delete_pause_seconds() -> float:
    return _env_float("CONVERSATION_RECONCILER_DELETE_PAUSE_SECONDS", DELETE_PAUSE_SECONDS)


# ── AWS plumbing (patched in tests) ──────────────────────────────────────────
def _region() -> Optional[str]:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")


def s3_client():
    import boto3

    return boto3.client("s3", region_name=_region())


def bedrock_agent_client():
    import boto3
    from botocore.config import Config

    # Adaptive retries back off on throttling, the failure a shared quota produces.
    return boto3.client(
        "bedrock-agent",
        region_name=_region(),
        config=Config(retries={"mode": "adaptive", "max_attempts": 8}),
    )


def locate_conversations_kb() -> Optional[Tuple[str, str]]:
    """``(awsKbId, awsDataSourceId)`` from the KB_Record, or None if not provisioned."""
    from apis.shared.kb_backend.records import get_kb_record

    record = get_kb_record(CONVERSATIONS_KB_ID, CONVERSATIONS_KB_ID)
    if not record or not record.get("awsKbId") or not record.get("awsDataSourceId"):
        return None
    return record["awsKbId"], record["awsDataSourceId"]


# ── Report ───────────────────────────────────────────────────────────────────
@dataclass
class ReconcileReport:
    """What one run found and did. Counts only: no user ids, no text."""

    dry_run: bool = False
    retention_days: int = 0
    cutoff: str = ""
    kb_provisioned: bool = False
    documents_listed: int = 0
    documents_already_deleting: int = 0
    documents_already_deleted: int = 0
    legacy_documents: int = 0
    archive_objects: int = 0
    expired_objects: int = 0
    expired_objects_deleted: int = 0
    orphan_documents: int = 0
    orphan_documents_deleted: int = 0
    limit_reached: bool = False
    aborted: Optional[str] = None
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": "dry-run" if self.dry_run else "live",
            "retentionDays": self.retention_days,
            "cutoff": self.cutoff,
            "kbProvisioned": self.kb_provisioned,
            "documentsListed": self.documents_listed,
            "documentsAlreadyDeleting": self.documents_already_deleting,
            "documentsAlreadyDeleted": self.documents_already_deleted,
            "legacyDocuments": self.legacy_documents,
            "archiveObjects": self.archive_objects,
            "expiredObjects": self.expired_objects,
            "expiredObjectsDeleted": self.expired_objects_deleted,
            "orphanDocuments": self.orphan_documents,
            "orphanDocumentsDeleted": self.orphan_documents_deleted,
            "limitReached": self.limit_reached,
            "aborted": self.aborted,
            "errors": self.errors,
        }


class ListingIncomplete(RuntimeError):
    """A listing that could not be completed; the run must not act on it."""


# ── Listings ─────────────────────────────────────────────────────────────────
def list_document_ids(client: Any, kb_id: str, data_source_id: str) -> Tuple[Set[str], int, int]:
    """Live conversation document ids, how many are being deleted, and how many already are.

    Ids that are not conversation ids, current or legacy, are ignored (the
    knowledge base only holds conversation turns, but nothing here should act
    on an id it did not mint). Raises :class:`ListingIncomplete` past
    :data:`MAX_DOCUMENTS_LISTED`.
    """
    ids: Set[str] = set()
    deleting = 0
    deleted = 0
    token: Optional[str] = None
    while True:
        params: Dict[str, Any] = {
            "knowledgeBaseId": kb_id,
            "dataSourceId": data_source_id,
            "maxResults": _LIST_PAGE_SIZE,
        }
        if token:
            params["nextToken"] = token
        page = client.list_knowledge_base_documents(**params)
        for detail in page.get("documentDetails") or []:
            document_id = ((detail.get("identifier") or {}).get("custom") or {}).get("id") or ""
            if parse_index_document_id(document_id) is None and not is_legacy_index_document_id(document_id):
                continue
            status = str(detail.get("status") or "")
            if status in _ALREADY_DELETING:
                deleting += 1
                continue
            if status in _ALREADY_DELETED:
                deleted += 1
                continue
            ids.add(document_id)
        if len(ids) + deleting + deleted > MAX_DOCUMENTS_LISTED:
            raise ListingIncomplete(f"more than {MAX_DOCUMENTS_LISTED} documents listed")
        token = page.get("nextToken")
        if not token:
            return ids, deleting, deleted


@dataclass(frozen=True)
class ArchiveObject:
    key: str
    document_id: str
    last_modified: datetime


def list_archive(s3: Any, bucket: str) -> List[ArchiveObject]:
    """Every archived turn under ``conversations/``. Keys that are not turns are skipped."""
    out: List[ArchiveObject] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=ARCHIVE_PREFIX):
        for obj in page.get("Contents") or []:
            parsed = parse_archive_key(obj["Key"])
            if parsed is None:
                continue
            user_id, session_id, message_index = parsed
            modified = obj["LastModified"]
            if modified.tzinfo is None:
                modified = modified.replace(tzinfo=timezone.utc)
            out.append(ArchiveObject(obj["Key"], index_document_id(user_id, session_id, message_index), modified))
    return out


# ── Deletes ──────────────────────────────────────────────────────────────────
def delete_archive_objects(s3: Any, bucket: str, keys: List[str]) -> Tuple[Set[str], List[str]]:
    """Delete ``keys``; returns (keys deleted, error strings)."""
    deleted: Set[str] = set()
    errors: List[str] = []
    for start in range(0, len(keys), _S3_DELETE_BATCH):
        batch = keys[start : start + _S3_DELETE_BATCH]
        try:
            response = s3.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
        except Exception as exc:  # noqa: BLE001 - reported, and the next run retries
            errors.append(f"DeleteObjects: {type(exc).__name__}")
            continue
        failed = {error.get("Key") for error in response.get("Errors") or []}
        if failed:
            errors.append(f"DeleteObjects left {len(failed)} object(s)")
        deleted.update(key for key in batch if key not in failed)
    return deleted, errors


def delete_documents(
    client: Any,
    kb_id: str,
    data_source_id: str,
    document_ids: List[str],
    *,
    pause_seconds: float,
    sleep: Callable[[float], None],
) -> Tuple[int, List[str]]:
    """Delete documents ten at a time, one call in flight, pausing between calls."""
    from apis.shared.kb_backend.managed_backend import document_identifier

    deleted = 0
    errors: List[str] = []
    for start in range(0, len(document_ids), _KB_DELETE_BATCH):
        if start:
            sleep(pause_seconds)
        batch = document_ids[start : start + _KB_DELETE_BATCH]
        try:
            client.delete_knowledge_base_documents(
                knowledgeBaseId=kb_id,
                dataSourceId=data_source_id,
                documentIdentifiers=[document_identifier(document_id) for document_id in batch],
            )
        except Exception as exc:  # noqa: BLE001 - reported, and the next run retries
            errors.append(f"DeleteKnowledgeBaseDocuments: {type(exc).__name__}")
            continue
        deleted += len(batch)
    return deleted, errors


# ── The pass ─────────────────────────────────────────────────────────────────
def reconcile(
    *,
    bucket: str,
    s3: Any,
    client: Any,
    locate: Callable[[], Optional[Tuple[str, str]]] = locate_conversations_kb,
    now: Optional[datetime] = None,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> ReconcileReport:
    """One pass. See the module docstring for the rules it follows."""
    days = retention_days()
    cutoff = retention_cutoff(now, days)
    report = ReconcileReport(dry_run=dry_run, retention_days=days, cutoff=cutoff.isoformat())

    # 1. The knowledge base, first (see "Order matters").
    located = locate()
    documents: Set[str] = set()
    if located is not None:
        report.kb_provisioned = True
        try:
            (
                documents,
                report.documents_already_deleting,
                report.documents_already_deleted,
            ) = list_document_ids(client, *located)
        except Exception as exc:  # noqa: BLE001 - never act on a partial listing
            report.aborted = f"knowledge base listing failed: {type(exc).__name__}"
            return report
        report.documents_listed = len(documents)
        report.legacy_documents = sum(1 for document_id in documents if is_legacy_index_document_id(document_id))

    # 2. The archive.
    try:
        archive = list_archive(s3, bucket)
    except Exception as exc:  # noqa: BLE001 - never act on a partial listing
        report.aborted = f"archive listing failed: {type(exc).__name__}"
        return report
    report.archive_objects = len(archive)

    if not archive and documents:
        report.aborted = "archive listed empty while the knowledge base holds documents"
        return report

    # 3. Objects past retention. Their documents join the orphans below, so
    #    they leave the index this run whether or not the consumer is on.
    expired = [obj for obj in archive if obj.last_modified < cutoff]
    report.expired_objects = len(expired)
    object_cap = max_object_deletes()
    if len(expired) > object_cap:
        report.limit_reached = True
        expired = expired[:object_cap]

    expired_keys: Set[str] = set()
    if expired and not dry_run:
        expired_keys, errors = delete_archive_objects(s3, bucket, [obj.key for obj in expired])
        report.expired_objects_deleted = len(expired_keys)
        report.errors.extend(errors)
    elif dry_run:
        expired_keys = {obj.key for obj in expired}

    present = {obj.document_id for obj in archive if obj.key not in expired_keys}

    # 4. Documents with no object.
    orphans = sorted(documents - present)
    report.orphan_documents = len(orphans)
    document_cap = max_document_deletes()
    if len(orphans) > document_cap:
        report.limit_reached = True
        orphans = orphans[:document_cap]

    if orphans and located is not None and not dry_run:
        deleted, errors = delete_documents(
            client, *located, orphans, pause_seconds=delete_pause_seconds(), sleep=sleep
        )
        report.orphan_documents_deleted = deleted
        report.errors.extend(errors)

    return report


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Scheduled entry point (EventBridge, once a day)."""
    bucket = os.environ.get(BUCKET_ENV_VAR, "").strip()
    if not bucket:
        raise RuntimeError(f"{BUCKET_ENV_VAR} is not set")
    dry_run = bool((event or {}).get("dryRun"))

    report = reconcile(
        bucket=bucket,
        s3=s3_client(),
        client=bedrock_agent_client(),
        now=datetime.now(timezone.utc),
        dry_run=dry_run,
    )
    result = report.to_dict()
    logger.info(f"conversation index reconcile: {result}")
    if report.aborted:
        # Raise so the Lambda error alarm sees it; nothing was deleted.
        raise RuntimeError(f"conversation index reconcile aborted: {report.aborted}")
    return result


__all__ = [
    "ArchiveObject",
    "ListingIncomplete",
    "ReconcileReport",
    "delete_archive_objects",
    "delete_documents",
    "lambda_handler",
    "list_archive",
    "list_document_ids",
    "reconcile",
]
