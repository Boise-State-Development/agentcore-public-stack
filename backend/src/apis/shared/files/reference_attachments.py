"""Reference attachments: large documents a turn carries as text, never as bytes.

``docs/specs/large-document-attachments.md`` §4B. A reference-class upload
(``is_reference_document``: a pdf, docx, txt, md or html file over 4 MB and up to
50 MB) never rides inline. The resolver returns its row without fetching the
object, the turn partitions it into its own list, and the user message carries one
block per file:

- its stored digest with the ``document_read`` handle, when preparation is ready;
- otherwise a fixed ``<document-reference … status="…"/>`` stub.

**The turn path never builds a digest.** Building one for a 50 MB file is seconds
of CPU before the first token; the ``large-document-prepare`` Lambda builds it off
the request path.

**Prompt-cache contract (§4B.5).** The block is rendered once, into the persisted
user message, and is a pure function of the row as it was read on that turn. A
digest that becomes ready later never rewrites the earlier message (the model gets
it from ``document_read``'s index mode instead), and restore finds only text, no
``document`` block to strip or rehydrate, so history stays byte-stable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence

from apis.shared.files import large_document
from apis.shared.files.document_digest import DocumentDigest, render_digest
from apis.shared.files.models import FileMetadata

logger = logging.getLogger(__name__)

#: ``status`` on the stub, from the row's ``preparation``. A row with no
#: preparation reads as preparing: ``complete_upload`` writes ``pending`` before
#: it returns, so by the time the SPA can send the file the row says so.
STUB_STATUS_PREPARING = "preparing"
STUB_STATUS_READY = "ready"
STUB_STATUS_FAILED = "failed"


@dataclass
class ReferenceAttachment:
    """One reference-class upload on this turn: its row, and no bytes.

    Duck-types the ``filename`` / ``content_type`` / ``bytes`` shape the route's
    attachment pipeline reads, so dedupe, the file cap and the marker names treat
    it like any other attachment. ``bytes`` is always empty: a reference adds
    nothing to the inline byte budget. Deliberately not a ``FileContent``, which
    is the request body's schema; nothing a client sends can become one of these.
    """

    meta: FileMetadata

    @property
    def filename(self) -> str:
        return self.meta.filename

    @property
    def content_type(self) -> str:
        return self.meta.mime_type

    @property
    def bytes(self) -> str:
        return ""

    @property
    def upload_id(self) -> str:
        return self.meta.upload_id

    @property
    def size_bytes(self) -> int:
        return self.meta.size_bytes

    @property
    def format(self) -> Optional[str]:
        from apis.shared.files.document_read import document_format_for

        return document_format_for(self.meta.mime_type, self.meta.filename)

    def ready_digest(self) -> Optional[DocumentDigest]:
        """The stored digest, when preparation is ready and the digest built."""
        if not large_document.is_prepared(self.meta):
            return None
        digest = DocumentDigest.from_item(self.meta.digest)
        if digest is None or digest.status != "ready":
            return None
        return digest

    def stub_status(self) -> str:
        status = large_document.preparation_status(self.meta)
        if status == large_document.STATUS_READY:
            return STUB_STATUS_READY
        if status == large_document.STATUS_FAILED:
            return STUB_STATUS_FAILED
        return STUB_STATUS_PREPARING


def _attr(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def render_reference(ref: ReferenceAttachment, *, include_handle: bool) -> str:
    """The digest with its handle, or the fixed stub. Pure: same row, same text.

    ``include_handle=False`` omits ``upload_id`` when ``document_read`` is not on
    this turn, for the same reason ``render_digest`` does: advertising a handle
    for a tool the model does not have invites a call that cannot be made.
    """
    digest = ref.ready_digest()
    if digest is not None:
        return render_digest(
            digest, filename=ref.filename, upload_id=ref.upload_id, include_handle=include_handle
        )
    handle = f' upload_id="{_attr(ref.upload_id)}"' if include_handle else ""
    return (
        f'<document-reference name="{_attr(ref.filename)}"{handle} '
        f'size_bytes="{ref.size_bytes}" status="{ref.stub_status()}"/>'
    )


def render_references_section(refs: Sequence[ReferenceAttachment], *, include_handle: bool) -> str:
    """Every reference's block, in attachment order, for the user message."""
    return "\n\n".join(render_reference(ref, include_handle=include_handle) for ref in refs)


def format_size(size_bytes: int) -> str:
    """``38 MB`` / ``4.2 MB``: one decimal, dropped when it is zero."""
    mb = round(size_bytes / (1024 * 1024), 1)
    text = f"{mb:.1f}".removesuffix(".0")
    return f"{text} MB"


#: Upper bounds of the ``sizeBucket`` property, in MB. Coarse on purpose: the
#: readout wants the shape of demand, and a fine bucket is close to a file size.
_SIZE_BUCKETS_MB = (8, 16, 32, 50)


def size_bucket(size_bytes: int) -> str:
    mb = size_bytes / (1024 * 1024)
    lower = 4
    for upper in _SIZE_BUCKETS_MB:
        if mb <= upper:
            return f"{lower}-{upper}MB"
        lower = upper
    return f">{_SIZE_BUCKETS_MB[-1]}MB"


def record_reference_attachments(refs: List[ReferenceAttachment]) -> None:
    """One content-free ``AttachmentReference`` EMF record per reference (§6).

    In ``AgentCoreStack/Compaction`` beside ``DocumentRead``. Properties are the
    size bucket, the format and the stub status: never a name, an id or text.
    Never raises.
    """
    if not refs:
        return
    try:
        from apis.shared.observability.emf import emit_emf_metrics
        from apis.shared.observability.prompt_cache import prompt_cache_observability_enabled

        if not prompt_cache_observability_enabled():
            return
        for ref in refs:
            emit_emf_metrics(
                "AgentCoreStack/Compaction",
                metrics={"AttachmentReference": 1},
                properties={
                    "sizeBucket": size_bucket(ref.size_bytes),
                    "format": ref.format,
                    "status": ref.stub_status(),
                },
                units={"AttachmentReference": "Count"},
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("AttachmentReference EMF skipped: %s", e)
