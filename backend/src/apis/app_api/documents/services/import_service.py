"""Async file-source import: download from a connector, stage to S3.

The import endpoint creates document records synchronously and returns,
then schedules `run_import` as a fire-and-forget task (mirroring
`cleanup_service`). For each file the task downloads the bytes through the
file-source adapter, backfills the real file metadata onto the document
record, and PUTs the bytes into the documents bucket — where the existing
S3-event ingestion Lambda picks them up and drives the document through
chunking/embedding exactly as a device upload would.

Never raises: a per-file failure marks that one document 'failed' and the
batch continues. The access token is held in memory for the life of the
task only and is never logged.
"""

import asyncio
import logging
import os
from typing import List, Tuple

import boto3

from apis.app_api.documents.services.document_service import (
    update_document_import_metadata,
    update_document_status,
)
from apis.app_api.documents.services.storage_service import (
    _get_documents_bucket,
    _get_s3_key,
    _sanitize_filename,
)
from apis.app_api.file_sources.adapter import FileSourceAdapter
from apis.app_api.file_sources.models import FileSourceError

logger = logging.getLogger(__name__)


async def run_import(
    assistant_id: str,
    adapter: FileSourceAdapter,
    access_token: str,
    items: List[Tuple[str, str]],
) -> None:
    """Download each imported file and stage it to S3 for ingestion.

    `items` is a list of `(document_id, source_file_id)` pairs — one per
    document record the import endpoint created. Processed sequentially so a
    large batch doesn't open many provider connections at once. Never raises.

    Args:
        assistant_id: Parent assistant identifier
        adapter: The resolved file-source adapter (registry singleton)
        access_token: The importing user's OAuth access token
        items: (document_id, source_file_id) pairs to import
    """
    for document_id, file_id in items:
        try:
            await _import_one(assistant_id, adapter, access_token, document_id, file_id)
        except Exception as e:
            # Defensive: _import_one already swallows its own errors, but a
            # bug there must not abort the rest of the batch.
            logger.error(
                f"Unexpected error importing document {document_id}: {e}",
                exc_info=True,
            )


async def _import_one(
    assistant_id: str,
    adapter: FileSourceAdapter,
    access_token: str,
    document_id: str,
    file_id: str,
) -> None:
    """Import a single file: download, backfill metadata, PUT to S3."""
    try:
        downloaded = await adapter.download(access_token, file_id)
    except FileSourceError as e:
        logger.warning(f"File-source download failed for document {document_id}: {e}")
        await _mark_failed(
            assistant_id,
            document_id,
            "Could not download this file from the file source.",
            str(e),
        )
        return
    except Exception as e:
        logger.error(
            f"Unexpected download error for document {document_id}: {e}",
            exc_info=True,
        )
        await _mark_failed(
            assistant_id,
            document_id,
            "Could not download this file from the file source.",
            str(e),
        )
        return

    try:
        # The real filename is known only after download — Google-native
        # docs export to a different extension — so the final S3 key is
        # computed here, not at import-request time.
        s3_key = _get_s3_key(
            assistant_id, document_id, _sanitize_filename(downloaded.filename)
        )
        updated = await update_document_import_metadata(
            assistant_id,
            document_id,
            filename=downloaded.filename,
            content_type=downloaded.content_type,
            size_bytes=len(downloaded.content),
            s3_key=s3_key,
        )
        if updated is None:
            # Document was deleted between the import request and now —
            # don't strand orphan bytes (and an orphan ingestion run) in S3.
            logger.info(
                f"Document {document_id} gone before S3 stage; skipping import"
            )
            return

        await _put_object(s3_key, downloaded.content, downloaded.content_type)
        logger.info(
            f"Staged imported document {document_id} to S3; ingestion will start"
        )
    except Exception as e:
        logger.error(
            f"Failed to stage imported document {document_id} to S3: {e}",
            exc_info=True,
        )
        await _mark_failed(
            assistant_id,
            document_id,
            "Could not stage this file for ingestion.",
            str(e),
        )


async def _put_object(s3_key: str, content: bytes, content_type: str) -> None:
    """PUT bytes into the documents bucket, triggering S3-event ingestion."""
    bucket = _get_documents_bucket()
    loop = asyncio.get_event_loop()
    s3_client = boto3.client("s3")
    await loop.run_in_executor(
        None,
        lambda: s3_client.put_object(
            Bucket=bucket,
            Key=s3_key,
            Body=content,
            ContentType=content_type,
        ),
    )


async def _mark_failed(
    assistant_id: str,
    document_id: str,
    error_message: str,
    error_details: str,
) -> None:
    """Mark a document 'failed' so the SPA stops polling. Never raises."""
    try:
        await update_document_status(
            assistant_id=assistant_id,
            document_id=document_id,
            status="failed",
            error_message=error_message,
            error_details=error_details,
        )
    except Exception as e:
        logger.error(
            f"Failed to mark imported document {document_id} as failed: {e}",
            exc_info=True,
        )
