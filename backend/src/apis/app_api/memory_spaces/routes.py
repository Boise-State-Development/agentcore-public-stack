"""Memory Spaces user surface — CRUD over `/memory/spaces/*` (Workstream A2).

The user-facing surface for the Memory Space primitive: a person creates,
lists, reads, edits, and deletes their own (and shared-with-them) spaces,
entries, and index. This is the "own your data" surface; the *agent*
consumption of a space (tools, binding, prompt injection) is a separate
workstream (Agent/Harness layer), not here.

Gating: the router is mounted unconditionally but every route depends on
``require_memory_spaces_user`` — a 404 when ``MEMORY_SPACES_ENABLED`` is off
(the surface behaves as if unmounted). Auth is the standard SPA cookie
dependency (``get_current_user_from_session``) per the CLAUDE.md app-api rule.
Memory spaces are user-owned personal data (like sessions/assistants), so
there is no cohort RBAC capability gate; access to a *specific* space is the
identity-based ``resolve_permission`` check inside ``MemorySpaceService``.
"""

from __future__ import annotations

import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from tempfile import SpooledTemporaryFile
from typing import Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.feature_flags import memory_spaces_enabled
from apis.shared.memory.models import EntryType
from apis.shared.memory.service import (
    MemorySpaceConcurrencyError,
    MemorySpaceError,
    MemorySpaceExport,
    MemorySpaceNotFoundError,
    MemorySpacePermissionError,
    MemorySpaceService,
)
from apis.shared.memory.store import MemorySpaceStoreError

from apis.app_api.memory_spaces.models import (
    ConsolidateRequest,
    ConsolidationReportResponse,
    CreateSpaceRequest,
    EntriesListResponse,
    EntryContentResponse,
    EntryRefResponse,
    IndexContentResponse,
    MemberResponse,
    MembersListResponse,
    ShareRequest,
    SpaceDetailResponse,
    SpaceSummaryResponse,
    SpacesListResponse,
    UpdateIndexRequest,
    UpdateShareRequest,
    UpsertEntryRequest,
    all_templates,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memory/spaces", tags=["memory-spaces"])

_service: Optional[MemorySpaceService] = None


def _svc() -> MemorySpaceService:
    global _service
    if _service is None:
        _service = MemorySpaceService()
    return _service


async def require_memory_spaces_user(
    user: User = Depends(get_current_user_from_session),
) -> User:
    """Cookie auth + the environment kill switch.

    404 when ``MEMORY_SPACES_ENABLED`` is off, so the surface behaves as if
    unmounted (mirrors the schedules/runs pattern).
    """
    if not memory_spaces_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return user


def _translate(e: Exception) -> HTTPException:
    """Map a service error to an HTTP status."""
    if isinstance(e, MemorySpaceNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    if isinstance(e, MemorySpacePermissionError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    if isinstance(e, MemorySpaceConcurrencyError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    if isinstance(e, MemorySpaceError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    raise e


# ---- export (§9) -------------------------------------------------------

# Spill the zip to disk beyond this size so a large space never pins app-api
# memory (the entry count is bounded by the consolidation cap, so this is a
# ceiling, not the common case).
_ZIP_SPOOL_MAX_BYTES = 8 * 1024 * 1024
_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_component(value: str, fallback: str) -> str:
    """Reduce a user string to one safe archive path segment.

    Collapses separators / ``..`` / other unsafe characters so a hostile slug
    or space name cannot escape its folder in the zip (zip-slip). Empty results
    fall back to ``fallback``.
    """
    cleaned = _UNSAFE_PATH_CHARS.sub("-", (value or "").strip()).strip("-._")
    return cleaned or fallback


def _export_metadata_json(export: MemorySpaceExport) -> str:
    """Serialize the space-level state the markdown files don't carry (§9)."""
    space = export.space
    meta = {
        "spaceId": space.space_id,
        "name": space.name,
        "template": space.template,
        "createdAt": space.created_at,
        "updatedAt": space.updated_at,
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "owner": {"userId": space.owner_id, "email": space.owner_email},
        "members": [
            {
                "email": m.email,
                "permission": m.permission,
                "createdAt": m.created_at,
            }
            for m in export.members
        ],
        "entryCount": len(export.files),
    }
    return json.dumps(meta, indent=2, ensure_ascii=False)


def _build_export_zip(root: str, export: MemorySpaceExport) -> SpooledTemporaryFile:
    """Write the space's corpus into a spooled zip mirroring the S3 layout."""
    spool: SpooledTemporaryFile = SpooledTemporaryFile(
        max_size=_ZIP_SPOOL_MAX_BYTES, mode="w+b"
    )
    with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{root}/MEMORY.md", export.index_text)
        for ref, body in export.files:
            slug = _safe_component(ref.slug, "entry")
            entry_type = _safe_component(ref.entry_type, "fact")
            zf.writestr(f"{root}/entries/{entry_type}/{slug}.md", body)
        zf.writestr(f"{root}/metadata.json", _export_metadata_json(export))
    spool.seek(0)
    return spool


def _stream_and_close(spool: SpooledTemporaryFile) -> Iterator[bytes]:
    """Yield the spooled zip in chunks, closing (and unlinking) it when done."""
    try:
        while True:
            chunk = spool.read(65536)
            if not chunk:
                break
            yield chunk
    finally:
        spool.close()


# ---- spaces ------------------------------------------------------------


@router.get("", response_model=SpacesListResponse)
def list_spaces(user: User = Depends(require_memory_spaces_user)) -> SpacesListResponse:
    svc = _svc()
    summaries = [
        SpaceSummaryResponse.from_space(space, role)
        for space, role in svc.list_spaces_for_user(user.user_id, user.email)
    ]
    return SpacesListResponse(spaces=summaries, templates=all_templates())


@router.post("", response_model=SpaceSummaryResponse, status_code=status.HTTP_201_CREATED)
def create_space(
    request: CreateSpaceRequest,
    user: User = Depends(require_memory_spaces_user),
) -> SpaceSummaryResponse:
    try:
        space = _svc().create_space(
            owner_id=user.user_id,
            owner_email=user.email,
            name=request.name,
            template=request.template,
        )
    except MemorySpaceError as e:
        raise _translate(e)
    return SpaceSummaryResponse.from_space(space, "owner")


@router.get("/{space_id}", response_model=SpaceDetailResponse)
def get_space(
    space_id: str, user: User = Depends(require_memory_spaces_user)
) -> SpaceDetailResponse:
    svc = _svc()
    try:
        space, role = svc.resolve_permission(space_id, user.user_id, user.email)
        if space is None:
            raise MemorySpaceNotFoundError(f"Memory space '{space_id}' not found")
        if role is None:
            raise MemorySpacePermissionError(
                f"'viewer' access required on memory space '{space_id}'"
            )
        index_text = svc.read_index(space_id, user.user_id, user.email)
        entries = svc.list_entries(space_id, user.user_id, user.email)
    except MemorySpaceError as e:
        raise _translate(e)
    return SpaceDetailResponse(
        space_id=space.space_id,
        name=space.name,
        template=space.template,
        role=role,
        owner_id=space.owner_id,
        created_at=space.created_at,
        updated_at=space.updated_at,
        index=index_text,
        entries=[EntryRefResponse.from_ref(r) for r in entries],
    )


@router.get("/{space_id}/export")
def export_space(
    space_id: str, user: User = Depends(require_memory_spaces_user)
) -> StreamingResponse:
    """Download the whole space as a `.zip` of its raw markdown (viewer+, §9).

    The loss-free "own your data" export: the ``MEMORY.md`` index, every entry
    with frontmatter intact under ``entries/<type>/``, and a small
    ``metadata.json``. Any member who can read the space may export it; the
    owner exports the full space. Streamed from a spooled buffer so a large
    space never pins app-api memory.
    """
    svc = _svc()
    try:
        export = svc.export_space(space_id, user.user_id, user.email)
    except MemorySpaceError as e:
        raise _translate(e)
    except MemorySpaceStoreError as e:
        logger.error("memory-spaces: export failed for space=%s: %s", space_id, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed to read memory space contents for export",
        )

    root = _safe_component(export.space.name, export.space.space_id)
    spool = _build_export_zip(root, export)
    return StreamingResponse(
        _stream_and_close(spool),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{root}.zip"'},
    )


@router.post("/{space_id}/consolidate", response_model=ConsolidationReportResponse)
def consolidate_space(
    space_id: str,
    request: ConsolidateRequest | None = None,
    user: User = Depends(require_memory_spaces_user),
) -> ConsolidationReportResponse:
    """Run a deterministic consolidation (health) pass on a space (editor+, A6).

    Auto-fixes storage hygiene (orphaned-object GC) and reports issues that
    need judgment (duplicate content, dead ``[[slug]]`` links, over-cap). Never
    merges or evicts entries. ``stripDeadLinks`` opts into unlinking dead
    wikilinks from MEMORY.md.
    """
    req = request or ConsolidateRequest()
    try:
        report = _svc().consolidate(
            space_id,
            user.user_id,
            user.email,
            apply_gc=req.apply_gc,
            strip_dead_links=req.strip_dead_links,
        )
    except MemorySpaceError as e:
        raise _translate(e)
    except MemorySpaceStoreError as e:
        logger.error("memory-spaces: consolidate failed for space=%s: %s", space_id, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed to consolidate memory space storage",
        )
    return ConsolidationReportResponse.from_report(report)


@router.delete("/{space_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_or_leave_space(
    space_id: str, user: User = Depends(require_memory_spaces_user)
) -> None:
    """Owner deletes the whole space; a member drops their own grant (leave)."""
    svc = _svc()
    try:
        _, role = svc.resolve_permission(space_id, user.user_id, user.email)
        if role == "owner":
            svc.delete_space(space_id, user.user_id, user.email)
        else:
            svc.leave_space(space_id, user.user_id, user.email)
    except MemorySpaceError as e:
        raise _translate(e)


# ---- sharing (A4) ------------------------------------------------------


@router.get("/{space_id}/shares", response_model=MembersListResponse)
def list_shares(
    space_id: str, user: User = Depends(require_memory_spaces_user)
) -> MembersListResponse:
    """List a space's shared grants (editor+; the owner is implicit)."""
    try:
        members = _svc().list_members(space_id, user.user_id, user.email)
    except MemorySpaceError as e:
        raise _translate(e)
    return MembersListResponse(
        members=[MemberResponse.from_member(m) for m in members]
    )


@router.post(
    "/{space_id}/shares",
    response_model=MemberResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_share(
    space_id: str,
    request: ShareRequest,
    user: User = Depends(require_memory_spaces_user),
) -> MemberResponse:
    """Grant a user viewer/editor access to the space (owner only)."""
    try:
        member = _svc().share(
            space_id, user.user_id, user.email, request.email, request.permission
        )
    except MemorySpaceError as e:
        raise _translate(e)
    return MemberResponse.from_member(member)


@router.patch("/{space_id}/shares/{email}", response_model=MemberResponse)
def update_share(
    space_id: str,
    email: str,
    request: UpdateShareRequest,
    user: User = Depends(require_memory_spaces_user),
) -> MemberResponse:
    """Change an existing grant's role (owner only)."""
    try:
        member = _svc().update_share(
            space_id, user.user_id, user.email, email, request.permission
        )
    except MemorySpaceError as e:
        raise _translate(e)
    return MemberResponse.from_member(member)


@router.delete(
    "/{space_id}/shares/{email}", status_code=status.HTTP_204_NO_CONTENT
)
def remove_share(
    space_id: str, email: str, user: User = Depends(require_memory_spaces_user)
) -> None:
    """Revoke a user's grant (owner only). Idempotent."""
    try:
        _svc().revoke(space_id, user.user_id, user.email, email)
    except MemorySpaceError as e:
        raise _translate(e)


# ---- index (MEMORY.md) -------------------------------------------------


@router.get("/{space_id}/index", response_model=IndexContentResponse)
def read_index(
    space_id: str, user: User = Depends(require_memory_spaces_user)
) -> IndexContentResponse:
    try:
        text = _svc().read_index(space_id, user.user_id, user.email)
    except MemorySpaceError as e:
        raise _translate(e)
    return IndexContentResponse(content=text)


@router.put("/{space_id}/index", response_model=IndexContentResponse)
def update_index(
    space_id: str,
    request: UpdateIndexRequest,
    user: User = Depends(require_memory_spaces_user),
) -> IndexContentResponse:
    try:
        _svc().update_index(space_id, user.user_id, user.email, request.content)
    except MemorySpaceError as e:
        raise _translate(e)
    return IndexContentResponse(content=request.content)


# ---- entries -----------------------------------------------------------


@router.get("/{space_id}/entries", response_model=EntriesListResponse)
def list_entries(
    space_id: str,
    user: User = Depends(require_memory_spaces_user),
    entry_type: Optional[EntryType] = Query(None, alias="type"),
) -> EntriesListResponse:
    try:
        entries = _svc().list_entries(
            space_id, user.user_id, user.email, entry_type=entry_type
        )
    except MemorySpaceError as e:
        raise _translate(e)
    return EntriesListResponse(entries=[EntryRefResponse.from_ref(r) for r in entries])


@router.get("/{space_id}/entries/{slug}", response_model=EntryContentResponse)
def read_entry(
    space_id: str, slug: str, user: User = Depends(require_memory_spaces_user)
) -> EntryContentResponse:
    try:
        content = _svc().read_entry(space_id, user.user_id, user.email, slug)
    except MemorySpaceError as e:
        raise _translate(e)
    return EntryContentResponse(slug=slug, content=content)


@router.put("/{space_id}/entries/{slug}", response_model=EntryRefResponse)
def upsert_entry(
    space_id: str,
    slug: str,
    request: UpsertEntryRequest,
    user: User = Depends(require_memory_spaces_user),
) -> EntryRefResponse:
    try:
        ref = _svc().write_entry(
            space_id,
            user.user_id,
            user.email,
            slug,
            request.body,
            entry_type=request.entry_type,
            description=request.description,
            indexed=request.indexed,
        )
    except MemorySpaceError as e:
        raise _translate(e)
    return EntryRefResponse.from_ref(ref)


@router.delete("/{space_id}/entries/{slug}", status_code=status.HTTP_204_NO_CONTENT)
def delete_entry(
    space_id: str, slug: str, user: User = Depends(require_memory_spaces_user)
) -> None:
    try:
        _svc().delete_entry(space_id, user.user_id, user.email, slug)
    except MemorySpaceError as e:
        raise _translate(e)
