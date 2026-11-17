"""The native attachment bytes a warm agent's history will send again.

``docs/specs/unified-attachment-delivery.md`` §4.4.1. A warm agent keeps every
earlier native document and image block in ``agent.messages`` as bytes: offload
swaps a document for its digest only after its pin and only when the prefix
re-write is free (``document_offload``), and only restore swaps them all. Those
bytes ride in every request, so the planner takes them out of the turn's request
budget (``ConversationFacts.held_native_bytes``) before it places this turn's
attachments. Without that, two warm turns at the 16 MB budget send about 45 MB
of base64, past Bedrock's 32 MB payload, which answers "Input is too long".

Counted: top-level ``document`` and ``image`` blocks of every message, and the
same blocks inside ``toolResult`` content (``document_read`` slices). A block
whose ``source`` holds a handle or no bytes counts nothing. Non-PDF documents
count their raw bytes although the per-call projection sends them as text: the
text is rarely longer, so the count errs high, towards ``on_demand``.

Pure: a walk over dicts and lists, ``len`` of what is there, no decode and no
I/O. Never raises.
"""

from __future__ import annotations

from typing import Any, Iterable

_MEDIA_KEYS = ("document", "image")


def _source_bytes(payload: Any) -> int:
    if not isinstance(payload, dict):
        return 0
    source = payload.get("source")
    raw = source.get("bytes") if isinstance(source, dict) else None
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return len(raw)
    if isinstance(raw, str):
        # A base64 string (a message built from JSON): its decoded length.
        return len(raw) * 3 // 4
    return 0


def _block_bytes(block: Any) -> int:
    if not isinstance(block, dict):
        return 0
    total = 0
    for key in _MEDIA_KEYS:
        if key in block:
            total += _source_bytes(block[key])
    result = block.get("toolResult")
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        for inner in result["content"]:
            if isinstance(inner, dict):
                for key in _MEDIA_KEYS:
                    if key in inner:
                        total += _source_bytes(inner[key])
    return total


def held_native_bytes(messages: Iterable[Any]) -> int:
    """Raw bytes of the native document and image blocks in ``messages``."""
    total = 0
    try:
        for message in messages or ():
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                total += _block_bytes(block)
    except Exception:  # noqa: BLE001 - a count that cannot be taken is no count, never a failed turn
        return total
    return total


__all__ = ["held_native_bytes"]
