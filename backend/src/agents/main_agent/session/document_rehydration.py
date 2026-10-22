"""Rehydrate stripped documents on restore — digest plus a live handle, not a
contentless placeholder (docs/specs/document-context-offload.md §4E, PR-3).

Restore must drop inline document bytes from history: Bedrock rejects any
request where two document blocks share a name across the conversation, and
a re-attached file collides with the copy already in history. Until PR-3 the
replacement was ``[Document placeholder: name=…, format=…, original_size=…]``
— zero content — so a returning user's document was silently gone (the 14%
same-file re-upload rate). This module replaces the bytes with the document's
``DocumentDigest`` rendered as a ``<document-digest …>`` block that names the
``upload_id`` the ``document_read`` tool (PR-1) reads pages back from.

How a block finds its file: the session's upload rows (``SessionIndex``, one
query per restore that has a document to rehydrate, none otherwise) are
matched on the sanitized filename — the same sanitizer ``PromptBuilder`` used
when the block was built, allowing for its ``_2`` / ``_3`` duplicate suffix —
and the byte size. A block with no matching row (a direct base64 attachment
that never went through the upload flow, or a deleted file) keeps today's
placeholder, so nothing here can be worse than before.

Digests missing from the row (uploads that predate PR-2, agent-written files)
are built lazily from the bytes that are *right there* in the restored
message — the outline only, no model call on the restore path — and
persisted so the next restore renders the same bytes. The upload-path build
(PR-2) may later overwrite an outline-only digest with one that has an
abstract; that changes the rendered block once, which is one prefix
re-write, accepted and recorded (``document_rehydrated`` carries the digest
tokens, so the change is visible).

When ``DOCUMENT_READ_ENABLED=false`` has taken the tool away the digest is
still rendered — it is strictly more than the placeholder — but without the
``upload_id`` handle, so the model is never invited to call a tool it does
not have. The live offload path stops entirely in that case; see
``document_offload.offload_enabled_for``.

Everything is synchronous: it runs inside ``TurnBasedSessionManager.initialize``
under the Strands agent constructor. It never raises — every failure falls
back to the placeholder for that block.
"""

from __future__ import annotations

import copy
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from agents.main_agent.multimodal.file_sanitizer import FileSanitizer
from agents.main_agent.session.compaction_policy import CHARS_PER_TOKEN
from apis.shared.files.document_tokens import estimate_document_tokens

logger = logging.getLogger(__name__)

_DUPLICATE_SUFFIX = re.compile(r"_(\d+)$")


def document_rehydration_enabled() -> bool:
    """Default ON with a kill switch (house style): ``DOCUMENT_REHYDRATE_ENABLED=false``
    restores today's placeholder behavior without touching PR-1/PR-2."""
    return os.environ.get("DOCUMENT_REHYDRATE_ENABLED", "").strip().lower() != "false"


def placeholder_text(name: str, fmt: str, size: int) -> str:
    """The pre-PR-3 stand-in, kept byte-identical for blocks that cannot be
    matched to an upload (restore output must be stable across restores)."""
    return f"[Document placeholder: name={name}, format={fmt}, original_size={size} bytes]"


@dataclass
class RehydrationResult:
    messages: List[Dict[str, Any]]
    rehydrated: int = 0
    stripped: int = 0
    digest_tokens: int = 0
    stripped_tokens: int = 0
    lazy_digests: int = 0
    upload_ids: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _base_name(block_name: str) -> str:
    """``"policy_2"`` → ``"policy"`` (PromptBuilder's duplicate suffix)."""
    return _DUPLICATE_SUFFIX.sub("", block_name)


def match_document(
    name: str,
    fmt: str,
    size: int,
    candidates: List[Any],
    used: Set[str],
) -> Optional[Any]:
    """The upload row a document block came from, or ``None``.

    Preference order: same sanitized filename **and** same byte size; then
    same filename; then same format and size (a renamed re-upload). Rows
    already claimed by another block in this restore are skipped, so two
    copies of one file map to two rows, newest first.
    """
    sanitize = FileSanitizer.sanitize_filename
    wanted = {name, _base_name(name)}
    fresh = [c for c in candidates if c.upload_id not in used]
    by_name = [c for c in fresh if sanitize(c.filename) in wanted]
    for c in by_name:
        if c.size_bytes == size:
            return c
    if by_name:
        return by_name[0]
    for c in fresh:
        if c.size_bytes == size and (c.file_format or "") == fmt:
            return c
    return None


# ---------------------------------------------------------------------------
# Data access (sync — see module docstring)
# ---------------------------------------------------------------------------


def load_session_documents(session_id: str, user_id: Optional[str]) -> List[Any]:
    """This user's READY document-class uploads in the session, newest first."""
    from apis.shared.files.document_read import is_document_class
    from apis.shared.files.models import FileStatus
    from apis.shared.files.repository import get_file_upload_repository

    rows = get_file_upload_repository().list_session_files_sync(session_id, status=FileStatus.READY)
    return [
        meta for meta in rows
        if (not user_id or meta.user_id == user_id) and is_document_class(meta.mime_type, meta.filename)
    ]


def digest_for(meta: Any, raw: bytes) -> Optional[Any]:
    """The row's ready ``DocumentDigest``, or one built now from ``raw``
    (outline only) and persisted best-effort. ``None`` if neither works."""
    from apis.shared.files.document_digest import DocumentDigest, estimate_tokens, extract_outline, render_digest
    from apis.shared.files.repository import get_file_upload_repository

    existing = DocumentDigest.from_item(getattr(meta, "digest", None))
    if existing is not None and existing.status == "ready":
        return existing

    fmt = meta.file_format
    if not fmt:
        return None
    digest = extract_outline(fmt, raw)
    digest.tokens = estimate_tokens(render_digest(digest, filename=meta.filename, upload_id=meta.upload_id))
    try:
        get_file_upload_repository().update_file_digest_sync(meta.user_id, meta.upload_id, digest.to_item())
    except Exception:  # noqa: BLE001 - the digest still serves this restore
        logger.debug("Lazy digest not persisted for upload %s", meta.upload_id, exc_info=True)
    return digest


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


def rehydrate_documents(
    messages: List[Dict[str, Any]],
    *,
    session_id: Optional[str],
    user_id: Optional[str],
) -> RehydrationResult:
    """Replace every inline document block with its digest block (matched to
    an upload row) or, failing that, the placeholder. Never raises."""
    from apis.shared.files.document_digest import render_digest

    from apis.shared.feature_flags import document_read_enabled

    out = copy.deepcopy(messages)
    result = RehydrationResult(messages=out)
    enabled = document_rehydration_enabled() and bool(session_id)
    # Restore has to drop the bytes either way (Bedrock rejects duplicate
    # document names), so a digest is still strictly better than the
    # placeholder when the tool is off — but it must not advertise a handle
    # the model cannot use.
    handle = document_read_enabled()
    candidates: Optional[List[Any]] = None
    used: Set[str] = set()

    for msg in out:
        content = msg.get("content", []) if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for idx, block in enumerate(content):
            if not isinstance(block, dict) or "document" not in block:
                continue
            doc = block["document"] if isinstance(block["document"], dict) else {}
            source = doc.get("source") or {}
            if not isinstance(source, dict) or "bytes" not in source:
                continue
            raw = source.get("bytes", b"")
            size = len(raw) if isinstance(raw, (bytes, bytearray)) else 0
            name = str(doc.get("name", "unknown"))
            fmt = str(doc.get("format", "unknown"))

            replacement: Optional[str] = None
            if enabled:
                try:
                    if candidates is None:
                        candidates = load_session_documents(session_id, user_id)
                    meta = match_document(name, fmt, size, candidates, used)
                    if meta is not None:
                        digest = digest_for(meta, bytes(raw) if isinstance(raw, (bytes, bytearray)) else b"")
                        if digest is not None:
                            was_lazy = not (isinstance(getattr(meta, "digest", None), dict) and meta.digest.get("status") == "ready")
                            replacement = render_digest(
                                digest, filename=meta.filename, upload_id=meta.upload_id,
                                include_handle=handle,
                            )
                            used.add(meta.upload_id)
                            result.rehydrated += 1
                            result.digest_tokens += len(replacement) // CHARS_PER_TOKEN
                            result.lazy_digests += 1 if was_lazy else 0
                            result.upload_ids.append(meta.upload_id)
                except Exception:  # noqa: BLE001 - fall back to the placeholder for this block
                    logger.warning("Document rehydration failed for one block; using placeholder", exc_info=True)
                    if candidates is None:
                        candidates = []  # do not retry the lookup for every block

            if replacement is None:
                replacement = placeholder_text(name, fmt, size)
                result.stripped += 1
                # Same estimator the row and the offloader use, so the
                # ``document_stripped`` ledger event is comparable to
                # ``document_offload`` / ``document_rehydrated``.
                result.stripped_tokens += estimate_document_tokens(fmt, raw)
            content[idx] = {"text": replacement}

    return result


__all__ = [
    "RehydrationResult",
    "digest_for",
    "document_rehydration_enabled",
    "load_session_documents",
    "match_document",
    "placeholder_text",
    "rehydrate_documents",
]
