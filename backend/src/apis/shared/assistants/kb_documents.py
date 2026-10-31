"""An assistant's knowledge-base documents, listed and read from their originals.

Two readers share these: full-context mode (:mod:`kb_full_context`, which sends
a small knowledge base whole) and a project harness's ``project_files`` tool
(``agents/builtin_tools/project_files_tool.py``, which lists a project's Files
and reads one by name). Both want the same two things, so they live here once:

* :func:`list_kb_documents` — every ``DOC#`` row under ``AST#{assistant_id}``,
  one paginated Query, never cached, so a deleted document is gone on the next
  read.
* :func:`kb_document_text` — a document's whole text layer, extracted from the
  original upload in the documents bucket, which both the legacy and the
  managed engine keep (a managed knowledge base has no other copy the Runtime
  can read). Text extraction is pure, so the result is cached per process,
  keyed by everything that changes when the bytes do. Word tracked changes are
  written out as ``[deleted: …]`` / ``[inserted: …]`` while
  ``WORD_TRACKED_CHANGES_ENABLED`` is on, exactly as the indexer and
  ``document_read`` render them (``docx_paragraphs``).

Neither function checks access: callers resolve the invoking user's grant
(:mod:`kb_access`) before asking for anything here. Both block; run them off the
event loop.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

#: Per-document text cache entries. A document's text is a pure function of its
#: bytes, so the key is what changes when they do; entries never go stale, they
#: are only evicted for room.
TEXT_CACHE_MAX_ENTRIES = 256

_text_cache: Dict[Tuple[Any, ...], Optional[str]] = {}
_text_cache_lock = threading.Lock()


def list_kb_documents(assistant_id: str) -> List[Dict[str, Any]]:
    """Every DOC# row of this assistant (blocking; paginated)."""
    from boto3.dynamodb.conditions import Key

    from apis.shared.aws_clients import get_dynamodb_table

    table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not table_name:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME not configured")
    table = get_dynamodb_table(table_name, os.environ.get("AWS_REGION", "us-west-2"))
    query: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(f"AST#{assistant_id}") & Key("SK").begins_with("DOC#"),
    }
    items: List[Dict[str, Any]] = []
    while True:
        response = table.query(**query)
        items.extend(response.get("Items", []))
        last = response.get("LastEvaluatedKey")
        if not last:
            return items
        query["ExclusiveStartKey"] = last


def text_cache_key(doc: Mapping[str, Any]) -> Tuple[Any, ...]:
    return (
        doc.get("documentId"),
        doc.get("s3Key"),
        doc.get("updatedAt"),
        doc.get("contentHash"),
        int(doc.get("sizeBytes") or 0),
    )


def kb_document_text(doc: Mapping[str, Any], fmt: str, limit: int) -> Optional[str]:
    """The document's whole text layer, or ``None`` if it is empty or over ``limit``.

    Blocking (S3 read and parse); run it off the loop. Cached by
    :func:`text_cache_key`, including the ``None`` answers, so an over-budget or
    scanned document is downloaded once per process, not once per turn.
    """
    from apis.shared.aws_clients import get_client
    from apis.shared.files.document_digest import document_text

    key = text_cache_key(doc) + (limit,)
    with _text_cache_lock:
        if key in _text_cache:
            return _text_cache[key]

    bucket = os.environ.get("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME")
    if not bucket:
        raise RuntimeError("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME not configured")
    raw = get_client("s3").get_object(Bucket=bucket, Key=doc["s3Key"])["Body"].read()
    extracted = document_text(fmt, raw, limit)
    text: Optional[str] = extracted.text.strip()
    if extracted.truncated or not text:
        # Over budget, or no text layer (a scan): the caller decides what that
        # means (full context falls back to retrieval; the tool says so).
        text = None

    with _text_cache_lock:
        if len(_text_cache) >= TEXT_CACHE_MAX_ENTRIES:
            _text_cache.pop(next(iter(_text_cache)))
        _text_cache[key] = text
    return text


def reset_text_cache_for_tests() -> None:
    with _text_cache_lock:
        _text_cache.clear()


__all__ = [
    "TEXT_CACHE_MAX_ENTRIES",
    "kb_document_text",
    "list_kb_documents",
    "reset_text_cache_for_tests",
    "text_cache_key",
]
