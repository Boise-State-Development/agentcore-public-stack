"""large-document-prepare: build the sidecars ``document_read`` reads for a large document.

``docs/specs/large-document-attachments.md`` §4C. app-api's ``complete_upload``
sends ``{"user_id", "upload_id"}`` to the prepare queue for a reference-class
upload (``is_reference_document``) instead of building its digest in-process.
For each message this reads the file row (``PK = USER#{user_id}``,
``SK = FILE#{upload_id}``) and the original, then writes beside it, under
``{upload prefix}/_derived/``:

* ``text.jsonl``: one record per PDF page, DOCX paragraph or text-format line
  (the units ``document_read`` numbers by), so pattern, text and index modes
  never parse the document again;
* PDF only, ``pages-{start:04d}-{end:04d}.pdf`` shards of
  ``LARGE_DOCUMENT_SHARD_PAGES`` pages, so a page range fetches only the
  shards that cover it;

and finally, in one conditional write, the digest (``build_digest``, from the
units already extracted) and ``preparation = {status: "ready", ...}``. Ready is
written last, so a reader that sees it can trust every sidecar to exist.

**Idempotent.** A redelivered message for a prepared row is acknowledged
without work, and a re-run writes the same keys with the same content. A row
that is gone, not READY or not reference-class is acknowledged and skipped.

**Never raises on a bad document.** A document that cannot be opened or parsed
records ``preparation = failed`` (the exception class name only) and is
acknowledged; ``document_read`` then falls back to the original where it can.
AWS errors (S3, DynamoDB) are reported as batch item failures so SQS retries
them; the last attempt records ``failed`` instead, so the row never stays
``pending`` because of one. Only a crash (time-out, out of memory) reaches the
dead-letter queue.

**Deleted while preparing.** The final write is conditional on the row; when it
fails the file was deleted meanwhile, so this deletes the sidecars it wrote.

**The attachments knowledge base (§4E, Tier 2).** With ``ATTACHMENT_KB_ENABLED``
on, the same queue also carries the index pipeline (``apis/shared/files/
attachment_index.py``), because the stack had no room for a second one:

* a preparation that ends ``ready`` indexes the original in the same invocation;
* ``{"action": "index" | "check", "user_id", "upload_id"}`` re-syncs or polls one
  upload;
* an S3 *Object Deleted* event for an original (EventBridge) removes its document;
* ``{"action": "reconcile"}`` (a daily schedule) sweeps the knowledge base.

An index failure never undoes a preparation: Tier 1 does not depend on Tier 2.
With the flag off, every index message is acknowledged untouched.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from typing import Any, BinaryIO, Dict, List, Optional, Tuple, Union

from botocore.exceptions import BotoCoreError, ClientError

from apis.shared.aws_clients import get_client
from apis.shared.feature_flags import attachment_kb_enabled
from apis.shared.files import large_document
from apis.shared.files.document_digest import build_digest, document_digest_enabled, record_digest
from apis.shared.files.document_read import _open_pdf, _pdf_page_text, document_format_for, docx_paragraphs
from apis.shared.files.models import FileMetadata, FileStatus, is_reference_document
from apis.shared.files.pdfium_lock import serialized_pdfium
from apis.shared.files.repository import FileUploadRepository, get_file_upload_repository
from apis.shared.timestamps import utc_now_iso

logger = logging.getLogger()
logger.setLevel(logging.INFO)

#: The queue's maxReceiveCount (CDK sets it). The attempt that reaches it
#: records ``failed`` rather than leaving the row ``pending`` for good.
MAX_RECEIVE_COUNT = int(os.environ.get("LARGE_DOCUMENT_PREPARE_MAX_RECEIVE_COUNT", 3))

#: Shards together may hold at most this many times the original's bytes.
#: Each shard carries every resource its pages use, so a large image shared by
#: every page (a full-page background) is copied into each shard: measured, a
#: 23 MB, 300-page PDF with one 20 MB shared image produced 603 MB of shards.
#: Ordinary documents come out near 1x. Past the cap preparation fails, and
#: ``document_read`` says the document could not be prepared.
MAX_SHARD_BYTES_RATIO = float(os.environ.get("LARGE_DOCUMENT_MAX_SHARD_BYTES_RATIO", 10))

_TRANSIENT = (ClientError, BotoCoreError)


class PreparationFailed(Exception):
    """The document itself could not be prepared. ``cause`` is a class name,
    never a message."""

    def __init__(self, cause: Union[BaseException, str]):
        name = cause if isinstance(cause, str) else type(cause).__name__
        super().__init__(name)
        self.cause = name


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _render_shard(pdf: Any, start: int, end: int, out: BinaryIO) -> None:
    """Pages ``start..end`` (1-indexed, inclusive) as a standalone PDF, written to ``out``.

    Saved to a file, not a ``BytesIO``: measured on a 300-page PDF whose pages
    share one 20 MB image, saving each shard into a ``BytesIO`` grew the
    process by ~19 MB per shard (726 MB peak over 30 shards) while saving the
    same shards to a file stayed flat. Every shard carries the resources its
    pages use, so a large shared one is copied into each.
    """
    import pypdfium2 as pdfium

    sub = pdfium.PdfDocument.new()
    try:
        sub.import_pages(pdf, pages=list(range(start - 1, end)))
        sub.save(out)
    finally:
        sub.close()


def _put(s3: Any, bucket: str, key: str, body: Union[bytes, BinaryIO], content_type: str) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)


@serialized_pdfium
def _prepare_pdf(s3: Any, meta: FileMetadata, raw: bytes, shard_pages: int) -> List[str]:
    """Page texts, with every shard uploaded as it is rendered (one shard in
    memory at a time). Document errors raise ``PreparationFailed``; S3 errors
    propagate as they are."""
    try:
        pdf = _open_pdf(raw)
    except Exception as e:  # noqa: BLE001
        raise PreparationFailed(e) from e
    try:
        try:
            texts = [_pdf_page_text(pdf, i) for i in range(len(pdf))]
        except Exception as e:  # noqa: BLE001
            raise PreparationFailed(e) from e
        budget = MAX_SHARD_BYTES_RATIO * len(raw)
        written = 0
        for start, end in large_document.shard_ranges(len(texts), shard_pages):
            with tempfile.TemporaryFile() as out:
                try:
                    _render_shard(pdf, start, end, out)
                except Exception as e:  # noqa: BLE001
                    raise PreparationFailed(e) from e
                written += out.tell()
                if written > budget:
                    raise PreparationFailed("ShardsTooLarge")
                out.seek(0)
                _put(s3, meta.s3_bucket, large_document.shard_key(meta.s3_key, start, end), out, "application/pdf")
        return texts
    finally:
        pdf.close()


def _units(fmt: str, raw: bytes) -> List[str]:
    """Unit texts for the non-PDF formats, exactly as ``document_read`` splits them."""
    try:
        if fmt == "docx":
            return docx_paragraphs(raw)
        return raw.decode("utf-8", errors="replace").splitlines()
    except Exception as e:  # noqa: BLE001
        raise PreparationFailed(e) from e


# ---------------------------------------------------------------------------
# One upload
# ---------------------------------------------------------------------------


def _delete_derived(s3: Any, meta: FileMetadata) -> None:
    prefix = large_document.derived_prefix(meta.s3_key)
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=meta.s3_bucket, Prefix=prefix):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=meta.s3_bucket, Delete={"Objects": keys, "Quiet": True})


def _key_is_owned(meta: FileMetadata, user_id: str, upload_id: str) -> bool:
    """The row's key must sit in its owner's prefix and its own upload folder.
    IAM confines the function to ``user-files/*`` anyway; this keeps a
    malformed row from reading or writing another user's folder inside it."""
    key = meta.s3_key or ""
    return key.startswith(f"user-files/{user_id}/") and f"/{upload_id}/" in key and ".." not in key


async def _record_failed(
    repository: FileUploadRepository, meta: FileMetadata, fmt: Optional[str], error: str, started: float
) -> str:
    await repository.update_file_preparation(
        meta.user_id, meta.upload_id,
        large_document.preparation_record(
            status=large_document.STATUS_FAILED, fmt=fmt, error=error, prepared_at=utc_now_iso()
        ),
    )
    large_document.record_preparation(outcome="failed", fmt=fmt, ms=int((time.monotonic() - started) * 1000))
    return "failed"


async def prepare_upload(
    user_id: str,
    upload_id: str,
    *,
    s3: Any = None,
    repository: Optional[FileUploadRepository] = None,
    final_attempt: bool = False,
) -> str:
    """Prepare one upload; returns the outcome. Raises only on AWS errors
    before the final attempt, so SQS retries them."""
    repository = repository or get_file_upload_repository()
    s3 = s3 or get_client("s3", os.environ.get("AWS_REGION") or None)
    started = time.monotonic()

    meta = await repository.get_file(user_id, upload_id)
    if meta is None:
        return "missing"
    status = meta.status if isinstance(meta.status, str) else meta.status.value
    if status != FileStatus.READY.value:
        return "not_ready"
    if not is_reference_document(meta):
        return "not_reference"
    if large_document.is_prepared(meta):
        return "already_prepared"

    fmt = document_format_for(meta.mime_type, meta.filename)
    if not _key_is_owned(meta, user_id, upload_id):
        return await _record_failed(repository, meta, fmt, "UnexpectedKey", started)

    try:
        raw = s3.get_object(Bucket=meta.s3_bucket, Key=meta.s3_key)["Body"].read()
        shard_pages = large_document.LARGE_DOCUMENT_SHARD_PAGES
        if fmt == "pdf":
            texts = _prepare_pdf(s3, meta, raw, shard_pages)
        else:
            texts = _units(fmt, raw)
        layer = large_document.encode_text_layer(large_document.UNIT_FOR_FORMAT[fmt], texts)
        _put(s3, meta.s3_bucket, large_document.text_layer_key(meta.s3_key), layer, "application/x-ndjson")
    except PreparationFailed as e:
        logger.warning("large-document-prepare: document could not be prepared (%s)", e.cause)
        try:
            _delete_derived(s3, meta)  # shards written before the failure
        except _TRANSIENT:
            logger.warning("large-document-prepare: partial sidecars left behind", exc_info=True)
        return await _record_failed(repository, meta, fmt, e.cause, started)
    except _TRANSIENT as e:
        if not final_attempt:
            raise
        logger.warning("large-document-prepare: giving up after retries (%s)", type(e).__name__)
        return await _record_failed(repository, meta, fmt, type(e).__name__, started)

    digest: Optional[Dict[str, Any]] = None
    if document_digest_enabled():
        # The digest reads the units already extracted, so a 50 MB PDF is
        # parsed once. HTML is the exception: the digest outlines its
        # tag-stripped text, while document_read searches the raw lines.
        units: Optional[Tuple[str, List[str]]] = None
        if fmt != "html":
            units = (large_document.UNIT_FOR_FORMAT[fmt], texts)
        built = await build_digest(
            raw=raw if units is None else b"",
            mime_type=meta.mime_type,
            filename=meta.filename,
            upload_id=meta.upload_id,
            units=units,
        )
        record_digest(built)
        digest = built.to_item()

    ms = int((time.monotonic() - started) * 1000)
    page_count = len(texts) if fmt == "pdf" else 0
    preparation = large_document.preparation_record(
        status=large_document.STATUS_READY,
        fmt=fmt,
        page_count=page_count,
        shard_pages=shard_pages if fmt == "pdf" else 0,
        text_bytes=len(layer),
        ms=ms,
        prepared_at=utc_now_iso(),
    )
    stored = await repository.update_file_preparation(user_id, upload_id, preparation, digest)
    if stored is None:
        # Deleted (or no longer READY) while it was being prepared: app-api's
        # delete may already have run, so the sidecars are this run's to remove.
        # A retry could not finish this (the row is gone), so it is not one.
        try:
            _delete_derived(s3, meta)
        except _TRANSIENT:
            logger.warning("large-document-prepare: sidecars of a deleted file left behind", exc_info=True)
        large_document.record_preparation(outcome="deleted", fmt=fmt, ms=ms, pages=page_count, units=len(texts))
        return "deleted"
    large_document.record_preparation(outcome="ready", fmt=fmt, ms=ms, pages=page_count, units=len(texts))
    return "ready"


# ---------------------------------------------------------------------------
# The attachments knowledge base
# ---------------------------------------------------------------------------


async def index_after_prepare(user_id: str, upload_id: str, indexer: Any = None) -> str:
    """Index a freshly prepared upload. Never raises: a failure queues a retry
    (``{"action": "index"}``) and the preparation stands either way."""
    from apis.shared.files import attachment_index

    indexer = indexer or attachment_index.Indexer()
    try:
        return await attachment_index.sync_upload(indexer, user_id, upload_id)
    except Exception:  # noqa: BLE001 - Tier 1 never waits on Tier 2
        logger.warning("large-document-prepare: indexing failed; queued a retry", exc_info=True)
        try:
            indexer.send({"action": "index", "user_id": user_id, "upload_id": upload_id}, 60)
        except Exception:  # noqa: BLE001 - the daily reconcile cannot see an unindexed row, so say so
            logger.error("large-document-prepare: index retry could not be queued", exc_info=True)
        return "index_retry_queued"


async def _run_index_message(message: Dict[str, Any]) -> str:
    from apis.shared.files import attachment_index

    indexer = attachment_index.Indexer()
    kind = message["kind"]
    if kind == "reconcile":
        counts = await attachment_index.reconcile(indexer)
        return f"reconciled {counts}"
    if kind == "check":
        return await attachment_index.check_upload(indexer, message["user_id"], message["upload_id"], message["attempt"])
    if kind == "deleted":
        return await attachment_index.sync_upload(
            indexer, message["user_id"], message["upload_id"], session_id=message["session_id"]
        )
    return await attachment_index.sync_upload(indexer, message["user_id"], message["upload_id"], force=message["force"])


# ---------------------------------------------------------------------------
# SQS entrypoint
# ---------------------------------------------------------------------------


def _ids(message: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    user_id, upload_id = message.get("user_id"), message.get("upload_id")
    if not (isinstance(user_id, str) and user_id and isinstance(upload_id, str) and upload_id):
        return None
    return user_id, upload_id


def _parse(body: str) -> Optional[Dict[str, Any]]:
    """The message's kind and fields, or None when it is malformed.

    Kinds: ``prepare`` (``{user_id, upload_id}``, from app-api), ``index`` and
    ``check`` (from this pipeline and app-api's re-index on open), ``deleted``
    (an S3 Object Deleted event for an original) and ``reconcile`` (the daily
    schedule).
    """
    try:
        message = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(message, dict):
        return None

    if message.get("source") == "aws.s3" and message.get("detail-type") == "Object Deleted":
        from apis.shared.files.attachment_index import parse_original_key

        key = str(((message.get("detail") or {}).get("object") or {}).get("key") or "")
        parsed_key = parse_original_key(key)
        if parsed_key is None:
            return {"kind": "ignored"}  # a sidecar, a thumbnail, or not an upload
        user_id, session_id, upload_id = parsed_key
        return {"kind": "deleted", "user_id": user_id, "session_id": session_id, "upload_id": upload_id}

    action = message.get("action")
    if action == "reconcile":
        return {"kind": "reconcile"}
    ids = _ids(message)
    if ids is None:
        return None
    if action is None:
        return {"kind": "prepare", "user_id": ids[0], "upload_id": ids[1]}
    if action == "index":
        return {"kind": "index", "user_id": ids[0], "upload_id": ids[1], "force": bool(message.get("force"))}
    if action == "check":
        try:
            attempt = max(1, int(message.get("attempt") or 1))
        except (TypeError, ValueError):
            attempt = 1
        return {"kind": "check", "user_id": ids[0], "upload_id": ids[1], "attempt": attempt}
    return None


async def _handle(message: Dict[str, Any], final_attempt: bool) -> str:
    kind = message["kind"]
    if kind == "ignored":
        return "ignored"
    if kind == "prepare":
        outcome = await prepare_upload(message["user_id"], message["upload_id"], final_attempt=final_attempt)
        if outcome == "ready" and attachment_kb_enabled():
            outcome += f", index: {await index_after_prepare(message['user_id'], message['upload_id'])}"
        return outcome
    if not attachment_kb_enabled():
        return "attachment KB off; acknowledged"
    return await _run_index_message(message)


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    failures: List[Dict[str, str]] = []
    for record in (event or {}).get("Records") or []:
        message_id = record.get("messageId", "")
        parsed = _parse(record.get("body", ""))
        if parsed is None:
            # A malformed message can never succeed; retrying it only delays
            # the dead-letter queue. Acknowledge and say so.
            logger.error("large-document-prepare: malformed message %s dropped", message_id)
            continue
        try:
            receive_count = int((record.get("attributes") or {}).get("ApproximateReceiveCount", 1))
        except (TypeError, ValueError):
            receive_count = 1
        try:
            outcome = asyncio.run(_handle(parsed, final_attempt=receive_count >= MAX_RECEIVE_COUNT))
            logger.info("large-document-prepare: %s (%s) -> %s", message_id, parsed["kind"], outcome)
        except Exception:  # noqa: BLE001 - one upload never fails its neighbours
            logger.exception("large-document-prepare: %s will be retried", message_id)
            if message_id:
                failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}
