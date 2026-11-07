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

A replaced block becomes a text block with the document's text: UTF-8 for
txt, md and csv, ``html_markdown`` for html, and ``docx_markdown`` for docx
(falling back to ``docx_paragraphs`` if that renderer cannot read the file).
Formats without a pure extractor (doc, xls, xlsx), and a docx that will not
parse, become a one-line stub that names the document.

Documents as text (``CONVERSE_DOCUMENTS_AS_TEXT_ENABLED``, in development)
--------------------------------------------------------------------------

With the flag on, the same projection sends **every** non-PDF document it can
render as text, not just the ones past the fifth, the way Claude's own product
treats files: PDFs stay native document blocks (Claude reads their pages),
everything else is text the deployment rendered. There is then no
document-count limit to reach, and the rendering is ours to improve. A
document with no renderer stays a native block and still counts toward the
cap.

Measured on dev (2026-11-06, Haiku 4.5 and Opus 5.5, Converse, two to three
trials per question): a native docx block and ``docx_markdown`` text answered
the same table, list and tracked-change questions equally (markdown slightly
ahead on nested lists, where Bedrock's own conversion drops the nesting), at
input tokens within a few percent of each other. ``docx_paragraphs`` text did
not: a table with blank cells lost its column alignment and both models gave
wrong or no values for 13 of 18 lookups. That is why the cap path renders
through ``docx_markdown`` too. Raw HTML cost 12x the native block's tokens, so
html goes through ``html_markdown``. See the PR that introduced the flag for
the full table.

The stored conversation is unchanged either way: the user message keeps its
document block, so the attachment card, restore's digest swap, offload's
upload matching and the share and handoff views all see what they always saw.
Only the request carries text.

Rendering happens at most once per document per process. The text is
memoized by the bytes' content (their hash, confirmed by equality), so the
inference route renders a turn's new attachments in worker threads before the
agent runs (``prerender_document_text``), and the projection on the event loop
finds the text ready. A miss, after eviction for example, renders in place and
gives the same text.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from apis.shared.models.history_projection import project_model_history

logger = logging.getLogger(__name__)

#: Non-PDF document blocks Bedrock's Converse API accepts in one request.
CONVERSE_MAX_NON_PDF_DOCUMENTS = 5

#: No cap: used when the cap's rollout switch is off but documents-as-text is on.
UNCAPPED = sys.maxsize

#: Formats that do not count toward the limit.
_EXEMPT_FORMATS = frozenset({"pdf"})
#: Formats whose bytes are the text.
_RAW_TEXT_FORMATS = frozenset({"txt", "md", "csv"})
#: Formats this module can render as text.
RENDERABLE_FORMATS = frozenset(_RAW_TEXT_FORMATS | {"html", "docx"})

#: Rendered text, keyed by ``(format, hash of the bytes)``. A tool round
#: re-sends the same request and the next turn re-sends the same history, so
#: the same block is projected many times; the inference route also renders a
#: turn's attachments ahead of time from its own copy of the bytes. The entry
#: holds the bytes it was rendered from and a hit is confirmed by equality
#: (``bytes.__eq__`` short-circuits on identity, and a ``bytes`` object caches
#: its hash), so a hash collision can never hand one document another's text.
#: Bounded by entry count and by total source bytes. Guarded by a lock: the
#: pre-render runs in worker threads.
_TEXT_CACHE: "OrderedDict[Tuple[str, int], Tuple[bytes, str]]" = OrderedDict()
_TEXT_CACHE_MAX_ENTRIES = 32
_TEXT_CACHE_MAX_BYTES = 32 * 1024 * 1024
_text_cache_bytes = 0
_TEXT_CACHE_LOCK = threading.Lock()


def _normalized_format(document: Dict[str, Any]) -> str:
    fmt = document.get("format")
    return fmt.strip().lower() if isinstance(fmt, str) and fmt.strip() else "txt"


def _counts(block: Any) -> bool:
    """True when ``block`` is a document block that counts toward the limit."""
    if not isinstance(block, dict):
        return False
    document = block.get("document")
    if not isinstance(document, dict):
        return False
    fmt = document.get("format")
    return not (isinstance(fmt, str) and fmt.strip().lower() in _EXEMPT_FORMATS)


def _cache_key(fmt: str, raw: bytes) -> Tuple[str, int]:
    return fmt, hash(raw)


def _cached_text(fmt: str, raw: bytes) -> Optional[str]:
    key = _cache_key(fmt, raw)
    with _TEXT_CACHE_LOCK:
        entry = _TEXT_CACHE.get(key)
        if entry is None or entry[0] != raw:
            return None
        if entry[0] is not raw:
            # Hold the caller's object from now on: the block in the live
            # history is what every later call asks about, so its equality
            # check becomes an identity check instead of a full compare.
            entry = (raw, entry[1])
            _TEXT_CACHE[key] = entry
        _TEXT_CACHE.move_to_end(key)
        return entry[1]


def _remember_text(fmt: str, raw: bytes, text: str) -> None:
    global _text_cache_bytes
    if len(raw) > _TEXT_CACHE_MAX_BYTES:
        return
    key = _cache_key(fmt, raw)
    with _TEXT_CACHE_LOCK:
        stale = _TEXT_CACHE.pop(key, None)
        if stale is not None:
            _text_cache_bytes -= len(stale[0])
        _TEXT_CACHE[key] = (raw, text)
        _text_cache_bytes += len(raw)
        while _TEXT_CACHE and (len(_TEXT_CACHE) > _TEXT_CACHE_MAX_ENTRIES or _text_cache_bytes > _TEXT_CACHE_MAX_BYTES):
            _, (old, _) = _TEXT_CACHE.popitem(last=False)
            _text_cache_bytes -= len(old)


def _render(fmt: str, raw: bytes) -> Optional[str]:
    if fmt in _RAW_TEXT_FORMATS:
        return raw.decode("utf-8", errors="replace")
    if fmt == "html":
        from apis.shared.files.html_markdown import html_to_markdown

        return html_to_markdown(raw.decode("utf-8", errors="replace"))
    if fmt != "docx":
        return None
    # Imported here: the document modules are large and the cap path is rare.
    from apis.shared.files.docx_markdown import docx_to_markdown

    try:
        return docx_to_markdown(raw)
    except Exception:  # noqa: BLE001 — try the plain extractor before giving up
        logger.info("document_cap: docx_markdown could not render a docx; trying docx_paragraphs", exc_info=True)
    from apis.shared.files.document_read import docx_paragraphs

    try:
        return "\n\n".join(docx_paragraphs(raw))
    except Exception:  # noqa: BLE001 — a corrupt file becomes a stub, never a failed turn
        logger.info("document_cap: a docx block did not parse; sending a stub", exc_info=True)
        return None


def _extract_text(fmt: str, raw: Any) -> Optional[str]:
    """The document's text, or ``None`` when there is no renderer for it."""
    if fmt not in RENDERABLE_FORMATS or not isinstance(raw, (bytes, bytearray)):
        return None
    raw = bytes(raw) if isinstance(raw, bytearray) else raw
    cached = _cached_text(fmt, raw)
    if cached is not None:
        return cached
    text = _render(fmt, raw)
    if text is not None:
        _remember_text(fmt, raw, text)
    return text


def prerender_document_text(fmt: str, raw: bytes) -> bool:
    """Render ``raw`` now so a later projection of the same bytes is a cache hit.

    For the inference route, which calls it in a worker thread for each of a
    turn's new non-PDF attachments, so the parse never runs on the event loop.
    Returns whether there is text for it. Never raises.
    """
    try:
        return _extract_text((fmt or "").strip().lower(), raw) is not None
    except Exception:  # noqa: BLE001 - the projection renders it again in place
        logger.info("document_cap: pre-render failed", exc_info=True)
        return False


def _display_name(document: Dict[str, Any]) -> str:
    name = document.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else "document"


def _envelope(name: str, fmt: str, text: str) -> str:
    # A document cannot end its own envelope early.
    body = text.replace("</document", "<\\/document")
    quoted = name.replace('"', "'")
    return f'<document name="{quoted}" format="{fmt}">\n{body}\n</document>'


def _stub(name: str, fmt: str) -> str:
    return (
        f'[Document "{name}" ({fmt}) is not shown here. A request can carry at most '
        f"{CONVERSE_MAX_NON_PDF_DOCUMENTS} non-PDF documents, and earlier messages already "
        "carry that many. If the file is in this session, a file tool may still read it.]"
    )


def _rendered_text(document: Dict[str, Any]) -> Optional[str]:
    """The envelope for ``document``'s text, or ``None`` with no renderer for it."""
    fmt = _normalized_format(document)
    source = document.get("source")
    raw = source.get("bytes") if isinstance(source, dict) else None
    text = _extract_text(fmt, raw)
    if text is None:
        return None
    return _envelope(_display_name(document), fmt, text)


def render_document_as_text(document: Dict[str, Any]) -> str:
    """The text block that stands in for one document block. Pure: same block, same text."""
    rendered = _rendered_text(document)
    if rendered is not None:
        return rendered
    return _stub(_display_name(document), _normalized_format(document))


class _Pass:
    """One projection pass: ``seen`` counts the documents that stay native and
    count toward the cap; ``capped`` and ``as_text`` count replacements."""

    __slots__ = ("limit", "as_text", "seen", "capped", "rendered")

    def __init__(self, limit: int, as_text: bool) -> None:
        self.limit = limit
        self.as_text = as_text
        self.seen = 0
        self.capped = 0
        self.rendered = 0

    def block(self, block: Any) -> Any:
        if _counts(block):
            if self.as_text:
                rendered = _rendered_text(block["document"])
                if rendered is not None:
                    self.rendered += 1
                    return {"text": rendered}
            self.seen += 1
            if self.seen > self.limit:
                self.capped += 1
                return {"text": render_document_as_text(block["document"])}
            return block
        tool_result = block.get("toolResult") if isinstance(block, dict) else None
        if not isinstance(tool_result, dict):
            return block
        items = tool_result.get("content")
        if not isinstance(items, list):
            return block
        projected = self.content(items)
        if projected is items:
            return block
        return {**block, "toolResult": {**tool_result, "content": projected}}

    def content(self, content: List[Any]) -> List[Any]:
        replaced: Optional[List[Any]] = None
        for i, block in enumerate(content):
            projected = self.block(block)
            if projected is not block:
                if replaced is None:
                    replaced = list(content)
                replaced[i] = projected
        return content if replaced is None else replaced


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


def project_document_cap(
    messages: List[Any],
    limit: int = CONVERSE_MAX_NON_PDF_DOCUMENTS,
    *,
    as_text: bool = False,
) -> List[Any]:
    """``messages`` with every non-PDF document after the first ``limit`` replaced by text.

    With ``as_text``, every non-PDF document that has a renderer is replaced by
    its text first, and only the rest count toward ``limit``.

    Returns ``messages`` itself when there is nothing to replace. Otherwise
    returns a new list that shares every untouched message with the input.
    """
    if _count_over(messages, 0 if as_text else limit) <= (0 if as_text else limit):
        return messages
    projection = _Pass(limit, as_text)
    replaced: Optional[List[Any]] = None
    for i, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        projected = projection.content(content)
        if projected is not content:
            if replaced is None:
                replaced = list(messages)
            replaced[i] = {**message, "content": projected}
    if projection.capped:
        logger.info(
            "document_cap: %d non-PDF documents in the request; %d sent as text",
            projection.seen,
            projection.capped,
        )
    return messages if replaced is None else replaced


def converse_document_cap(
    model: Any,
    *,
    limit: int = CONVERSE_MAX_NON_PDF_DOCUMENTS,
    as_text: bool = False,
) -> Any:
    """Make ``model`` see its message history through the document-cap projection."""
    return project_model_history(model, lambda messages: project_document_cap(messages, limit, as_text=as_text))
