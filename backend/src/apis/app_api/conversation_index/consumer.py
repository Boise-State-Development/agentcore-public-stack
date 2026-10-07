"""Conversation-search index consumer: archive objects in, KB documents out.

docs/specs/conversation-search.md §4, PR-2b. The conversation archive bucket raises
an S3 *Object Created* or *Object Deleted* event on EventBridge for every turn the
runtime archives and every turn a session delete or the retention lifecycle rule
removes. Two rules put those events on an SQS queue, and this Lambda drains it in
batches of up to ten, keeping the shared ``conversations`` Managed Knowledge Base in
step with the archive: one document per archived turn, none for a turn that is gone.

Why a queue, when the kb-migration consumer is invoked by EventBridge directly
------------------------------------------------------------------------------
Batching. A session delete is one ``DeleteObjects`` call that raises one *Object
Deleted* per turn at the same instant, and lifecycle expiry arrives in daily
bursts. Invoked directly, each would be its own Lambda and its own one-document
``DeleteKnowledgeBaseDocuments`` call, at unbounded concurrency, against document
quotas (20 ingest/s, 10 delete/s, 10 concurrent operations) that are **per account**
and shared with every agent's document uploads. Through the queue, ten events
become one call, and the event source mapping's ``maximumConcurrency`` (2) bounds
this consumer to at most two calls in flight, so a bulk delete cannot starve a user
uploading a document. The spec records the arithmetic (§4, "Consumer shape").

Every event means "make the index match the archive for this key"
-----------------------------------------------------------------
The event type is only a hint. For each distinct key in a batch the consumer reads
the object as it is *now*: present → ingest its text (re-ingesting an id replaces
the document), absent → delete the document. That makes the outcome independent of
the order events arrive in, and of redelivery. Three cases that would otherwise be
bugs:

* A retried or redriven *Object Created* for a turn deleted since does not bring
  the deleted text back: the object is gone, so it deletes instead.
* A user who deletes a conversation the moment its last turn finishes produces a
  Created and a Deleted that can be processed concurrently. The ingest re-checks
  the object after the call returns and deletes the document if the object went
  away in between, so a deleted turn never stays indexed.
* An expiry (``reason: "Lifecycle Expiration"``) and a user delete are the same
  thing here; the reason is logged and nothing else.

Ownership comes from the key
----------------------------
``conversations/{user_id}/{session_id}/{index:06d}.json``. A key that does not
parse is not ours and is acknowledged untouched; a body whose ``userId``,
``sessionId`` or ``messageIndex`` disagrees with its key is refused rather than
indexed, so no malformed write can place one user's words behind another user's
``user_id`` filter.

Lazy provisioning of the one shared knowledge base
--------------------------------------------------
Created on the first ingest through the shared ``provision_managed_kb`` saga, with
the reserved app KB id ``conversations`` for both ``assistant_id`` and
``app_kb_id``, so its KB_Record lives at ``AST#conversations / KB#conversations``
(the record is what keeps the kb-migration reconciler from treating it as an
orphan). Measured 47–124 s to ACTIVE, then ~65 s of first-ingest warm-up that this
Lambda does not wait for (ingestion is accepted asynchronously). Two batches can
reach an unprovisioned knowledge base together; the one that loses the record race
waits here for the winner rather than creating a second knowledge base. A delete
never provisions: with no knowledge base there is nothing to delete.

The switch
----------
``CONVERSATION_INDEX_ENABLED`` (the same flag that gates the archive writes): off,
and every message is acknowledged with nothing read or written. The rules are also
created disabled while the flag is off, so in practice nothing arrives.

Import boundary
---------------
The image carries ``kb_backend`` (stdlib-only module scope), the archive's pure
document modules, the flag reader, and ``extract_records`` from the kb-migration
consumer. ``boto3`` is imported inside functions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote_plus

from apis.app_api.kb_migration.ingestion_consumer import extract_records
from apis.shared.conversation_archive.documents import ArchivedTurn, parse_archive_key
from apis.shared.conversation_archive.index_documents import (
    index_attributes,
    index_document_id,
    index_text,
    turn_matches_key,
)
from apis.shared.feature_flags import conversation_index_enabled
from apis.shared.kb_backend.reserved import CONVERSATIONS_KB_ID

logger = logging.getLogger()
logger.setLevel(logging.INFO)

#: The archive bucket this consumer serves. An event for any other bucket is
#: acknowledged and ignored.
BUCKET_ENV_VAR = "CONVERSATION_ARCHIVE_BUCKET_NAME"

#: Opaque owner recorded on the KB_Record and its ``ManagedKbOwnerUserId`` tag.
#: The knowledge base belongs to the platform, not to a user.
KB_OWNER = "system"

#: Written beside the tag contract's keys so the knowledge base's storage and
#: retrieval lines are attributable in Cost Explorer.
KB_PURPOSE_TAGS = {"purpose": "conversation-search"}

#: How long a batch that finds the knowledge base being provisioned by another
#: invocation waits for it. Covers the measured 124 s worst case to ACTIVE plus
#: the data source, with room; well inside the Lambda's 10-minute timeout.
PROVISIONING_WAIT_SECONDS = 360.0
PROVISIONING_POLL_SECONDS = 5.0

#: An archived turn is capped at 16 KB of text; anything far larger at an
#: archive key was not written by the archive, and is not read into memory.
MAX_OBJECT_BYTES = 64 * 1024

#: Bedrock control-plane retries. Adaptive mode backs off on throttling, which is
#: the failure the shared per-account document quotas produce.
BEDROCK_MAX_ATTEMPTS = 8


# ── AWS plumbing (patched in tests) ──────────────────────────────────────────
def _region() -> Optional[str]:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")


def s3_client():
    import boto3

    return boto3.client("s3", region_name=_region())


def bedrock_agent_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "bedrock-agent",
        region_name=_region(),
        config=Config(retries={"mode": "adaptive", "max_attempts": BEDROCK_MAX_ATTEMPTS}),
    )


def _is_missing(exc: BaseException) -> bool:
    """Whether an S3 error means the object is not there.

    Only a real 404. A 403 is a permissions fault, not evidence of a delete:
    treating it as one would empty the index the day a grant regressed.
    """
    response = getattr(exc, "response", None) or {}
    code = str((response.get("Error") or {}).get("Code") or "")
    status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    return code in ("NoSuchKey", "NotFound", "404") or status == 404


def read_archive_object(s3: Any, bucket: str, key: str) -> Optional[bytes]:
    """The object's bytes, or None when it does not exist. Other errors raise."""
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 - classified below
        if _is_missing(exc):
            return None
        raise
    body = response["Body"]
    try:
        if int(response.get("ContentLength") or 0) > MAX_OBJECT_BYTES:
            raise ValueError(f"archive object is larger than {MAX_OBJECT_BYTES} bytes")
        return body.read()
    finally:
        body.close()


def archive_object_exists(s3: Any, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 - classified below
        if _is_missing(exc):
            return False
        raise
    return True


# ── Provisioning ─────────────────────────────────────────────────────────────
def _provisioned(record: Optional[Dict[str, Any]]) -> bool:
    return bool(record) and bool(record.get("awsKbId")) and bool(record.get("awsDataSourceId"))


async def _wait_until_provisioned(
    sleep: Callable[[float], Awaitable[None]],
    wait_seconds: float,
    poll_seconds: float,
) -> bool:
    from apis.shared.kb_backend.records import get_kb_record

    waited = 0.0
    while True:
        record = await asyncio.to_thread(get_kb_record, CONVERSATIONS_KB_ID, CONVERSATIONS_KB_ID)
        if _provisioned(record):
            return True
        if waited >= wait_seconds:
            return False
        await sleep(poll_seconds)
        waited += poll_seconds


async def ensure_conversations_kb(
    client: Any,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    wait_seconds: Optional[float] = None,
    poll_seconds: Optional[float] = None,
    provision_kwargs: Optional[Dict[str, Any]] = None,
) -> None:
    """Make sure the shared knowledge base exists, creating it at most once.

    * Already provisioned: one record read, nothing else.
    * A record without both AWS ids: another invocation is provisioning (or one
      crashed mid-way). Wait for it; if it never finishes, resume through
      ``provision_managed_kb``, which reuses the record's persisted token.
    * No record: provision. Losing the record race raises
      ``ProvisioningInProgress``, and the loser waits for the winner.

    Raises when the knowledge base is still not ready at the end, so the batch
    is retried from the queue rather than ingesting into nothing.
    """
    from apis.shared.kb_backend.provisioning import ProvisioningInProgress, provision_managed_kb
    from apis.shared.kb_backend.records import get_kb_record

    wait = PROVISIONING_WAIT_SECONDS if wait_seconds is None else wait_seconds
    poll = PROVISIONING_POLL_SECONDS if poll_seconds is None else poll_seconds

    record = await asyncio.to_thread(get_kb_record, CONVERSATIONS_KB_ID, CONVERSATIONS_KB_ID)
    if _provisioned(record):
        return
    if record and await _wait_until_provisioned(sleep, wait, poll):
        return

    try:
        provisioned = await provision_managed_kb(
            CONVERSATIONS_KB_ID,
            CONVERSATIONS_KB_ID,
            KB_OWNER,
            client=client,
            extra_tags=KB_PURPOSE_TAGS,
            sleep=sleep,
            **(provision_kwargs or {}),
        )
    except ProvisioningInProgress:
        logger.info("conversation KB is being provisioned by another invocation; waiting")
        if not await _wait_until_provisioned(sleep, wait, poll):
            raise
        return
    if provisioned.created:
        logger.info(f"provisioned the conversation-search knowledge base {provisioned.aws_kb_id}")


# ── The sync ─────────────────────────────────────────────────────────────────
def _document_source(turn: ArchivedTurn):
    from apis.shared.kb_backend.protocol import DocumentSource

    return DocumentSource(
        document_id=index_document_id(turn.session_id, turn.message_index),
        # The shared payload builder writes `filename` as an attribute; the
        # object's leaf is the honest value and carries nothing identifying.
        filename=f"{turn.message_index:06d}.json",
        chunks=[index_text(turn)],
        metadata=index_attributes(turn),
    )


async def sync_keys(
    bucket: str,
    keys: List[str],
    *,
    s3: Any,
    backend: Any,
    ensure_kb: Callable[[], Awaitable[None]],
) -> Set[str]:
    """Bring the index in line with the archive for ``keys``; returns the keys
    that could not be synced (their messages are retried from the queue).

    Every key must already have passed :func:`parse_archive_key`.
    """
    from apis.shared.kb_backend.managed_backend import ManagedKbNotProvisioned

    failed: Set[str] = set()
    present: Dict[str, ArchivedTurn] = {}
    absent: List[str] = []

    for key in keys:
        user_id, session_id, message_index = parse_archive_key(key)  # type: ignore[misc]
        try:
            body = await asyncio.to_thread(read_archive_object, s3, bucket, key)
        except ValueError as exc:
            # Too large to be an archived turn. Not ours, and a retry reads the same bytes.
            logger.error(f"skipping archive object for session {session_id}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - one unreadable object must not sink the batch
            logger.warning(f"could not read archive object for session {session_id}: {exc}")
            failed.add(key)
            continue
        if body is None:
            absent.append(key)
            continue
        try:
            turn = ArchivedTurn.from_json(body)
        except ValueError as exc:
            logger.error(f"skipping malformed archive object for session {session_id}: {exc}")
            continue
        if not turn_matches_key(turn, user_id, session_id, message_index):
            # Indexing it would file this text under whichever user the body
            # names. Refused, not retried: a retry would refuse it again.
            logger.error(
                f"skipping archive object for session {session_id}: its body names a "
                f"different user, session or turn than its key"
            )
            continue
        present[key] = turn

    if present:
        try:
            await ensure_kb()
            await backend.ingest_documents(
                CONVERSATIONS_KB_ID, [_document_source(t) for t in present.values()]
            )
        except Exception as exc:  # noqa: BLE001 - reported per message below
            logger.error(f"ingest of {len(present)} conversation turn(s) failed: {exc}", exc_info=True)
            failed.update(present)
            present = {}

        # A delete that landed between the read above and the ingest returning
        # would otherwise leave the document behind for good.
        for key in present:
            try:
                still_there = await asyncio.to_thread(archive_object_exists, s3, bucket, key)
            except Exception:  # noqa: BLE001 - the delete event re-checks this key anyway
                continue
            if not still_there:
                absent.append(key)

    if absent:
        document_ids = []
        for key in absent:
            _, session_id, message_index = parse_archive_key(key)  # type: ignore[misc]
            document_ids.append(index_document_id(session_id, message_index))
        try:
            await backend.delete_documents(CONVERSATIONS_KB_ID, document_ids)
        except ManagedKbNotProvisioned:
            # No knowledge base, so nothing was ever indexed. Never provision to delete.
            pass
        except Exception as exc:  # noqa: BLE001 - reported per message below
            logger.error(f"delete of {len(absent)} conversation turn(s) failed: {exc}", exc_info=True)
            failed.update(absent)

    logger.info(
        f"conversation index sync: ingested={len(present)} deleted={len(absent)} "
        f"failed={len(failed)}"
    )
    return failed


def _keys_of(message: Dict[str, Any], bucket: str) -> List[Tuple[str, str]]:
    """``(key, detail-type)`` pairs this message asks about, in our bucket."""
    try:
        event = json.loads(message.get("body") or "")
    except (TypeError, ValueError):
        logger.warning("skipping a queue message whose body is not JSON")
        return []
    if not isinstance(event, dict):
        return []

    detail_type = str(event.get("detail-type") or "")
    reason = str((event.get("detail") or {}).get("reason") or "")
    out: List[Tuple[str, str]] = []
    for record in extract_records(event):
        # URL-decoded as the kb-migration consumer does; archive keys hold only
        # opaque ids, so the decode is a no-op for every key the archive writes.
        key = unquote_plus(record.get("key") or "")
        if record.get("bucket") != bucket:
            logger.warning("skipping an event for a bucket this consumer does not serve")
            continue
        if parse_archive_key(key) is None:
            logger.warning("skipping an event whose key is not an archived turn")
            continue
        if reason:
            logger.info(f"{detail_type} ({reason})")
        out.append((key, detail_type))
    return out


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """SQS entry point with partial batch responses.

    Only the messages whose keys failed to sync are reported back, so one bad
    turn retries alone instead of taking nine good ones with it.
    """
    messages = event.get("Records") or []
    if not conversation_index_enabled():
        logger.info(f"CONVERSATION_INDEX_ENABLED is off; acknowledging {len(messages)} message(s)")
        return {"batchItemFailures": []}

    bucket = os.environ.get(BUCKET_ENV_VAR, "").strip()
    if not bucket:
        raise RuntimeError(f"{BUCKET_ENV_VAR} is not set")

    keys_by_message: Dict[str, List[str]] = {}
    distinct: List[str] = []
    for message in messages:
        message_id = str(message.get("messageId") or "")
        keys = [key for key, _ in _keys_of(message, bucket)]
        keys_by_message[message_id] = keys
        for key in keys:
            if key not in distinct:
                distinct.append(key)

    if not distinct:
        return {"batchItemFailures": []}

    from apis.shared.kb_backend.managed_backend import ManagedKbBackend

    client = bedrock_agent_client()
    failed = asyncio.run(
        sync_keys(
            bucket,
            distinct,
            s3=s3_client(),
            backend=ManagedKbBackend(agent_client=client),
            ensure_kb=lambda: ensure_conversations_kb(client),
        )
    )
    return {
        "batchItemFailures": [
            {"itemIdentifier": message_id}
            for message_id, keys in keys_by_message.items()
            if any(key in failed for key in keys)
        ]
    }


__all__ = [
    "BUCKET_ENV_VAR",
    "KB_OWNER",
    "KB_PURPOSE_TAGS",
    "archive_object_exists",
    "ensure_conversations_kb",
    "lambda_handler",
    "read_archive_object",
    "sync_keys",
]
