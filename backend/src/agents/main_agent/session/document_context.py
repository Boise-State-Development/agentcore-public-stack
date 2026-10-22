"""Content-free summary of the attachments in a conversation's live context.

The cost rows could say what a call cost and (since the context ledger) what
compaction did to the history before it, but not *how much of the context
was documents* — the one quantity ``docs/specs/document-context-offload.md``
needs to decide whether its cost work is worth shipping (evaluation spec §4.1:
the recoverable envelope is the document share of every cold re-write, and
that share was unmeasured). This module measures it from ``agent.messages``
at turn end, and the stream coordinator persists the numbers on each of the
turn's ``C#`` rows next to ``prefixTokens`` and ``compactionEvents``.

Everything here is a count, a byte size or a token estimate. No filename,
title, text or document byte ever leaves this function — the MIME map is
keyed by Bedrock's ``document.format`` enum (``pdf`` / ``docx`` / …) plus
``image``.

Token numbers are heuristics (``document_tokens.estimate_document_tokens``:
pages x the per-page image estimate for PDFs, bytes/4 otherwise; a flat figure
per image), not Bedrock counts: Bedrock reports no per-block usage. They are comparable across rows and against
``contextBreakdown.messages``, which is the measured total they are a share of.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agents.main_agent.session.compaction_policy import IMAGE_TOKEN_ESTIMATE
from apis.shared.files.document_tokens import estimate_document_tokens

#: A restore-time stand-in for a document that is no longer inline — today the
#: contentless placeholder ``_strip_document_bytes`` writes; from PR-3 the
#: digest block. Both count as "the model has a reference, not the bytes".
DIGEST_MARKERS = ("[Document placeholder:", "<document-digest")


def _inline_bytes(block: Dict[str, Any], key: str) -> Optional[int]:
    """Byte length of an inline ``document`` / ``image`` block, or ``None``
    when the block is not one (or carries no bytes)."""
    payload = block.get(key)
    if not isinstance(payload, dict):
        return None
    raw = (payload.get("source") or {}).get("bytes")
    if isinstance(raw, (bytes, bytearray)):
        return len(raw)
    return None


def _is_digest(block: Dict[str, Any]) -> bool:
    text = block.get("text")
    return isinstance(text, str) and text.lstrip().startswith(DIGEST_MARKERS)


def _is_turn_prompt(message: Dict[str, Any]) -> bool:
    """The user message that carries the turn's prompt (never a tool-result
    message, which is also ``role: user``)."""
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return isinstance(content, str)
    return not any(isinstance(b, dict) and "toolResult" in b for b in content)


def summarize_document_context(messages: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """The attachment footprint of ``messages`` (the live conversation).

    Returns ``None`` for an empty or malformed list, otherwise a dict with:

    - ``hasDocuments`` — at least one inline attachment (document or image
      bytes) is in context.
    - ``documentCount`` / ``documentTokens`` — inline attachment blocks on
      user prompts and their estimated token weight.
    - ``documentDigests`` — digest / placeholder stand-ins in context (a turn
      with digests and no inline documents answered from the digest).
    - ``documentsAttached`` — attachment blocks on the most recent prompt
      (an attach turn vs. a follow-up).
    - ``documentSlices`` / ``documentSliceTokens`` — document blocks that
      ``document_read`` returned inside tool results and that still live in
      the history (the re-injected pages, bounded by the tool's cap).
    - ``documentMime`` — ``{format: count}`` over the inline attachments,
      ``image`` for image blocks.
    """
    if not isinstance(messages, list) or not messages:
        return None

    count = tokens = digests = slices = slice_tokens = 0
    mime: Dict[str, int] = {}
    last_prompt_attachments = 0

    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        prompt = _is_turn_prompt(message)
        attached_here = 0
        for block in content:
            if not isinstance(block, dict):
                continue
            size = _inline_bytes(block, "document")
            if size is not None:
                count += 1
                attached_here += 1
                fmt = str((block.get("document") or {}).get("format") or "unknown")
                tokens += estimate_document_tokens(
                    fmt, ((block.get("document") or {}).get("source") or {}).get("bytes")
                )
                mime[fmt] = mime.get(fmt, 0) + 1
                continue
            size = _inline_bytes(block, "image")
            if size is not None:
                count += 1
                attached_here += 1
                tokens += IMAGE_TOKEN_ESTIMATE
                mime["image"] = mime.get("image", 0) + 1
                continue
            if _is_digest(block):
                digests += 1
                continue
            tool_result = block.get("toolResult")
            if isinstance(tool_result, dict):
                for inner in tool_result.get("content") or []:
                    if not isinstance(inner, dict):
                        continue
                    inner_size = _inline_bytes(inner, "document")
                    if inner_size is not None:
                        slices += 1
                        inner_doc = inner.get("document") or {}
                        slice_tokens += estimate_document_tokens(
                            inner_doc.get("format"), (inner_doc.get("source") or {}).get("bytes")
                        )
        if prompt:
            last_prompt_attachments = attached_here

    return {
        "hasDocuments": count > 0,
        "documentCount": count,
        "documentTokens": tokens,
        "documentDigests": digests,
        "documentsAttached": last_prompt_attachments,
        "documentSlices": slices,
        "documentSliceTokens": slice_tokens,
        "documentMime": mime,
    }


__all__ = ["DIGEST_MARKERS", "summarize_document_context"]
