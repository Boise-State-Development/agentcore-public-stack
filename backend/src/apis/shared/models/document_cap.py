"""Keep a Converse request within Bedrock's five-document limit.

Bedrock's Converse API rejects a request that carries more than five
**non-PDF** document blocks ("You can't include more than 5 documents in a
request"). The count is over the whole request: every message in the history,
and the documents inside ``toolResult`` content too. PDFs do not count. Probed on
dev, 2026-11-06, on Claude Haiku 4.5, Sonnet 4.6, Haiku 5.5 and Opus 5.5 and
on Nova Lite 2 and Nova Pro: six PDFs pass, and so do five txt plus five PDFs. Six txt, md, csv or
html blocks fail, whether in one message, split across two turns, or in tool
results. Prod agrees: no successful Claude call in 30 days carried more than
five non-PDF documents, though some carried 20 PDFs.

The composer caps a *message* at five attachments, but not a *conversation*.
On a warm session an earlier turn's Word or text attachments stay inline,
because offload waits until the prefix re-write is free. So a second turn that
attaches more files can push the request past five, and then the turn is
force-stopped. The failed user message stays in the history, so every retry
fails the same way.

The fix is a projection at the model seam, per request, beside
``text_only_input``. It shares that projection's properties:

- **Never mutates.** The message list is aliased across every agent serving
  the session (#741). Only the lists and dicts on the path to a replaced block
  are copied.
- **A request that fits is untouched.** With five or fewer non-PDF documents,
  the input list itself is returned, so every request that succeeds today is
  sent byte for byte as before.
- **Prefix-stable.** Blocks are counted in message order and the first five
  are kept. Whether a block is replaced depends only on the blocks *before* it,
  and its replacement depends only on the block. So a projected prefix is
  byte-identical when later turns append to it, which is the prompt-cache
  contract. A slot frees only when something earlier is re-written anyway: an
  offload to a digest, a compaction cut, or a rewind. The block that moves back
  inline sits after that change, so it costs no extra re-write. The newest
  documents are the ones converted. A converted block never reached the model
  as a document, because that request failed, so no cached prefix holds it.

A replaced block becomes a text block with the document's text, from the same
extractors ``document_read`` uses: UTF-8 for txt, md, csv and html, and
``docx_paragraphs`` for docx. Bedrock turns a non-PDF document into text before
the model sees it, so the model reads nearly the same content. Formats without
a pure extractor (doc, xls, xlsx), and a docx that will not parse, become a
one-line stub that names the document.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from apis.shared.models.history_projection import project_model_history

logger = logging.getLogger(__name__)

#: Non-PDF document blocks Bedrock's Converse API accepts in one request.
CONVERSE_MAX_NON_PDF_DOCUMENTS = 5

#: Formats that do not count toward the limit.
_EXEMPT_FORMATS = frozenset({"pdf"})
#: Formats whose bytes are the text.
_TEXT_FORMATS = frozenset({"txt", "md", "csv", "html"})

#: Extracted text, keyed by the identity of the bytes it came from. A tool
#: round re-sends the same request and the next turn re-sends the same history,
#: so the same block is projected many times. The entry holds the bytes
#: object itself. That keeps its ``id`` from being reused while the entry
#: lives, and ``is`` proves the content is the same. Bounded by entry count
#: and by total source bytes.
_TEXT_CACHE: "OrderedDict[int, Tuple[bytes, str]]" = OrderedDict()
_TEXT_CACHE_MAX_ENTRIES = 32
_TEXT_CACHE_MAX_BYTES = 32 * 1024 * 1024
_text_cache_bytes = 0


def _counts(block: Any) -> bool:
    """True when ``block`` is a document block that counts toward the limit."""
    if not isinstance(block, dict):
        return False
    document = block.get("document")
    if not isinstance(document, dict):
        return False
    fmt = document.get("format")
    return not (isinstance(fmt, str) and fmt.strip().lower() in _EXEMPT_FORMATS)


def _cached_text(raw: bytes) -> Optional[str]:
    entry = _TEXT_CACHE.get(id(raw))
    if entry is None or entry[0] is not raw:
        return None
    _TEXT_CACHE.move_to_end(id(raw))
    return entry[1]


def _remember_text(raw: bytes, text: str) -> None:
    global _text_cache_bytes
    if len(raw) > _TEXT_CACHE_MAX_BYTES:
        return
    stale = _TEXT_CACHE.pop(id(raw), None)
    if stale is not None:
        _text_cache_bytes -= len(stale[0])
    _TEXT_CACHE[id(raw)] = (raw, text)
    _text_cache_bytes += len(raw)
    while _TEXT_CACHE and (len(_TEXT_CACHE) > _TEXT_CACHE_MAX_ENTRIES or _text_cache_bytes > _TEXT_CACHE_MAX_BYTES):
        _, (old, _) = _TEXT_CACHE.popitem(last=False)
        _text_cache_bytes -= len(old)


def _extract_text(fmt: str, raw: bytes) -> Optional[str]:
    """The document's text, or ``None`` when there is no pure extractor for it."""
    if fmt in _TEXT_FORMATS:
        return raw.decode("utf-8", errors="replace")
    if fmt != "docx":
        return None
    cached = _cached_text(raw)
    if cached is not None:
        return cached
    # Imported here: document_read is a large module and this path is rare.
    from apis.shared.files.document_read import docx_paragraphs

    try:
        text = "\n\n".join(docx_paragraphs(raw))
    except Exception:  # noqa: BLE001 — a corrupt file becomes a stub, never a failed turn
        logger.info("document_cap: a docx block did not parse; sending a stub", exc_info=True)
        return None
    _remember_text(raw, text)
    return text


def _display_name(document: Dict[str, Any]) -> str:
    name = document.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else "document"


def render_document_as_text(document: Dict[str, Any]) -> str:
    """The text block that stands in for one document block. Pure: same block, same text."""
    name = _display_name(document)
    fmt_value = document.get("format")
    fmt = fmt_value.strip().lower() if isinstance(fmt_value, str) and fmt_value.strip() else "txt"
    source = document.get("source")
    raw = source.get("bytes") if isinstance(source, dict) else None
    text = _extract_text(fmt, raw) if isinstance(raw, (bytes, bytearray)) else None
    if text is None:
        return (
            f'[Document "{name}" ({fmt}) is not shown here. A request can carry at most '
            f"{CONVERSE_MAX_NON_PDF_DOCUMENTS} non-PDF documents, and earlier messages already "
            "carry that many. If the file is in this session, a file tool may still read it.]"
        )
    return f'<document name="{name}" format="{fmt}">\n{text}\n</document>'


def _project_block(block: Any, seen: int, limit: int) -> Tuple[Any, int]:
    if _counts(block):
        seen += 1
        if seen > limit:
            return {"text": render_document_as_text(block["document"])}, seen
        return block, seen
    tool_result = block.get("toolResult") if isinstance(block, dict) else None
    if not isinstance(tool_result, dict):
        return block, seen
    items = tool_result.get("content")
    if not isinstance(items, list):
        return block, seen
    projected, seen = _project_content(items, seen, limit)
    if projected is items:
        return block, seen
    return {**block, "toolResult": {**tool_result, "content": projected}}, seen


def _project_content(content: List[Any], seen: int, limit: int) -> Tuple[List[Any], int]:
    replaced: Optional[List[Any]] = None
    for i, block in enumerate(content):
        projected, seen = _project_block(block, seen, limit)
        if projected is not block:
            if replaced is None:
                replaced = list(content)
            replaced[i] = projected
    return (content if replaced is None else replaced), seen


def _count_over(messages: List[Any], limit: int) -> int:
    """Count the blocks that count toward the limit, stopping once past it.

    The fast path every Converse call takes, so it sits on the TTFT path:
    nearly every request carries no non-PDF document at all. A flat scan with
    no copies, membership tests before any lookup, and a function call only
    for an actual document block.
    """
    total = 0
    for message in messages:
        if type(message) is not dict:
            continue
        content = message.get("content")
        if type(content) is not list:
            continue
        for block in content:
            if type(block) is not dict:
                continue
            if "document" in block:
                if _counts(block):
                    total += 1
                    if total > limit:
                        return total
            elif "toolResult" in block:
                tool_result = block["toolResult"]
                items = tool_result.get("content") if type(tool_result) is dict else None
                if type(items) is not list:
                    continue
                for item in items:
                    if type(item) is dict and "document" in item and _counts(item):
                        total += 1
                if total > limit:
                    return total
    return total


def project_document_cap(messages: List[Any], limit: int = CONVERSE_MAX_NON_PDF_DOCUMENTS) -> List[Any]:
    """``messages`` with every non-PDF document after the first ``limit`` replaced by text.

    Returns ``messages`` itself when it carries ``limit`` or fewer. Otherwise
    returns a new list that shares every untouched message with the input.
    """
    if _count_over(messages, limit) <= limit:
        return messages
    seen = 0
    replaced: Optional[List[Any]] = None
    for i, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        projected, seen = _project_content(content, seen, limit)
        if projected is not content:
            if replaced is None:
                replaced = list(messages)
            replaced[i] = {**message, "content": projected}
    if replaced is not None:
        logger.info(
            "document_cap: %d non-PDF documents in the request; %d sent as text",
            seen,
            seen - limit,
        )
    return messages if replaced is None else replaced


def converse_document_cap(model: Any) -> Any:
    """Make ``model`` see its message history through the document-cap projection."""
    return project_model_history(model, project_document_cap)
