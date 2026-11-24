"""The tier-neutral half of skill version history (docs/specs/skill-version-history.md §6.1).

My Skills (``version_routes``) and the admin catalog (``admin/skills/routes``) list,
read and restore versions the same way; they differ only in who may see which skill
and how a version's author is named. So each tier keeps its own routes and access
check, and shares the wire models and builders here, as bundle import does with
``bundle_import_core``.

A version's detail carries the diff **from the live skill to that version**: what
restoring it would change. That is the import preview's shape (``ImportTextDiff``,
``ImportFileRow``), so the SPA renders both with one component.
"""

from typing import Dict, List, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field

from apis.shared.feature_flags import skill_version_history_enabled
from apis.shared.skills.bundle_import import text_diff
from apis.shared.skills.history import (
    SkillCheckpointError,
    SkillRestoreConflictError,
    SkillVersionNotFoundError,
)
from apis.shared.skills.models import SkillDefinition, SkillResourceRef
from apis.shared.skills.resource_store import SkillResourceStoreError
from apis.shared.skills.versions import SkillVersion, SkillVersionSummary, content_hash

from .bundle_import_core import ImportFileRow, ImportTextDiff

# A history page; the SPA asks for a page at a time.
DEFAULT_PAGE = 20
MAX_PAGE = 50


class VersionSummaryResponse(BaseModel):
    """One row of a skill's history."""

    version: int
    created_at: Optional[str] = Field(None, alias="createdAt")
    trigger: Optional[str] = None
    restored_from: Optional[int] = Field(None, alias="restoredFrom")
    display_name: str = Field(..., alias="displayName")
    file_count: int = Field(0, alias="fileCount")
    # Its content is the live skill's (spec §6.2's Current badge).
    is_current: bool = Field(False, alias="isCurrent")
    # Provenance only (type, owner, repo, ref, commit): never the URL or path.
    import_source: Optional[Dict[str, str]] = Field(None, alias="importSource")
    # My Skills: whether the caller cut it. Never another user's id or email (D7).
    created_by_you: Optional[bool] = Field(None, alias="createdByYou")
    # Admin catalog: who cut it, when the user is known.
    created_by_email: Optional[str] = Field(None, alias="createdByEmail")

    model_config = {"populate_by_name": True}


class VersionListResponse(BaseModel):
    versions: List[VersionSummaryResponse]
    # Pass as ``before`` for the next page; None at the end.
    next_cursor: Optional[int] = Field(None, alias="nextCursor")

    model_config = {"populate_by_name": True}


class VersionFile(BaseModel):
    path: str
    filename: str
    kind: str
    size: int


class VersionDetailResponse(VersionSummaryResponse):
    description: str
    instructions: str
    files: List[VersionFile] = Field(default_factory=list)
    # From the live skill to this version: what restoring it would change.
    description_diff: ImportTextDiff = Field(..., alias="descriptionDiff")
    instructions_diff: ImportTextDiff = Field(..., alias="instructionsDiff")
    file_changes: List[ImportFileRow] = Field(default_factory=list, alias="fileChanges")


class RestoreRequest(BaseModel):
    # The live skill's ``updatedAt`` as the caller saw it; a mismatch is a 409.
    expected_updated_at: Optional[str] = Field(None, alias="expectedUpdatedAt")
    # My Skills only: sent after the user confirmed the reach a 409 reported.
    confirm_reach: bool = Field(False, alias="confirmReach")

    model_config = {"populate_by_name": True}


def require_history_enabled() -> None:
    """404 while the flag is off — called in each handler, after auth ran."""
    if not skill_version_history_enabled():
        raise HTTPException(status_code=404, detail="Not found")


def page_size(limit: Optional[int]) -> int:
    return max(1, min(limit or DEFAULT_PAGE, MAX_PAGE))


def summarize(version: SkillVersion) -> SkillVersionSummary:
    """The list row for a whole version (a detail read, a restore's result)."""
    return SkillVersionSummary(
        skill_id=version.skill_id,
        version=version.version or 0,
        content_hash=version.content_hash,
        created_at=version.created_at,
        created_by=version.created_by,
        display_name=version.display_name,
        file_count=len(version.resources),
        import_source=version.import_source,
        trigger=version.trigger,
        restored_from=version.restored_from,
    )


def summary_response(
    row: SkillVersionSummary,
    live: SkillDefinition,
    *,
    created_by_you: Optional[bool] = None,
    created_by_email: Optional[str] = None,
) -> VersionSummaryResponse:
    return VersionSummaryResponse(
        version=row.version,
        created_at=row.created_at,
        trigger=row.trigger,
        restored_from=row.restored_from,
        display_name=row.display_name,
        file_count=row.file_count,
        is_current=row.content_hash == content_hash(live),
        import_source=row.import_source,
        created_by_you=created_by_you,
        created_by_email=created_by_email,
    )


def detail_response(
    version: SkillVersion,
    live: SkillDefinition,
    *,
    created_by_you: Optional[bool] = None,
    created_by_email: Optional[str] = None,
) -> VersionDetailResponse:
    summary = summary_response(
        summarize(version), live, created_by_you=created_by_you, created_by_email=created_by_email
    )
    return VersionDetailResponse(
        **summary.model_dump(),
        description=version.description,
        instructions=version.instructions,
        files=[
            VersionFile(path=r.path or r.filename, filename=r.filename, kind=r.kind, size=r.size)
            for r in version.resources
        ],
        description_diff=ImportTextDiff.from_diff(
            text_diff(live.description, version.description, label="description")
        ),
        instructions_diff=ImportTextDiff.from_diff(
            text_diff((live.instructions or "").strip(), version.instructions.strip(), label="SKILL.md")
        ),
        file_changes=file_changes(live.resources, version.resources),
    )


def file_changes(
    live: List[SkillResourceRef], version: List[SkillResourceRef]
) -> List[ImportFileRow]:
    """Each file's fate if the version were restored, by path and content hash."""
    current = {r.path or r.filename: r for r in live}
    wanted = {r.path or r.filename: r for r in version}
    rows: List[ImportFileRow] = []
    for path, ref in wanted.items():
        before = current.get(path)
        status = "added" if before is None else (
            "unchanged" if before.content_hash == ref.content_hash else "changed"
        )
        rows.append(
            ImportFileRow(path=path, status=status, filename=ref.filename, kind=ref.kind, size=ref.size)
        )
    for path, ref in current.items():
        if path not in wanted:
            rows.append(
                ImportFileRow(path=path, status="removed", filename=ref.filename, kind=ref.kind, size=ref.size)
            )
    return rows


def history_error(e: Exception) -> Optional[HTTPException]:
    """The HTTP status for a version-history failure; None for anything else."""
    if isinstance(e, SkillVersionNotFoundError):
        return HTTPException(status_code=404, detail=str(e))
    if isinstance(e, SkillRestoreConflictError):
        return HTTPException(status_code=409, detail=str(e))
    if isinstance(e, (SkillCheckpointError, SkillResourceStoreError)):
        return HTTPException(status_code=503, detail=str(e) or "Try again in a moment.")
    return None
