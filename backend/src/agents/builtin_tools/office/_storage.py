"""Shared storage + Code Interpreter helpers for the office document tools.

Both the Word (``word_document_tool``) and Excel (``excel_spreadsheet_tool``)
toolsets build a binary Office file inside AWS Bedrock Code Interpreter and
persist it to the existing user-files store (``apis.shared.files``): the file
lands in ``S3_USER_FILES_BUCKET_NAME`` with a ``FileMetadata`` row (status
READY) in ``DYNAMODB_USER_FILES_TABLE_NAME``, so it appears in the chat's Files
panel and is downloadable via the app-api ``/files/{id}/preview-url`` route.

The two toolsets differ only in the document format (``.docx`` vs ``.xlsx``)
and the library used inside the sandbox (python-docx vs openpyxl); everything
about talking to Code Interpreter and the user-files store is identical, so it
lives here to avoid drift.

Design notes
------------
* Code Interpreter usage mirrors ``code_interpreter_diagram_tool.py`` — the
  interpreter id is resolved from ``AGENTCORE_CODE_INTERPRETER_ID`` (or SSM),
  a session is started with ``CodeInterpreter(region).start(identifier=...)``,
  and always stopped in a ``finally`` block by the caller.
* Storage resolves the user-files bucket's real region via ``HeadBucket``
  (NOT ``AWS_REGION``, which the AgentCore Runtime does not reliably pin to the
  bucket region) and does not hard-pin ``endpoint_url`` so botocore can still
  auto-correct the region. ``S3_USER_FILES_BUCKET_NAME`` is required; the
  helpers fail loudly when it is unset rather than targeting a bogus default
  bucket the runtime has no access to.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Presigned download links are short-lived; long enough for the user to click.
_DOWNLOAD_URL_TTL = 60 * 60  # 1 hour


class _DocGenError(Exception):
    """Raised when Code Interpreter fails to run the document code."""


class _StorageNotConfiguredError(Exception):
    """Raised when the user-files S3 bucket is not configured for the runtime."""


def _region() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )


def _get_code_interpreter_id() -> Optional[str]:
    """Resolve the Custom Code Interpreter id (env first, then SSM)."""
    ci_id = os.getenv("AGENTCORE_CODE_INTERPRETER_ID")
    if ci_id:
        return ci_id
    try:
        project_name = os.getenv("PROJECT_NAME", "strands-agent-chatbot")
        environment = os.getenv("ENVIRONMENT", "dev")
        ssm = boto3.client("ssm", region_name=_region())
        resp = ssm.get_parameter(
            Name=f"/{project_name}/{environment}/agentcore/code-interpreter-id"
        )
        return resp["Parameter"]["Value"]
    except Exception as exc:  # pragma: no cover - best-effort fallback
        logger.warning(f"Code Interpreter id not found in env or SSM: {exc}")
        return None


def _validate_document_name(name: str) -> Tuple[bool, Optional[str]]:
    """Validate a document name (without extension).

    Rules: letters, numbers, hyphens and underscores only; no spaces or other
    special characters; no consecutive, leading, or trailing hyphens.
    """
    if not name:
        return False, "Document name cannot be empty"

    if not re.match(r"^[a-zA-Z0-9_\-]+$", name):
        invalid = sorted(set(re.findall(r"[^a-zA-Z0-9_\-]", name)))
        return (
            False,
            f"Invalid characters in name: {invalid}. Use only letters, "
            "numbers, hyphens, and underscores.",
        )
    if "--" in name:
        return False, "Name cannot contain consecutive hyphens (--)"
    if name.startswith("-") or name.endswith("-"):
        return False, "Name cannot start or end with a hyphen"
    return True, None


_s3_client = None
_bucket_region: Optional[str] = None


def _resolve_bucket_region(bucket: str) -> str:
    """Discover the user-files bucket's real region.

    The AgentCore Runtime's ``AWS_REGION`` does not reliably match the
    deployment/bucket region. Pinning the S3 client to the wrong region makes
    ``PutObject`` fail with ``PermanentRedirect``. ``HeadBucket`` (maps to
    ``s3:ListBucket``, which the runtime role has) returns the true region in
    the ``x-amz-bucket-region`` header — on a 200 when probed from the matching
    region and on the 301 otherwise. This avoids depending on
    ``s3:GetBucketLocation``, which the inference-api role is not granted. Falls
    back to the env region if the lookup is unavailable — ``PutObject`` still
    succeeds in that case because the client below no longer hard-pins
    ``endpoint_url``, so botocore's built-in S3 region redirect can correct it.
    """
    global _bucket_region
    if _bucket_region:
        return _bucket_region
    region = None
    try:
        probe = boto3.client("s3", region_name="us-east-1")
        resp = probe.head_bucket(Bucket=bucket)
        region = (
            resp.get("ResponseMetadata", {})
            .get("HTTPHeaders", {})
            .get("x-amz-bucket-region")
        )
    except ClientError as exc:
        region = (
            exc.response.get("ResponseMetadata", {})
            .get("HTTPHeaders", {})
            .get("x-amz-bucket-region")
        )
        if not region:
            logger.warning(f"Could not resolve region for bucket {bucket}: {exc}")
    except Exception as exc:  # pragma: no cover - fall back to env region
        logger.warning(f"Could not resolve region for bucket {bucket}: {exc}")
    _bucket_region = region or _region()
    return _bucket_region


def _s3():
    """SigV4 S3 client pinned to the user-files bucket's actual region.

    Uses the bucket's real region (not ``AWS_REGION``) so ``PutObject`` never
    hits ``PermanentRedirect`` in the AgentCore Runtime. No explicit
    ``endpoint_url``: botocore then builds the correct regional virtual-host
    endpoint (which keeps presigned download URLs CORS-safe) and can still
    auto-correct the region if the resolved value is off.
    """
    global _s3_client
    if _s3_client is None:
        region = _resolve_bucket_region(_user_files_bucket())
        _s3_client = boto3.client(
            "s3",
            region_name=region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "virtual"},
            ),
        )
    return _s3_client


def _user_files_bucket() -> str:
    # Fail loudly rather than silently targeting a literal "user-files" bucket
    # the runtime has no access to. That default misreported a missing env var
    # as an S3 PermanentRedirect / AccessDenied and cost real debugging time.
    # The runtime env is wired in infrastructure's
    # inference-agentcore-construct.ts (S3_USER_FILES_BUCKET_NAME).
    bucket = os.environ.get("S3_USER_FILES_BUCKET_NAME")
    if not bucket:
        raise _StorageNotConfiguredError(
            "S3_USER_FILES_BUCKET_NAME is not set; the runtime cannot store or "
            "retrieve generated documents."
        )
    return bucket


def _storage_configured() -> bool:
    """True when the user-files bucket env var is set."""
    return bool(os.environ.get("S3_USER_FILES_BUCKET_NAME"))


# ---------------------------------------------------------------------------
# Code Interpreter primitives
# ---------------------------------------------------------------------------


def _ci_exec(code_interpreter, code: str) -> str:
    """Run Python in the sandbox; return stdout or raise _DocGenError."""
    response = code_interpreter.invoke(
        "executeCode",
        {"code": code, "language": "python", "clearContext": False},
    )
    stdout = ""
    for event in response.get("stream", []):
        result = event.get("result", {})
        if result.get("isError", False):
            stderr = result.get("structuredContent", {}).get(
                "stderr", "Unknown error"
            )
            logger.error(f"Code Interpreter error: {stderr[:500]}")
            raise _DocGenError(stderr[:1000])
        out = result.get("structuredContent", {}).get("stdout", "")
        if out:
            stdout += out
    return stdout


def _ci_read_bytes(code_interpreter, filename: str) -> Optional[bytes]:
    """Read a file out of the sandbox as bytes (or None if missing)."""
    download = code_interpreter.invoke("readFiles", {"paths": [filename]})
    content = None
    for event in download.get("stream", []):
        result = event.get("result", {})
        for block in result.get("content", []) or []:
            if "data" in block:
                content = block["data"]
            elif "resource" in block and "blob" in block["resource"]:
                content = block["resource"]["blob"]
            if content:
                break
        if content:
            break
    if content is None:
        return None
    # Code Interpreter may hand back raw bytes or a base64 string.
    if isinstance(content, str):
        content = base64.b64decode(content)
    return content


def _ci_write_bytes(code_interpreter, path: str, data: bytes) -> None:
    """Write binary bytes into the sandbox (base64 text + decode in-sandbox)."""
    b64 = base64.b64encode(data).decode("ascii")
    code_interpreter.invoke(
        "writeFiles",
        {"content": [{"path": f"{path}.b64", "text": b64}]},
    )
    _ci_exec(
        code_interpreter,
        (
            "import base64\n"
            f"with open({path + '.b64'!r}) as _f:\n"
            "    _raw = base64.b64decode(_f.read())\n"
            f"with open({path!r}, 'wb') as _o:\n"
            "    _o.write(_raw)\n"
        ),
    )


# ---------------------------------------------------------------------------
# User-files store helpers
# ---------------------------------------------------------------------------


def _download_s3_bytes(bucket: str, key: str) -> bytes:
    """Read an object's bytes from S3 (blocking — use ``asyncio.to_thread``)."""
    resp = _s3().get_object(Bucket=bucket, Key=key)
    return resp["Body"].read()


async def _store_document(
    user_id: str,
    session_id: str,
    filename: str,
    file_bytes: bytes,
    mime_type: str,
) -> Tuple[str, str, str]:
    """Persist a generated file to the user-files store and mint a download URL.

    Returns ``(upload_id, download_url, size_kb)``. ``mime_type`` is stored on
    the ``FileMetadata`` row and used for the download ``Content-Type``.
    """
    from apis.shared.files import (
        FileMetadata,
        FileStatus,
        get_file_upload_repository,
    )

    bucket = _user_files_bucket()
    timestamp_hex = format(
        int(datetime.now(timezone.utc).timestamp() * 1000), "x"
    )
    upload_id = f"{timestamp_hex}_{uuid.uuid4().hex[:16]}"
    s3_key = f"user-files/{user_id}/{session_id}/{upload_id}/{filename}"

    await asyncio.to_thread(
        _s3().put_object,
        Bucket=bucket,
        Key=s3_key,
        Body=file_bytes,
        ContentType=mime_type,
    )

    metadata = FileMetadata(
        upload_id=upload_id,
        user_id=user_id,
        session_id=session_id,
        filename=filename,
        mime_type=mime_type,
        size_bytes=len(file_bytes),
        s3_key=s3_key,
        s3_bucket=bucket,
        status=FileStatus.READY,
    )
    await get_file_upload_repository().create_file(metadata)

    download_url = await asyncio.to_thread(
        _s3().generate_presigned_url,
        "get_object",
        Params={
            "Bucket": bucket,
            "Key": s3_key,
            "ResponseContentType": mime_type,
            "ResponseContentDisposition": f'attachment; filename="{filename}"',
        },
        ExpiresIn=_DOWNLOAD_URL_TTL,
    )

    size_kb = f"{len(file_bytes) / 1024:.1f} KB"
    return upload_id, download_url, size_kb


def _download_card(filename: str, download_url: str, size_kb: str, verb: str) -> str:
    """Build the promoted inline-download-card tool result (JSON string).

    The ``ui_type``/``ui_display: inline`` discriminators make the frontend
    render a first-class download card (see inline-visual.component.ts,
    ``file_download``) instead of burying the link in the collapsed tool card.
    The renderer picks its icon from the filename extension, so a single
    ``file_download`` ui_type serves Word, Excel, and any future office file.
    """
    return json.dumps(
        {
            "success": True,
            "ui_type": "file_download",
            "ui_display": "inline",
            "payload": {
                "filename": filename,
                "download_url": download_url,
                "size_kb": size_kb,
            },
            "summary": (
                f"{verb} {filename} ({size_kb}). Also saved to this chat's Files."
            ),
        }
    )


def _error(text: str) -> Dict[str, Any]:
    return {"content": [{"text": text}], "status": "error"}


_NO_CI_MESSAGE = (
    "❌ Code Interpreter is not configured. AGENTCORE_CODE_INTERPRETER_ID was "
    "not found in the environment or Parameter Store."
)
