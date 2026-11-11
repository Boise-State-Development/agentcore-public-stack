"""Restore images persisted as handles, byte for byte (unified-attachment-delivery §6.2).

Restore re-sends an attached image, unlike a document, so a handle has to come
back as exactly the bytes the turn sent: the prompt cache is an exact-prefix
match, and a re-encoded image would re-write every cached turn behind it. The
handle names the stored copy (``bucket``, ``key``: the high or compact copy
``image_downscale`` wrote at upload, or the original for a legacy row). The
copies are deterministic and written before the row turns READY, so the object
is the bytes. Nothing is decoded or re-encoded here.

**Latency.** This runs inside ``TurnBasedSessionManager.initialize``, so only a
restore pays it (a cold agent, never a warm turn), and only a history holding
image handles. Every image is read in one concurrent batch
(:data:`IMAGE_RESTORE_CONCURRENCY` threads), so the cost is about one S3 GET,
not one per image. ``image_restored`` on the ledger carries the count, bytes
and milliseconds.

**Safety.** A key is read only if it lies under the restoring user's own
``user-files/{user_id}/`` prefix. A handle that fails (a missing object, a
denied read, a foreign key) becomes a short text note, so the turn still runs;
that one restore's prefix differs, which a restore after an expired cache
re-writes anyway. Never raises.
"""

from __future__ import annotations

import copy
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from apis.shared.files.attachment_handle import handle_of

logger = logging.getLogger(__name__)

#: Concurrent S3 reads per restore. A message holds at most 20 files.
IMAGE_RESTORE_CONCURRENCY = int(os.environ.get("IMAGE_RESTORE_CONCURRENCY", "8") or 8)


@dataclass
class ImageRestoreResult:
    messages: List[Dict[str, Any]]
    restored: int = 0
    failed: int = 0
    bytes_read: int = 0
    elapsed_ms: int = 0


def unavailable_text(handle: Dict[str, Any]) -> str:
    """The stand-in for an image whose stored copy could not be read."""
    return f"[Image unavailable: upload_id={handle.get('uploadId') or 'unknown'}]"


def _read(bucket: str, key: str) -> bytes:
    from apis.shared.files.document_read import _get_object_bytes

    return _get_object_bytes(bucket, key)


def _image_handles(messages: List[Dict[str, Any]]) -> List[Tuple[int, int, Dict[str, Any]]]:
    found = []
    for m, message in enumerate(messages or ()):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for b, block in enumerate(content):
            if isinstance(block, dict) and set(block) == {"image"}:
                handle = handle_of(block.get("image"))
                if handle is not None and handle.get("class") == "image":
                    found.append((m, b, handle))
    return found


def restore_image_handles(messages: List[Dict[str, Any]], *, user_id: Optional[str]) -> ImageRestoreResult:
    """``messages`` with every image handle replaced by its stored bytes."""
    try:
        found = _image_handles(messages)
    except Exception:  # noqa: BLE001
        return ImageRestoreResult(messages=messages)
    if not found:
        return ImageRestoreResult(messages=messages)

    started = time.perf_counter()
    prefix = f"user-files/{user_id}/" if user_id else None
    bucket_default = os.environ.get("S3_USER_FILES_BUCKET_NAME", "")

    def _fetch(handle: Dict[str, Any]) -> Optional[bytes]:
        key = str(handle.get("key") or "")
        bucket = str(handle.get("bucket") or bucket_default)
        if not key or not bucket or prefix is None or not key.startswith(prefix):
            return None
        try:
            raw = _read(bucket, key)
        except Exception:  # noqa: BLE001
            logger.warning("image_restore: stored copy unreadable for upload %s", handle.get("uploadId"))
            return None
        size = handle.get("sizeBytes")
        if isinstance(size, int) and size and len(raw) != size:
            logger.warning("image_restore: stored copy for upload %s changed size", handle.get("uploadId"))
            return None
        return raw

    workers = max(1, min(IMAGE_RESTORE_CONCURRENCY, len(found)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="image-restore") as pool:
        fetched = list(pool.map(_fetch, [h for _, _, h in found]))

    out = copy.copy(messages)
    result = ImageRestoreResult(messages=out)
    copied: Dict[int, Dict[str, Any]] = {}
    for (m, b, handle), raw in zip(found, fetched):
        message = copied.get(m)
        if message is None:
            message = {**out[m], "content": list(out[m]["content"])}
            copied[m] = message
            out[m] = message
        if raw is None:
            message["content"][b] = {"text": unavailable_text(handle)}
            result.failed += 1
            continue
        fmt = (message["content"][b].get("image") or {}).get("format")
        message["content"][b] = {"image": {"format": fmt, "source": {"bytes": raw}}}
        result.restored += 1
        result.bytes_read += len(raw)
    result.elapsed_ms = int(round((time.perf_counter() - started) * 1000))
    return result


__all__ = ["IMAGE_RESTORE_CONCURRENCY", "ImageRestoreResult", "restore_image_handles", "unavailable_text"]
