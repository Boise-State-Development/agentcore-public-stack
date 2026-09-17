"""``document_read`` — page-range and pattern retrieval over the session's documents.

The recovery half of ``docs/specs/document-context-offload.md`` (§4B). A
document the user attached lives in the model's context only until history
is restored (restore strips inline bytes; see
``TurnBasedSessionManager._strip_document_bytes``) or, later, until the
offload trigger swaps it for a digest. This tool is how the model gets the
part it needs back — a PDF page range as a native ``document`` block (full
fidelity: text layer plus page images), a regex over the text layer to find
the right pages, or bounded text for text-family documents.

Design notes
------------
* **Gated on session state, not on the tool picker.** The tool is built for
  any session that has a readable document (``_build_document_tools`` in
  ``apis/inference_api/chat/routes.py``), whatever the user's RBAC grants.
  The ``workspace_files`` catalog key is granted to no prod role, so gating
  there would ship the recovery path dark. Its id therefore stays OUT of
  ``INJECTED_TOOL_IDS`` — same reasoning as the Memory-Space tools: the
  governing capability is the user's own attachment.
* Identity is captured by closure (``make_document_read_tool``), never read
  from ``invocation_state``.
* One call can never re-inject a whole document: page ranges are capped by
  ``max_pages`` (default 8, hard cap 20), pattern results by match count,
  text by the workspace read bound.
* **Content-free record per call** in ``AgentCoreStack/Compaction``
  (``DocumentRead``, ``DocumentReadPages``, ``DocumentReadBytes``; properties
  ``mode`` / ``format``) — the same namespace as the compaction cut and
  tool-result offload records, so one dashboard explains what bounded a
  session's prefix. Nothing from the document is ever emitted.
* Tool results carrying a native ``document`` block are exempt from the
  tool-result offloader (``core/tool_result_offload.py``): offloading the
  slice the model just asked for would defeat the read.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from strands import tool

from apis.shared.files.document_read import (
    DOCUMENT_READ_HARD_MAX_PAGES,
    DOCUMENT_READ_MAX_PAGES,
    DocumentReadResult,
    list_session_documents,
    read_document,
)
from apis.shared.files.workspace import WorkspaceError, WorkspaceStorageNotConfiguredError

logger = logging.getLogger(__name__)

#: The tool's name — also the census / ledger key and the offloader exemption.
DOCUMENT_READ_TOOL_NAME = "document_read"

_NO_STORAGE_MESSAGE = (
    "❌ Document storage is not configured (S3_USER_FILES_BUCKET_NAME is not set on the runtime)."
)


def _error(text: str) -> Dict[str, Any]:
    return {"content": [{"text": text}], "status": "error"}


def _success(result: DocumentReadResult) -> Dict[str, Any]:
    content = [{"json": result.payload}]
    if result.document_block is not None:
        content.append(result.document_block)
    content.extend(result.extra_blocks)
    return {"content": content, "status": "success"}


def record_document_read(result: DocumentReadResult) -> None:
    """One content-free EMF record per successful read. Never raises."""
    logger.info(
        "document_read: mode=%s format=%s pages=%d bytes=%d",
        result.mode, result.format, result.pages_returned, result.bytes_returned,
    )
    try:
        from apis.shared.observability.prompt_cache import prompt_cache_observability_enabled
        from apis.shared.observability.emf import emit_emf_metrics

        if not prompt_cache_observability_enabled():
            return
        emit_emf_metrics(
            "AgentCoreStack/Compaction",
            metrics={
                "DocumentRead": 1,
                "DocumentReadPages": int(result.pages_returned),
                "DocumentReadBytes": int(result.bytes_returned),
            },
            properties={"mode": result.mode, "format": result.format},
            units={"DocumentReadPages": "Count", "DocumentReadBytes": "Bytes"},
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("document_read EMF skipped: %s", e)


def make_document_read_tool(session_id: str, user_id: str):
    """Create a ``document_read`` tool bound to the given identity."""
    if not session_id or not user_id:
        raise ValueError("document_read requires a session_id and a user_id")

    @tool(name=DOCUMENT_READ_TOOL_NAME)
    async def document_read(
        upload_id: str = "",
        page_range: str = "",
        pattern: str = "",
        max_pages: int = DOCUMENT_READ_MAX_PAGES,
        offset: int = 0,
    ) -> Any:
        """Read part of a document the user attached to this conversation.

        Use this when a document you need is no longer inline (its content
        was replaced by a placeholder or digest after the conversation was
        restored) or when you need specific pages of a long document.

        Modes, by argument:
        - No arguments: list this conversation's readable documents with
          their upload_id, format and size.
        - upload_id + page_range (PDF only, e.g. "4-7"): returns those pages
          as an attached document at full fidelity (text and page images),
          at most max_pages per call. The attachment's own page k is
          original page start+k-1 — cite original page numbers.
        - upload_id + pattern: case-insensitive regex over the text; returns
          matching lines with page numbers (PDF) or line/paragraph numbers
          (text, Word), so you can then read the right pages. Use this first
          on a long document instead of paging through it.
        - upload_id alone: for a PDF, the page count and a per-page snippet
          index; for text and Word documents, the text itself (bounded —
          continue with offset = next_offset when truncated).

        Prefer pattern then a narrow page_range over reading many pages.
        Spreadsheets are read with analyze_spreadsheet and presentations
        with read_powerpoint_presentation, not here.

        Args:
            upload_id: The document's id (from the listing, the attachment
                note, or an earlier call). Empty to list documents.
            page_range: 1-indexed inclusive page range for PDFs, "start-end"
                or a single page "5".
            pattern: Regular expression (case-insensitive) to search for.
            max_pages: Cap on pages returned by page_range (default 8, hard
                cap 20).
            offset: Byte offset to continue a truncated text read.

        Returns:
            JSON metadata, plus the requested pages as an attached document
            for page_range reads.
        """
        try:
            if not upload_id:
                listing = await list_session_documents(user_id, session_id)
                listing["hint"] = (
                    "Call again with upload_id plus page_range (PDF), pattern, or neither."
                )
                result = DocumentReadResult(mode="list", payload=listing)
            else:
                result = await read_document(
                    user_id,
                    session_id,
                    upload_id,
                    page_range=page_range or None,
                    pattern=pattern or None,
                    max_pages=max_pages,
                    offset=offset,
                )
        except WorkspaceStorageNotConfiguredError:
            return _error(_NO_STORAGE_MESSAGE)
        except WorkspaceError as exc:
            return _error(f"❌ {exc}")
        except Exception as exc:  # noqa: BLE001 - surface conversationally, never raise through the loop
            logger.error("document_read error: %s", exc, exc_info=True)
            return _error(f"❌ Failed to read document '{upload_id}': {exc}")

        record_document_read(result)
        return _success(result)

    return document_read


__all__ = [
    "DOCUMENT_READ_HARD_MAX_PAGES",
    "DOCUMENT_READ_MAX_PAGES",
    "DOCUMENT_READ_TOOL_NAME",
    "make_document_read_tool",
    "record_document_read",
]
