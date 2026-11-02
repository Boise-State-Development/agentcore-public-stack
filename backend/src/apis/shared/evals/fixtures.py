"""Register a synthetic file as a READY session upload, the way a completed SPA upload is.

Moved here from ``scripts/smoke_turns.py`` because two scripts now need it
(the smoke matrix's document rows and the eval harness's ``document_qa``
family). The reason it exists is the smoke script's: the SPA presigns, PUTs to
S3 and completes the upload, then sends ``file_upload_ids``, never base64
``files``. That difference decides whether ``document_read`` and
``verify_sources`` exist on the turn — ``turn_has_document`` is classified from
the *resolved uploads* only, and inline base64 never produces an upload row for
a document. So a turn that needs the document path writes what a completed
upload writes: the S3 object at the conventional key, and a READY row.

Needs AWS credentials and ``S3_USER_FILES_BUCKET_NAME`` /
``DYNAMODB_USER_FILES_TABLE_NAME``, which the scripts export from the target's
SSM parameters.

A large (reference-class) document goes one step further, the step app-api's
``FileUploadService._enqueue_preparation`` takes after a real upload: the row is
marked ``preparation = pending`` and ``{user_id, upload_id}`` is sent to the
deployed prepare queue (``LARGE_DOCUMENT_PREPARE_QUEUE_URL``). The deployed
Lambda then builds the sidecars and, with the attachments KB on, ingests the
document, exactly as for a user's upload. ``wait_for_documents`` polls the rows
until each is ready to ask about. Nothing here reimplements preparation.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import time
import uuid
from pathlib import Path
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

#: Extensions the case fixtures use, pinned rather than left to the host's
#: ``mimetypes`` table, which differs between macOS and the CI image for ``.md``.
_MIME_BY_SUFFIX = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".html": "text/html",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class FixtureError(RuntimeError):
    """The environment cannot store a fixture upload."""


def mime_type_for(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return _MIME_BY_SUFFIX.get(suffix) or mimetypes.guess_type(filename)[0] or "application/octet-stream"


def load_fixture(fixtures_dir: Path, filename: str) -> Tuple[bytes, str]:
    path = fixtures_dir / filename
    if not path.is_file():
        raise FixtureError(f"fixture {filename} not found in {fixtures_dir}")
    return path.read_bytes(), mime_type_for(filename)


def upload_key(user_id: str, session_id: str, upload_id: str, filename: str) -> str:
    return f"user-files/{user_id}/{session_id}/{upload_id}/{filename}"


async def register_upload(user_id: str, session_id: str, filename: str, mime_type: str, data: bytes) -> str:
    """Store ``data`` as a READY upload in ``session_id`` and return its ``upload_id``."""
    import boto3

    from apis.shared.files.models import FileMetadata, FileStatus
    from apis.shared.files.repository import get_file_upload_repository

    bucket = os.environ.get("S3_USER_FILES_BUCKET_NAME", "")
    if not bucket:
        raise FixtureError("S3_USER_FILES_BUCKET_NAME is not set; needs --prefix (deployed) or a local stack")
    upload_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}"
    key = upload_key(user_id, session_id, upload_id, filename)
    await asyncio.to_thread(
        boto3.client("s3").put_object, Bucket=bucket, Key=key, Body=data, ContentType=mime_type
    )
    await get_file_upload_repository().create_file(
        FileMetadata(
            upload_id=upload_id,
            user_id=user_id,
            session_id=session_id,
            filename=filename,
            mime_type=mime_type,
            size_bytes=len(data),
            s3_key=key,
            s3_bucket=bucket,
            status=FileStatus.READY,
        )
    )
    return upload_id


async def register_reference_upload(
    user_id: str, session_id: str, filename: str, mime_type: str, data: bytes
) -> str:
    """Store ``data`` as a READY upload and queue its preparation, as app-api does for a large upload."""
    import json

    import boto3

    from apis.shared.files.large_document import STATUS_PENDING, preparation_record
    from apis.shared.files.repository import get_file_upload_repository

    queue_url = os.environ.get("LARGE_DOCUMENT_PREPARE_QUEUE_URL", "").strip()
    if not queue_url:
        raise FixtureError("LARGE_DOCUMENT_PREPARE_QUEUE_URL is not set; does the target run large-document attachments?")
    upload_id = await register_upload(user_id, session_id, filename, mime_type, data)
    # Pending before the message, so the Lambda's ``ready`` can never be overwritten.
    await get_file_upload_repository().update_file_preparation(
        user_id, upload_id, preparation_record(status=STATUS_PENDING)
    )
    body = json.dumps({"user_id": user_id, "upload_id": upload_id})
    await asyncio.to_thread(boto3.client("sqs").send_message, QueueUrl=queue_url, MessageBody=body)
    return upload_id


#: A prepared document with no ``kbIndex`` yet is about to be ingested by the
#: same Lambda run (``attachment_index.search_status``). One still without it
#: after this long belongs to a deployment with the attachments KB off.
KB_GRACE_SECONDS = 60.0
_KB_TERMINAL = ("indexed", "failed", "skipped_cap", "removed", "evicted")


def document_state(meta: Any) -> Dict[str, Any]:
    """What a waiting run needs from one upload row: preparation, KB status, text-searchable."""
    from apis.shared.files import large_document
    from apis.shared.files.attachment_index import is_text_indexed, kb_status

    if meta is None:
        return {"preparation": "missing", "kbIndex": None, "textIndexed": False}
    return {
        "preparation": large_document.preparation_status(meta),
        "kbIndex": kb_status(meta),
        "textIndexed": bool(is_text_indexed(meta)),
    }


def document_settled(state: Dict[str, Any], *, seconds_since_prepared: Optional[float]) -> bool:
    """Ready to ask about: prepared and searchable, or in a state that will not change.

    A failed preparation is settled too: the turn then gets what a user would
    get, and the sample's answer says whether that was good enough.
    """
    if state["preparation"] in ("failed", "missing"):
        return True
    if state["preparation"] != "ready":
        return False
    if state["kbIndex"] in _KB_TERMINAL or state["textIndexed"]:
        return True
    return state["kbIndex"] is None and (seconds_since_prepared or 0.0) >= KB_GRACE_SECONDS


async def wait_for_documents(
    user_id: str,
    upload_ids: Sequence[str],
    *,
    timeout: float,
    poll: float = 15.0,
    echo: Optional[Callable[[Dict[str, Dict[str, Any]]], None]] = None,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Dict[str, Dict[str, Any]]:
    """Poll each upload's row until every one is settled or ``timeout`` passes.

    Returns ``{upload_id: state}`` with ``settled`` and ``waitedSeconds`` added.
    An unsettled document is not an error: the turn goes ahead with whatever
    the document has by then, which is what a user asking early would get.
    """
    from apis.shared.files.repository import get_file_upload_repository

    repository = get_file_upload_repository()
    start = clock()
    prepared_at: Dict[str, float] = {}
    while True:
        now = clock()
        states: Dict[str, Dict[str, Any]] = {}
        for upload_id in upload_ids:
            state = document_state(await repository.get_file(user_id, upload_id))
            if state["preparation"] == "ready":
                prepared_at.setdefault(upload_id, now)
            since = now - prepared_at[upload_id] if upload_id in prepared_at else None
            state["settled"] = document_settled(state, seconds_since_prepared=since)
            state["waitedSeconds"] = round(now - start)
            states[upload_id] = state
        if echo is not None:
            echo(states)
        if all(s["settled"] for s in states.values()) or now - start >= timeout:
            return states
        await sleep(poll)


async def delete_upload(user_id: str, upload_id: str) -> bool:
    """Remove an upload this harness registered: the row, then its S3 objects. ``False`` if it was already gone."""
    from apis.shared.files.repository import get_file_upload_repository

    meta = await get_file_upload_repository().delete_file(user_id, upload_id)
    if meta is None:
        return False
    # Everything under the upload's prefix: the original, and for a prepared
    # document its ``_derived/`` sidecars. The delete events take a document
    # out of the attachments KB, as for a user's own delete.
    await delete_objects_under(meta.s3_bucket, meta.s3_key.rsplit("/", 1)[0] + "/")
    return True


async def delete_objects_under(bucket: str, prefix: str) -> int:
    """Delete every object under ``prefix``; returns how many."""
    import boto3

    s3 = boto3.client("s3")
    deleted = 0
    token: Optional[str] = None
    while True:
        kwargs: Dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = await asyncio.to_thread(s3.list_objects_v2, **kwargs)
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            await asyncio.to_thread(s3.delete_objects, Bucket=bucket, Delete={"Objects": keys})
            deleted += len(keys)
        if not page.get("IsTruncated"):
            return deleted
        token = page.get("NextContinuationToken")


def stale_uploads(objects: Iterable[Mapping[str, Any]], user_id: str, cutoff: datetime) -> List[Tuple[str, str]]:
    """``(session_id, upload_id)`` of each upload under ``user_id`` whose newest object predates ``cutoff``.

    ``objects`` is an S3 listing (``Key``, ``LastModified``). Grouping by the
    upload's prefix and taking the newest object means an upload the prepare
    Lambda is still writing sidecars for is never stale.
    """
    newest: Dict[Tuple[str, str], datetime] = {}
    root = f"user-files/{user_id}/"
    for obj in objects:
        key = str(obj.get("Key") or "")
        if not key.startswith(root):
            continue
        parts = key[len(root):].split("/")
        if len(parts) < 3:
            continue
        upload = (parts[0], parts[1])
        modified = obj["LastModified"]
        if upload not in newest or modified > newest[upload]:
            newest[upload] = modified
    return sorted(upload for upload, modified in newest.items() if modified < cutoff)
