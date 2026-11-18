"""Import a whole skill bundle into My Skills (docs/specs/skill-bundle-import.md).

Three routes, all owner-scoped through ``UserSkillService`` and 404 after
authentication while ``SKILL_BUNDLE_IMPORT_ENABLED`` is off:

* ``POST /skills/mine/import/preview`` — parse and diff, write nothing.
* ``POST /skills/mine/import`` — create a skill from the bundle.
* ``POST /skills/mine/{skill_id}/import`` — re-import into an owned skill.

Each takes a ``file`` (a ``.zip``) **or** a ``url`` (a public GitHub path), and
an optional ``path`` naming the bundle inside the archive. Apply re-reads the
source and refuses with 409 if it no longer matches the preview (D3). Nothing
here cuts a skill version (D5).
"""

import logging
import posixpath
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from apis.shared.auth import User, get_current_user_from_session
from apis.shared.blocking_io import run_blocking
from apis.shared.feature_flags import skill_bundle_import_enabled
from apis.shared.skills.bundle_import import (
    MAX_UPLOAD_ARCHIVE_BYTES,
    BundleImportError,
    BundleLoad,
    ParsedBundle,
    diff_fields,
    diff_files,
    load_bundle,
)
from apis.shared.skills.models import SkillDefinition, SkillImportSource
from apis.shared.skills.user_service import UserSkillError, get_user_skill_service

from .github_source import GitHubSourceError, fetch_archive, parse_github_url
from .routes import MySkillResponse, _user_skill_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["skills"])


class ImportFileRow(BaseModel):
    path: str
    status: str
    filename: Optional[str] = None
    kind: Optional[str] = None
    size: int = 0
    reason: Optional[str] = None


class ImportedSkillSummary(BaseModel):
    name: str
    display_name: str = Field(..., alias="displayName")
    description: str
    instructions_length: int = Field(..., alias="instructionsLength")
    allowed_tools: List[str] = Field(default_factory=list, alias="allowedTools")

    model_config = {"populate_by_name": True}


class ImportFieldChanges(BaseModel):
    description: bool
    instructions: bool
    allowed_tools: bool = Field(..., alias="allowedTools")
    skill_metadata: bool = Field(..., alias="skillMetadata")

    model_config = {"populate_by_name": True}


class ImportPreviewResponse(BaseModel):
    source: SkillImportSource
    candidates: List[str] = Field(default_factory=list)
    skill: Optional[ImportedSkillSummary] = None
    changes: Optional[ImportFieldChanges] = None
    files: List[ImportFileRow] = Field(default_factory=list)
    bundle_hash: Optional[str] = Field(None, alias="bundleHash")
    target_updated_at: Optional[str] = Field(None, alias="targetUpdatedAt")

    model_config = {"populate_by_name": True}


class ImportApplyResponse(BaseModel):
    skill: MySkillResponse
    files: List[ImportFileRow]


def _require_enabled() -> None:
    """404 while the flag is off — called in each handler, after auth ran."""
    if not skill_bundle_import_enabled():
        raise HTTPException(status_code=404, detail="Not found")


async def _load(
    file: Optional[UploadFile], url: Optional[str], path: Optional[str]
) -> Tuple[BundleLoad, SkillImportSource]:
    """Read the source into a bundle (or candidates) and describe where it came from."""
    has_url = bool((url or "").strip())
    if (file is None) == (not has_url):
        raise HTTPException(status_code=400, detail="Send a .zip file or a GitHub URL.")

    try:
        if file is not None:
            data = await file.read(MAX_UPLOAD_ARCHIVE_BYTES + 1)
            if len(data) > MAX_UPLOAD_ARCHIVE_BYTES:
                raise HTTPException(
                    status_code=400,
                    detail=f"The .zip is larger than {MAX_UPLOAD_ARCHIVE_BYTES // (1024 * 1024)} MiB.",
                )
            bundle_path = path
            source = {"type": "zip", "filename": posixpath.basename(file.filename or "")[:200] or None}
        else:
            location = parse_github_url(url or "")
            data = await fetch_archive(location)
            # An explicit path (the SPA's candidate pick) wins over the URL's.
            bundle_path = path if path is not None else (location.path or None)
            source = {
                "type": "github",
                "url": location.display_url,
                "owner": location.owner,
                "repo": location.repo,
                "ref": location.ref,
            }
        load = await run_blocking(load_bundle, data, path=bundle_path)
    except GitHubSourceError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except BundleImportError as e:
        raise HTTPException(status_code=400, detail=str(e))

    bundle = load.bundle
    return load, SkillImportSource(
        **source,
        path=bundle.root if bundle else (bundle_path or ""),
        commit_sha=load.commit_sha if source["type"] == "github" else None,
        bundle_hash=bundle.bundle_hash if bundle else "",
        imported_at=datetime.now(timezone.utc).isoformat(),
    )


def _rows(bundle: ParsedBundle, target: Optional[SkillDefinition]) -> List[ImportFileRow]:
    return [
        ImportFileRow(
            path=c.path, status=c.status, filename=c.filename, kind=c.kind, size=c.size, reason=c.reason
        )
        for c in diff_files(bundle, target)
    ]


def _require_bundle(load: BundleLoad) -> ParsedBundle:
    if load.bundle is None:
        raise HTTPException(
            status_code=400,
            detail="This archive holds several skills. Preview it and choose one.",
        )
    return load.bundle


@router.post("/mine/import/preview", response_model=ImportPreviewResponse)
async def preview_skill_import(
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
    path: Optional[str] = Form(None),
    skill_id: Optional[str] = Form(None, alias="skillId"),
    user: User = Depends(get_current_user_from_session),
) -> ImportPreviewResponse:
    """What importing this bundle would do, against ``skillId`` when given."""
    _require_enabled()
    service = get_user_skill_service()
    target: Optional[SkillDefinition] = None
    if skill_id:
        try:
            target = await service.require_owned(skill_id, user)
        except UserSkillError as e:
            raise _user_skill_error(e)

    load, source = await _load(file, url, path)
    if load.bundle is None:
        return ImportPreviewResponse(source=source, candidates=load.candidates)

    bundle = load.bundle
    changes = diff_fields(bundle, target) if target else None
    return ImportPreviewResponse(
        source=source,
        skill=ImportedSkillSummary(
            name=bundle.name,
            display_name=target.display_name if target else bundle.display_name,
            description=bundle.description,
            instructions_length=len(bundle.instructions),
            allowed_tools=bundle.allowed_tools,
        ),
        changes=ImportFieldChanges(
            description=changes.description,
            instructions=changes.instructions,
            allowed_tools=changes.allowed_tools,
            skill_metadata=changes.skill_metadata,
        )
        if changes
        else None,
        files=_rows(bundle, target),
        bundle_hash=bundle.bundle_hash,
        target_updated_at=target.updated_at.isoformat() if target else None,
    )


@router.post("/mine/import", response_model=ImportApplyResponse)
async def create_skill_from_import(
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
    path: Optional[str] = Form(None),
    display_name: Optional[str] = Form(None, alias="displayName", max_length=100),
    expected_bundle_hash: Optional[str] = Form(None, alias="expectedBundleHash"),
    user: User = Depends(get_current_user_from_session),
) -> ImportApplyResponse:
    """Create a new skill from a bundle."""
    _require_enabled()
    logger.info(f"User {user.name} importing a skill bundle")
    load, source = await _load(file, url, path)
    bundle = _require_bundle(load)
    rows = _rows(bundle, None)
    try:
        skill = await get_user_skill_service().import_bundle(
            user,
            bundle,
            source=source,
            display_name=display_name,
            expected_bundle_hash=expected_bundle_hash,
        )
    except UserSkillError as e:
        raise _user_skill_error(e)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ImportApplyResponse(skill=MySkillResponse.from_skill(skill), files=rows)


@router.post("/mine/{skill_id}/import", response_model=ImportApplyResponse)
async def reimport_skill(
    skill_id: str,
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
    path: Optional[str] = Form(None),
    expected_bundle_hash: Optional[str] = Form(None, alias="expectedBundleHash"),
    expected_updated_at: Optional[str] = Form(None, alias="expectedUpdatedAt"),
    user: User = Depends(get_current_user_from_session),
) -> ImportApplyResponse:
    """Refresh an owned skill from its bundle. Never cuts a version (D5)."""
    _require_enabled()
    logger.info(f"User {user.name} re-importing a skill bundle")
    service = get_user_skill_service()
    try:
        target = await service.require_owned(skill_id, user)
    except UserSkillError as e:
        raise _user_skill_error(e)

    load, source = await _load(file, url, path)
    bundle = _require_bundle(load)
    rows = _rows(bundle, target)
    try:
        skill = await service.import_bundle(
            user,
            bundle,
            source=source,
            skill_id=skill_id,
            expected_bundle_hash=expected_bundle_hash,
            expected_updated_at=expected_updated_at,
        )
    except UserSkillError as e:
        raise _user_skill_error(e)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ImportApplyResponse(skill=MySkillResponse.from_skill(skill), files=rows)
