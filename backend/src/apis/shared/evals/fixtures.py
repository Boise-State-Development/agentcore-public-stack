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
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import time
import uuid
from pathlib import Path
from typing import Tuple

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


async def delete_upload(user_id: str, upload_id: str) -> bool:
    """Remove an upload this harness registered: the S3 object, then the row. ``False`` if it was already gone."""
    import boto3

    from apis.shared.files.repository import get_file_upload_repository

    meta = await get_file_upload_repository().delete_file(user_id, upload_id)
    if meta is None:
        return False
    await asyncio.to_thread(boto3.client("s3").delete_object, Bucket=meta.s3_bucket, Key=meta.s3_key)
    return True
