"""Files shared between linked conversations, by reference (handoffs spec §4.9, PR-0.4a).

A file crosses from one of the user's conversations into a linked one as a **reference row**: an
ordinary ``FileMetadata`` row in the receiving conversation, with its own upload id, ``source =
"linked"``, ``delivery = "reference"``, ``status = READY``, the original's ``s3Key`` / ``s3Bucket``
/ ``filename`` / ``mimeType`` / ``sizeBytes`` / ``ttl``, and ``linkedFrom`` naming the original.
It carries no digest, preparation, KB index, facts or model image: derived state belongs to the
original and is read from there (``resolve_linked_file``). Bytes are never copied.

Because it is an ordinary row in the receiving conversation, everything that lists "this
conversation's files" sees it unchanged, including the ``document_read`` gate's query, so no gate
needs a second read before the first token.

The rules each row keeps (one test each, spec §10):

1. No quota on create or delete.
2. Deleting a reference never deletes bytes, the thumbnail or the ``_derived/`` sidecars.
3. Read-only: a tool that would change a file in place writes a new file in its own conversation.
4. Derived state comes from the original, through ``resolve_linked_file``.
5. A deleted original reads as gone (``linked_file_gone_message``), never ``NoSuchKey``.
6. Never inlined: a reference is never in a turn's ``file_upload_ids``.

A reference to a reference is made pointing at the original, so no chain is longer than one hop,
and a file handed back to the conversation it came from writes no row at all.

**Reference ids are deterministic** (``reference_upload_id``): the original's id plus the link (a
handoff or a delivery). The review sheet's preview and the live bubble can name the id the model
will read before the row exists, and a repeated write lands on the same row instead of a second.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence

from apis.shared.files.models import (
    DELIVERY_REFERENCE,
    SOURCE_LINKED,
    FileMetadata,
    FileStatus,
    LinkedFrom,
    is_linked_reference,
)

logger = logging.getLogger(__name__)

#: Files one handoff may carry into its target, and one delivery back (spec §4.9).
HANDOFF_MAX_FILES = 5
DELIVERY_MAX_FILES = 5

_REF_INFIX = "_ref_"


def reference_upload_id(original_upload_id: str, link_id: str) -> str:
    """The reference row's upload id: the original's id and the link's first 12 hex digits.

    Pure and mirrored in the SPA (``referenceUploadId`` in ``handoff-prompt.ts``), so the preview
    and the live bubble name the id the row is written under.
    """
    return f"{original_upload_id}{_REF_INFIX}{link_id.replace('-', '')[:12]}"


@dataclass(frozen=True)
class LinkedFileLine:
    """What a ``<file …/>`` line names. ``upload_id`` is the id readable in the receiving conversation."""

    upload_id: str
    filename: str
    mime_type: str
    size_bytes: int


@dataclass(frozen=True)
class OriginalRef:
    """A file as it crosses: the original it resolves to, and where that original lives."""

    upload_id: str
    session_id: str
    from_title: str
    filename: str
    mime_type: str
    size_bytes: int
    s3_key: str
    s3_bucket: str
    ttl: Optional[int]


def original_of(meta: FileMetadata, *, holder_title: str) -> OriginalRef:
    """The original a sender's file stands for: itself, or, for a reference, what it points at.

    No read: a reference row already carries the original's location and ``linkedFrom``. Whether
    the original still exists is left to the reader, which says "no longer available" when not.
    ``holder_title`` names the sender's conversation, used when ``meta`` is itself the original.
    """
    linked = meta.linked_from
    if linked is not None:
        return OriginalRef(
            upload_id=linked.upload_id,
            session_id=linked.session_id,
            from_title=linked.from_title,
            filename=meta.filename,
            mime_type=meta.mime_type,
            size_bytes=meta.size_bytes,
            s3_key=meta.s3_key,
            s3_bucket=meta.s3_bucket,
            ttl=meta.ttl,
        )
    return OriginalRef(
        upload_id=meta.upload_id,
        session_id=meta.session_id,
        from_title=holder_title,
        filename=meta.filename,
        mime_type=meta.mime_type,
        size_bytes=meta.size_bytes,
        s3_key=meta.s3_key,
        s3_bucket=meta.s3_bucket,
        ttl=meta.ttl,
    )


def linkable_refusal(meta: Optional[FileMetadata], *, user_id: str, sender_session_id: str) -> Optional[str]:
    """Why ``meta`` can't cross out of ``sender_session_id``, or ``None`` when it can.

    §4.8's sender rule applied to files: the caller's, ``READY``, and a row of the sending
    conversation. One reason for "another user's" and "absent", so a refusal never says which
    ids exist.
    """
    if meta is None or meta.user_id != user_id:
        return "not_found"
    if meta.session_id != sender_session_id:
        return "not_in_sender"
    status = meta.status if isinstance(meta.status, str) else meta.status.value
    if status != FileStatus.READY.value:
        return "not_ready"
    return None


def build_reference_row(
    original: OriginalRef,
    *,
    user_id: str,
    receiving_session_id: str,
    link_id: str,
) -> FileMetadata:
    """The reference row for ``original`` in ``receiving_session_id``. No IO."""
    now = datetime.now(timezone.utc)
    return FileMetadata(
        upload_id=reference_upload_id(original.upload_id, link_id),
        user_id=user_id,
        session_id=receiving_session_id,
        filename=original.filename,
        mime_type=original.mime_type,
        size_bytes=original.size_bytes,
        s3_key=original.s3_key,
        s3_bucket=original.s3_bucket,
        status=FileStatus.READY,
        source=SOURCE_LINKED,
        delivery=DELIVERY_REFERENCE,
        created_at=now,
        updated_at=now,
        ttl=original.ttl,
        linked_from=LinkedFrom(
            session_id=original.session_id, upload_id=original.upload_id, from_title=original.from_title
        ),
    )


def line_for(original: OriginalRef, *, receiving_session_id: str, link_id: str) -> LinkedFileLine:
    """The ``<file>`` line for ``original`` as the receiving conversation reads it.

    A file handed back to the conversation it came from names the original itself (no row is
    written); anything else names the reference row's id.
    """
    upload_id = (
        original.upload_id
        if original.session_id == receiving_session_id
        else reference_upload_id(original.upload_id, link_id)
    )
    return LinkedFileLine(
        upload_id=upload_id, filename=original.filename, mime_type=original.mime_type, size_bytes=original.size_bytes
    )


def rows_for(
    originals: Iterable[OriginalRef], *, user_id: str, receiving_session_id: str, link_id: str
) -> List[FileMetadata]:
    """The reference rows to write: one per original not already in the receiving conversation."""
    return [
        build_reference_row(o, user_id=user_id, receiving_session_id=receiving_session_id, link_id=link_id)
        for o in originals
        if o.session_id != receiving_session_id
    ]


def write_reference_rows(rows: Sequence[FileMetadata], repository=None) -> None:
    """One ``BatchWriteItem`` for every reference row of a handoff or a delivery. No quota change."""
    if not rows:
        return
    from apis.shared.files.repository import get_file_upload_repository

    (repository or get_file_upload_repository()).batch_put_files_sync(list(rows))


async def resolve_linked_file(meta: Optional[FileMetadata], repository=None) -> Optional[FileMetadata]:
    """The row whose bytes and derived state ``meta`` reads (rule 4).

    ``meta`` itself for an ordinary file. For a reference, the original's row, or ``None`` once the
    original is gone or no longer ``READY`` (rule 5; ``linked_file_gone_message`` says so).
    """
    if meta is None or not is_linked_reference(meta):
        return meta
    from apis.shared.files.repository import get_file_upload_repository

    linked = meta.linked_from
    assert linked is not None
    try:
        original = await (repository or get_file_upload_repository()).get_file(meta.user_id, linked.upload_id)
    except Exception:  # noqa: BLE001 - a failed read is "not available now", never a crash
        logger.warning("linked file: reading the original failed", exc_info=True)
        return None
    if original is None:
        return None
    status = original.status if isinstance(original.status, str) else original.status.value
    if status != FileStatus.READY.value or original.s3_key != meta.s3_key:
        return None
    return original


def as_read_through(reference: FileMetadata, original: FileMetadata) -> FileMetadata:
    """The original's bytes and derived state under the reference's identity.

    A reader works on this as on any row of its own conversation: the upload id, session and
    ``linkedFrom`` it reports and checks are the reference's, while ``s3Key``, size, status,
    digest, preparation, KB index, facts and model image are the original's (rule 4).
    """
    return original.model_copy(
        update={
            "upload_id": reference.upload_id,
            "session_id": reference.session_id,
            "user_id": reference.user_id,
            "source": reference.source,
            "linked_from": reference.linked_from,
            "created_at": reference.created_at,
        }
    )


async def readable_view(meta: FileMetadata, *, reader: str, repository=None) -> Optional[FileMetadata]:
    """What a reader reads for ``meta``: itself, or a reference read through to its original.

    ``None`` for a reference whose original is gone (rule 5); the caller answers with
    ``linked_file_gone_message``. Counts ``LinkedFileRead`` / ``LinkedFileGone`` by ``reader``.
    """
    if not is_linked_reference(meta):
        return meta
    original = await resolve_linked_file(meta, repository)
    if original is None:
        emit_linked_file_metric("LinkedFileGone", reader=reader, session_id=meta.session_id)
        return None
    emit_linked_file_metric("LinkedFileRead", reader=reader, session_id=meta.session_id)
    return as_read_through(meta, original)


def linked_file_gone_message(meta: FileMetadata) -> str:
    """What a listing or reader says for a reference whose original was deleted (rule 5)."""
    title = (meta.linked_from.from_title if meta.linked_from else "") or "another conversation"
    return f"No longer available (deleted from {title})"


def originals_available(metas: Sequence[FileMetadata], repository=None) -> Dict[str, bool]:
    """For each reference among ``metas``, whether its original still exists. One batched read.

    Keyed by the reference's upload id; ordinary files are absent from the map.
    """
    refs = [m for m in metas if is_linked_reference(m)]
    if not refs:
        return {}
    from apis.shared.files.repository import get_file_upload_repository

    repository = repository or get_file_upload_repository()
    by_user: Dict[str, List[FileMetadata]] = {}
    for m in refs:
        by_user.setdefault(m.user_id, []).append(m)
    result: Dict[str, bool] = {}
    for user_id, group in by_user.items():
        try:
            found = repository.batch_get_files_sync(user_id, [m.linked_from.upload_id for m in group])  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - unknown reads as available; the reader still says gone
            logger.warning("linked file: availability read failed", exc_info=True)
            continue
        for m in group:
            original = found.get(m.linked_from.upload_id)  # type: ignore[union-attr]
            result[m.upload_id] = original is not None and original.s3_key == m.s3_key
    return result


def emit_linked_file_metric(name: str, *, reader: str, session_id: Optional[str]) -> None:
    """``LinkedFileRead`` / ``LinkedFileGone`` by reader (§4.7). Never raises."""
    try:
        from apis.shared.handoff.metrics import emit_handoff_metric

        emit_handoff_metric(name, "linked_file", session_id=session_id, detail=reader)
    except Exception:  # noqa: BLE001
        logger.debug("linked file metric failed", exc_info=True)


__all__ = [
    "DELIVERY_MAX_FILES",
    "HANDOFF_MAX_FILES",
    "LinkedFileLine",
    "OriginalRef",
    "as_read_through",
    "build_reference_row",
    "emit_linked_file_metric",
    "line_for",
    "linkable_refusal",
    "linked_file_gone_message",
    "original_of",
    "originals_available",
    "readable_view",
    "reference_upload_id",
    "resolve_linked_file",
    "rows_for",
    "write_reference_rows",
]
