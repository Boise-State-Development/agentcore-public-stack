"""Seeding, waiting and cleanup against a deployed environment (dev).

The fixtures are seeded the way a completed upload leaves them, then handed to
the **deployed** pipeline: a READY row and the original in the user-files bucket,
``preparation = pending``, and the ``{user_id, upload_id}`` message app-api sends
(``FileUploadService._enqueue_preparation``). The prepare Lambda then builds the
sidecars and, with the attachments KB on, ingests the original, exactly as for a
real upload. Nothing here reimplements preparation or indexing.

Each document gets its own conversation under one synthetic harness user, so a
session-wide search and the document listing see only that document. The user
id names no person. Quota is not touched: the seed writes no usage, so cleanup
releases none.

Cleanup deletes the rows and every object under each upload's prefix. The S3
delete events take the documents out of the knowledge base and release their
bytes, as for a user's own delete.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

HARNESS_USER_ID = "ldq-harness"


def configure_environment(prefix: str, region: str) -> Dict[str, str]:
    """Point the shared code at the deployment: derived table and bucket names,
    resolved exactly as the Runtime resolves them."""
    import boto3

    os.environ["PROJECT_PREFIX"] = prefix
    os.environ["AWS_REGION"] = region
    os.environ.setdefault("AWS_DEFAULT_REGION", region)
    if not os.environ.get("AWS_ACCOUNT_ID"):
        os.environ["AWS_ACCOUNT_ID"] = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    from apis.shared.config.runtime_environment import hydrate_derived_environment

    hydrate_derived_environment()
    return {
        "bucket": os.environ["S3_USER_FILES_BUCKET_NAME"],
        "table": os.environ["DYNAMODB_USER_FILES_TABLE_NAME"],
        "queue": f"{prefix}-large-document-prepare",
    }


def _upload_id() -> str:
    timestamp_hex = format(int(datetime.now(timezone.utc).timestamp() * 1000), "x")
    return f"{timestamp_hex}_{uuid.uuid4().hex[:16]}"


async def seed(fixtures: List[Any], run_id: str, region: str, resources: Dict[str, str]) -> List[Dict[str, Any]]:
    """Upload each fixture, write its row, and queue its preparation."""
    import json

    import boto3

    from apis.shared.files.large_document import STATUS_PENDING, preparation_record
    from apis.shared.files.models import FileMetadata, FileStatus
    from apis.shared.files.repository import get_file_upload_repository

    s3 = boto3.client("s3", region_name=region)
    sqs = boto3.client("sqs", region_name=region)
    queue_url = sqs.get_queue_url(QueueName=resources["queue"])["QueueUrl"]
    repository = get_file_upload_repository()

    seeded: List[Dict[str, Any]] = []
    for fixture in fixtures:
        session_id = f"ldq-{run_id}-{fixture.name}"
        upload_id = _upload_id()
        key = f"user-files/{HARNESS_USER_ID}/{session_id}/{upload_id}/{fixture.filename}"
        s3.put_object(Bucket=resources["bucket"], Key=key, Body=fixture.data, ContentType=fixture.mime_type)
        meta = FileMetadata(
            upload_id=upload_id, user_id=HARNESS_USER_ID, session_id=session_id, filename=fixture.filename,
            mime_type=fixture.mime_type, size_bytes=len(fixture.data), s3_key=key, s3_bucket=resources["bucket"],
            status=FileStatus.READY,
        )
        await repository.create_file(meta)
        await repository.update_file_preparation(HARNESS_USER_ID, upload_id, preparation_record(status=STATUS_PENDING))
        sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps({"user_id": HARNESS_USER_ID, "upload_id": upload_id}))
        seeded.append({
            "name": fixture.name, "docClass": fixture.doc_class, "userId": HARNESS_USER_ID,
            "sessionId": session_id, "uploadId": upload_id, "key": key, "bucket": resources["bucket"],
            "sizeBytes": len(fixture.data), "seededAt": time.time(),
        })
    return seeded


async def statuses(seeded: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Each document's preparation, kbIndex and searchability, read off its row."""
    from apis.shared.files import large_document
    from apis.shared.files.attachment_index import is_text_indexed, kb_status, search_status
    from apis.shared.files.repository import get_file_upload_repository

    repository = get_file_upload_repository()
    out = []
    for doc in seeded:
        meta = await repository.get_file(doc["userId"], doc["uploadId"])
        out.append({
            "name": doc["name"],
            "preparation": large_document.preparation_status(meta) if meta else "missing",
            "kbIndex": kb_status(meta) if meta else None,
            "textIndexed": bool(meta and is_text_indexed(meta)),
            "searchStatus": search_status(meta, kb_enabled=True) if meta else "missing",
            "elapsedSeconds": round(time.time() - doc["seededAt"]),
        })
    return out


def is_settled(row: Dict[str, Any], *, full: bool) -> bool:
    """Ready to ask: prepared, and indexed (fully, or text-searchable when ``full`` is off)."""
    if row["preparation"] == "failed" or row["kbIndex"] in ("failed", "skipped_cap", "removed"):
        return True
    if row["preparation"] != "ready":
        return False
    return row["kbIndex"] == "indexed" or (not full and row["textIndexed"])


async def wait(seeded: List[Dict[str, Any]], *, full: bool, timeout: float, poll: float, echo) -> List[Dict[str, Any]]:
    deadline = time.time() + timeout
    while True:
        rows = await statuses(seeded)
        echo(rows)
        if all(is_settled(r, full=full) for r in rows) or time.time() > deadline:
            return rows
        await asyncio.sleep(poll)


async def cleanup(seeded: List[Dict[str, Any]], region: str) -> int:
    """Delete each upload's row, then every object under its prefix (original,
    ``_derived/``, thumbnail). Returns the number of objects deleted."""
    import boto3

    from apis.shared.files.repository import get_file_upload_repository

    s3 = boto3.client("s3", region_name=region)
    repository = get_file_upload_repository()
    deleted = 0
    for doc in seeded:
        await repository.delete_file(doc["userId"], doc["uploadId"])
        prefix = doc["key"].rsplit("/", 1)[0] + "/"
        token: Optional[str] = None
        while True:
            kwargs: Dict[str, Any] = {"Bucket": doc["bucket"], "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = s3.list_objects_v2(**kwargs)
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                s3.delete_objects(Bucket=doc["bucket"], Delete={"Objects": keys})
                deleted += len(keys)
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
    return deleted
