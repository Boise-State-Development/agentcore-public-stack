"""Prepared large documents: the ``_derived/`` sidecar layout and its readers.

A reference-class document (``is_reference_document`` in ``models.py``;
``docs/specs/large-document-attachments.md`` §4C–D) is prepared once, off the
request path, by the ``large-document-prepare`` Lambda. Preparation writes
sidecars beside the original::

    user-files/{user}/{session}/{upload_id}/{filename}             the original
    user-files/{user}/{session}/{upload_id}/_derived/text.jsonl     the text layer
    user-files/{user}/{session}/{upload_id}/_derived/pages-0001-0010.pdf  (PDF only)

and records ``preparation = {status: "ready", ...}`` on the file row last, so a
reader that sees ``ready`` can trust every sidecar to exist.

``document_read`` reads the sidecars instead of the original: pattern, text and
index modes read the text layer and never parse a PDF, and a page range fetches
only the shards that cover it. One call is bounded by shard size, not by
document size.

This module owns everything both sides must agree on: the key layout, the text
layer's encoding, and the shard arithmetic. Keys are derived from the row's
``s3_key``, never from model input.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Iterable, List, Optional, Tuple

from apis.shared.files.models import FileMetadata

logger = logging.getLogger(__name__)

#: Pages per PDF shard. Page-range mode fetches the shards covering the range,
#: so with the 20-page hard cap one call reads at most three shards.
LARGE_DOCUMENT_SHARD_PAGES = int(os.environ.get("LARGE_DOCUMENT_SHARD_PAGES", 10))

#: Bumped when the sidecar layout or the text layer's encoding changes, so a
#: reader can tell a preparation it understands from an older one.
PREPARATION_VERSION = 1

DERIVED_DIR = "_derived"
TEXT_LAYER_NAME = "text.jsonl"

STATUS_PENDING = "pending"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

#: The unit one text-layer record covers, by format. The same unit
#: ``document_read`` numbers its matches by: PDF pages, DOCX paragraphs (from
#: ``docx_paragraphs``), and lines of the text formats (``str.splitlines``).
UNIT_FOR_FORMAT = {"pdf": "page", "docx": "paragraph", "txt": "line", "md": "line", "html": "line"}


# ---------------------------------------------------------------------------
# Key layout
# ---------------------------------------------------------------------------


def upload_prefix(s3_key: str) -> str:
    """``user-files/{user}/{session}/{upload_id}`` (no trailing slash)."""
    base, _, _ = s3_key.rpartition("/")
    return base


def derived_prefix(s3_key: str) -> str:
    """``.../{upload_id}/_derived/``, with the trailing slash, for listing."""
    return f"{upload_prefix(s3_key)}/{DERIVED_DIR}/"


def text_layer_key(s3_key: str) -> str:
    return f"{derived_prefix(s3_key)}{TEXT_LAYER_NAME}"


def shard_key(s3_key: str, start: int, end: int) -> str:
    """``pages-{start:04d}-{end:04d}.pdf``; 1-indexed, inclusive."""
    return f"{derived_prefix(s3_key)}pages-{start:04d}-{end:04d}.pdf"


# ---------------------------------------------------------------------------
# Shard arithmetic
# ---------------------------------------------------------------------------


def shard_ranges(page_count: int, shard_pages: int) -> List[Tuple[int, int]]:
    """Every shard's ``(start, end)`` for a document, 1-indexed, inclusive."""
    if page_count <= 0 or shard_pages <= 0:
        return []
    return [(s, min(s + shard_pages - 1, page_count)) for s in range(1, page_count + 1, shard_pages)]


def covering_shards(start: int, end: int, page_count: int, shard_pages: int) -> List[Tuple[int, int]]:
    """The shards that hold pages ``start..end`` (already clamped to the document)."""
    return [(s, e) for s, e in shard_ranges(page_count, shard_pages) if e >= start and s <= end]


# ---------------------------------------------------------------------------
# Preparation state on the row
# ---------------------------------------------------------------------------


def preparation_status(meta: FileMetadata) -> Optional[str]:
    """``pending`` / ``ready`` / ``failed``, or ``None`` when never queued."""
    prep = meta.preparation
    if not isinstance(prep, dict):
        return None
    status = prep.get("status")
    return status if isinstance(status, str) else None


def is_prepared(meta: FileMetadata) -> bool:
    """Whether ``document_read`` can read this file from its sidecars."""
    if preparation_status(meta) != STATUS_READY:
        return False
    try:
        return int(meta.preparation.get("version", 0)) == PREPARATION_VERSION
    except (TypeError, ValueError):
        return False


def prepared_page_count(meta: FileMetadata) -> int:
    return int(meta.preparation.get("pageCount", 0))


def prepared_shard_pages(meta: FileMetadata) -> int:
    return int(meta.preparation.get("shardPages", 0))


# ---------------------------------------------------------------------------
# Text layer codec
# ---------------------------------------------------------------------------


def encode_text_layer(unit: str, texts: Iterable[str]) -> bytes:
    """One JSON object per line, ``{"<unit>": n, "text": ...}``, 1-indexed.

    JSON escapes every control character, so a record never contains a raw
    ``\\n`` and splitting the body on ``\\n`` recovers the records exactly,
    including texts with ``\\u2028`` or form feeds that ``str.splitlines``
    would cut.
    """
    return b"".join(
        json.dumps({unit: index, "text": text}, ensure_ascii=False).encode("utf-8") + b"\n"
        for index, text in enumerate(texts, start=1)
    )


def decode_text_layer(body: bytes) -> List[str]:
    """The unit texts, in order. Inverse of :func:`encode_text_layer`."""
    return [json.loads(line)["text"] for line in body.split(b"\n") if line]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def record_preparation(*, outcome: str, fmt: Optional[str], ms: int, pages: int = 0, units: int = 0) -> None:
    """One content-free ``LargeDocumentPrepared`` record per preparation.

    Spec §6, beside the ``DocumentDigest*`` records. Never raises.
    """
    logger.info(
        "large_document_prepared: outcome=%s format=%s pages=%d units=%d ms=%d",
        outcome, fmt, pages, units, ms,
    )
    try:
        from apis.shared.observability.emf import emit_emf_metrics
        from apis.shared.observability.prompt_cache import prompt_cache_observability_enabled

        if not prompt_cache_observability_enabled():
            return
        emit_emf_metrics(
            "AgentCoreStack/Compaction",
            metrics={"LargeDocumentPrepared": 1, "LargeDocumentPrepareMs": int(ms), "LargeDocumentPages": int(pages)},
            properties={"format": fmt, "outcome": outcome},
            units={"LargeDocumentPrepareMs": "Milliseconds", "LargeDocumentPages": "Count"},
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("large_document_prepared EMF skipped: %s", e)


def preparation_record(
    *,
    status: str,
    fmt: Optional[str] = None,
    page_count: int = 0,
    shard_pages: int = 0,
    text_bytes: int = 0,
    ms: int = 0,
    error: Optional[str] = None,
    prepared_at: Optional[str] = None,
) -> Dict[str, Any]:
    """The ``preparation`` map stored on the row. Numbers and enums only:
    ``error`` is an exception class name, never a message."""
    record: Dict[str, Any] = {"status": status, "version": PREPARATION_VERSION}
    if fmt:
        record["format"] = fmt
    if status == STATUS_READY:
        record.update({"pageCount": page_count, "shardPages": shard_pages, "textBytes": text_bytes, "ms": ms})
    if error:
        record["error"] = error
    if prepared_at:
        record["preparedAt"] = prepared_at
    return record


__all__ = [
    "DERIVED_DIR",
    "LARGE_DOCUMENT_SHARD_PAGES",
    "PREPARATION_VERSION",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_READY",
    "TEXT_LAYER_NAME",
    "UNIT_FOR_FORMAT",
    "covering_shards",
    "decode_text_layer",
    "derived_prefix",
    "encode_text_layer",
    "is_prepared",
    "preparation_record",
    "preparation_status",
    "prepared_page_count",
    "prepared_shard_pages",
    "record_preparation",
    "shard_key",
    "shard_ranges",
    "text_layer_key",
    "upload_prefix",
]
