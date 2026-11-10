"""Token estimates for the document blocks in a conversation, from row facts.

The agent-side half of ``apis.shared.files.document_estimate``: it finds the
row facts for a Converse document block and asks the shared estimator. Every
reader of a document's weight goes through :meth:`DocumentIndex.block_tokens`:
compaction's per-message estimate, the held-document count, offload, slice
ageing, rehydration and the cost row's document footprint
(``docs/specs/unified-attachment-delivery.md`` §9.1, phase 2).

How a block finds its facts:

* **An attached block** (top level of a user message) carries only a name,
  a format and its bytes. It is matched to a row with the restore path's own
  ``match_document``: the sanitized filename (allowing PromptBuilder's ``_2``
  duplicate suffix) and the byte size.
* **A ``document_read`` slice** sits in a tool result beside the tool's JSON
  payload, which names the ``upload_id`` and the pages returned. Its estimate is
  that share of the whole document, so an 8-page slice of a 24-page PDF is a
  third of it. Slices survive restore with their payload, so this works the
  same on a freshly restored agent.

A block with no facts falls back to ``estimate_document_tokens`` on its bytes
(pages x ``PDF_PAGE_TOKEN_ESTIMATE`` for a PDF, bytes/4 otherwise), memoized by
the bytes so a held document is parsed once per process.

Estimates only: nothing here changes a message.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

from apis.shared.files.document_estimate import DocumentTokenFacts, document_tokens

#: ``estimate_document_tokens`` by ``(format, hash of the bytes)``.
_FALLBACK: Dict[Tuple[str, int], int] = {}
_FALLBACK_MAX = 64


def _fallback_tokens(fmt: str, raw: Any) -> int:
    if not isinstance(raw, (bytes, bytearray)):
        return 0
    key = (fmt, hash(bytes(raw)))
    cached = _FALLBACK.get(key)
    if cached is not None:
        return cached
    tokens = document_tokens(fmt, raw)
    if len(_FALLBACK) >= _FALLBACK_MAX:
        _FALLBACK.clear()
    _FALLBACK[key] = tokens
    return tokens


def slice_reference(tool_result_content: Any) -> Tuple[Optional[str], Optional[int]]:
    """``(upload_id, pages returned)`` from a ``document_read`` result's JSON payload."""
    if not isinstance(tool_result_content, list):
        return None, None
    for inner in tool_result_content:
        payload = inner.get("json") if isinstance(inner, dict) else None
        if not isinstance(payload, dict) or not payload.get("upload_id"):
            continue
        pages = payload.get("pages_returned")
        if not isinstance(pages, int) or isinstance(pages, bool):
            span = payload.get("pages")
            if isinstance(span, dict) and isinstance(span.get("start"), int) and isinstance(span.get("end"), int):
                pages = span["end"] - span["start"] + 1
            else:
                pages = None
        return str(payload["upload_id"]), pages
    return None, None


class DocumentIndex:
    """The row facts a conversation's document blocks are estimated from."""

    def __init__(self, facts: Optional[Iterable[DocumentTokenFacts]] = None) -> None:
        self._facts = list(facts or ())
        self._by_id = {f.upload_id: f for f in self._facts}

    def __bool__(self) -> bool:
        return bool(self._facts)

    def for_upload(self, upload_id: Optional[str]) -> Optional[DocumentTokenFacts]:
        return self._by_id.get(upload_id) if upload_id else None

    def for_attached(self, name: str, fmt: str, size: int) -> Optional[DocumentTokenFacts]:
        if not self._facts:
            return None
        from agents.main_agent.session.document_rehydration import match_document

        return match_document(name, fmt, size, self._facts, set())

    def block_tokens(
        self,
        document: Any,
        *,
        upload_id: Optional[str] = None,
        pages_covered: Optional[int] = None,
    ) -> int:
        """Estimated tokens of one ``document`` payload. Never raises.

        ``upload_id`` and ``pages_covered`` describe a ``document_read`` slice
        (:func:`slice_reference`); without them the block is an attachment.
        """
        try:
            if not isinstance(document, dict):
                return 0
            source = document.get("source")
            raw = source.get("bytes") if isinstance(source, dict) else None
            if not isinstance(raw, (bytes, bytearray)):
                return 0
            fmt = str(document.get("format") or "").strip().lower()
            if upload_id:
                facts = self.for_upload(upload_id)
            else:
                facts = self.for_attached(str(document.get("name", "")), fmt, len(raw))
                pages_covered = None
            if facts is not None:
                return document_tokens(fmt, raw, facts, pages_covered=pages_covered)
            return _fallback_tokens(fmt, raw)
        except Exception:  # noqa: BLE001 - estimator must never raise
            return 0

    def unmatched(self, messages: Optional[Sequence[Dict[str, Any]]]) -> int:
        """Document blocks in ``messages`` that would fall back to the bytes."""
        missing = 0
        for document, upload_id, _pages in iter_document_blocks(messages):
            source = document.get("source") if isinstance(document, dict) else None
            raw = source.get("bytes") if isinstance(source, dict) else None
            if not isinstance(raw, (bytes, bytearray)):
                continue
            if upload_id:
                found = self.for_upload(upload_id)
            else:
                found = self.for_attached(
                    str(document.get("name", "")), str(document.get("format") or "").lower(), len(raw)
                )
            missing += found is None
        return missing


def iter_document_blocks(messages: Optional[Sequence[Dict[str, Any]]]):
    """Yield ``(document payload, slice upload_id or None, slice pages or None)``
    for every document block, attached or inside a tool result."""
    for message in messages or ():
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if "document" in block:
                yield block.get("document"), None, None
                continue
            result = block.get("toolResult")
            inner = result.get("content") if isinstance(result, dict) else None
            if not isinstance(inner, list):
                continue
            upload_id, pages = slice_reference(inner)
            for item in inner:
                if isinstance(item, dict) and "document" in item:
                    yield item.get("document"), upload_id, pages


#: The index with no facts: every block falls back to its bytes.
EMPTY_INDEX = DocumentIndex()


__all__ = ["DocumentIndex", "EMPTY_INDEX", "iter_document_blocks", "slice_reference"]
