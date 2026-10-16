"""The only code that queries the shared ``attachments`` knowledge base.

Every user's large attachments live in **one** Managed Knowledge Base per
environment (``docs/specs/large-document-attachments.md`` §4E), told apart only by
the ``user_id`` and ``session_id`` attributes each document carries. Isolation is
therefore ours to enforce, and it is enforced here, in one place, with the three
layers of ``conversation_search/index_search.py``:

1. :func:`search_attachments` takes the caller's user id and session id as
   required arguments and builds the ``andAll(user_id equals, session_id equals)``
   filter itself (plus ``upload_id equals`` when one document is named). It
   accepts no filter from its caller, so no caller can widen, replace or forget
   it. ``document_read`` passes the identity its closure captured, never one from
   the model.
2. Nothing else calls ``Retrieve`` with the attachments KB id.
   ``tests/architecture/test_attachment_kb_retrieve_boundary.py`` fails the build
   if another module does.
3. A hit is dropped unless **both** its document id
   (``att#{user_id}#{session_id}#{upload_id}``) and its attributes name the caller
   and the session, and its file row exists under ``USER#{caller}``, is READY and
   belongs to the same session. Any one of the three stops a leak through a lost
   filter.

Scoping is per **session**, which is stricter than ``document_read``'s other modes
(owner only): a search in one conversation never surfaces a document attached to
another.

A missing or empty user or session id raises before anything is sent: an empty
``equals`` value makes Bedrock raise ``ValidationException``, and an empty id is
never a caller anyone should be searching as.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from apis.shared.files.attachment_index import parse_document_id
from apis.shared.files.models import FileMetadata, FileStatus
from apis.shared.kb_backend.reserved import ATTACHMENTS_KB_ID

logger = logging.getLogger(__name__)

#: Chunks one search asks for (§4F).
DEFAULT_TOP_K = 5

#: Per-chunk text cap: the managed-KB context cap the RAG path already uses.
MAX_CHUNK_CHARS = 2000

#: Managed KBs report a PDF chunk's page here, as a float (§9 D3). The classic-KB
#: ``x-amz-bedrock-kb-document-page-number`` field does not apply.
PAGE_NUMBER_METADATA_KEY = "_excerpt_page_number"


@dataclass(frozen=True)
class AttachmentHit:
    """One retrieved chunk of the caller's own document, joined to its row."""

    upload_id: str
    filename: str
    text: str
    page: Optional[int]
    score: Optional[float]
    truncated: bool


def attachment_filter(user_id: str, session_id: str, upload_id: Optional[str] = None) -> Dict[str, Any]:
    """The retrieval filter for one caller in one conversation.

    Exact-match operators only, which ``validate_isolation_filter`` also insists
    on. Public so the isolation tests can assert the exact shape that is sent.
    """
    clauses = [
        {"equals": {"key": "user_id", "value": user_id}},
        {"equals": {"key": "session_id", "value": session_id}},
    ]
    if upload_id:
        clauses.append({"equals": {"key": "upload_id", "value": upload_id}})
    return {"andAll": clauses}


def _require(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"an attachment search requires the caller's {what}")
    return value


def _page(value: Any) -> Optional[int]:
    try:
        page = int(float(value))
    except (TypeError, ValueError):
        return None
    return page if page >= 1 else None


def _owned_in_session(meta: Optional[FileMetadata], session_id: str) -> bool:
    if meta is None or meta.session_id != session_id:
        return False
    status = meta.status if isinstance(meta.status, str) else meta.status.value
    return status == FileStatus.READY.value


async def search_attachments(
    user_id: str,
    session_id: str,
    query: str,
    *,
    upload_id: Optional[str] = None,
    top_k: int = DEFAULT_TOP_K,
    backend: Any = None,
    repository: Any = None,
) -> List[AttachmentHit]:
    """Search the caller's indexed documents in one conversation, best first.

    ``upload_id`` narrows the search to that document. ``backend`` (a
    ``ManagedKbBackend`` by default) and ``repository`` are injectable for tests;
    neither is a filter. Raises ``ManagedKbNotProvisioned`` when the knowledge
    base has not been created yet and lets any Bedrock error through; the caller
    decides how to degrade.
    """
    caller = _require(user_id, "user id")
    session = _require(session_id, "session id")
    if upload_id is not None and not upload_id.strip():
        upload_id = None
    if not query or not query.strip():
        return []

    if backend is None:
        from apis.shared.kb_backend.managed_backend import ManagedKbBackend

        backend = ManagedKbBackend()
    if repository is None:
        from apis.shared.files.repository import get_file_upload_repository

        repository = get_file_upload_repository()

    chunks = await backend.search(
        ATTACHMENTS_KB_ID,
        query.strip(),
        top_k,
        attachment_filter(caller, session, upload_id),
    )

    rows: Dict[str, Optional[FileMetadata]] = {}
    hits: List[AttachmentHit] = []
    foreign = 0
    for chunk in chunks:
        ref = parse_document_id(chunk.document_id or "")
        if ref is None:
            foreign += 1
            continue
        doc_user, doc_session, doc_upload = ref
        metadata = chunk.metadata or {}
        # Fail closed: a hit whose id or attributes do not name the caller and
        # this conversation is never theirs, and one with no owner attribute
        # cannot be shown to be.
        if (
            doc_user != caller
            or doc_session != session
            or metadata.get("user_id") != caller
            or metadata.get("session_id") != session
            or metadata.get("upload_id") != doc_upload
            or (upload_id is not None and doc_upload != upload_id)
        ):
            foreign += 1
            continue
        if doc_upload not in rows:
            rows[doc_upload] = await repository.get_file(caller, doc_upload)
        meta = rows[doc_upload]
        if not _owned_in_session(meta, session):
            # Deleted since it was indexed (the delete event has not landed yet),
            # or not this conversation's: never shown.
            continue
        text = chunk.text or ""
        hits.append(
            AttachmentHit(
                upload_id=doc_upload,
                filename=meta.filename,
                text=text[:MAX_CHUNK_CHARS],
                page=_page(metadata.get(PAGE_NUMBER_METADATA_KEY)),
                score=chunk.relevance,
                truncated=len(text) > MAX_CHUNK_CHARS,
            )
        )

    if foreign:
        # The filter should make this impossible. Count it, never show it.
        logger.warning("attachment search dropped %d hit(s) outside the caller's filter", foreign)
    return hits


__all__ = [
    "DEFAULT_TOP_K",
    "MAX_CHUNK_CHARS",
    "PAGE_NUMBER_METADATA_KEY",
    "AttachmentHit",
    "attachment_filter",
    "search_attachments",
]
