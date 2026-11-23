"""Import a whole skill bundle into My Skills (docs/specs/skill-bundle-import.md).

Three routes, all owner-scoped through ``UserSkillService`` and 404 after
authentication while ``SKILL_BUNDLE_IMPORT_ENABLED`` is off:

* ``POST /skills/mine/import/preview`` — parse and diff, write nothing.
* ``POST /skills/mine/import`` — create a skill from the bundle.
* ``POST /skills/mine/{skill_id}/import`` — re-import into an owned skill.

Each takes a ``file`` (a ``.zip``) **or** a ``url`` (a public GitHub path), and
an optional ``path`` naming the bundle inside the archive. Apply re-reads the
source and refuses with 409 if it no longer matches the preview (D3). Nothing
here cuts a skill version (D5). The loader and preview live in
``bundle_import_core``, shared with the admin catalog's routes (spec §6a).
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from apis.shared.auth import User, get_current_user_from_session
from apis.shared.skills.bundle import slugify_skill_name
from apis.shared.skills.bundle_import import ParsedBundle
from apis.shared.skills.models import SkillDefinition
from apis.shared.skills.user_service import (
    UserSkillError,
    UserSkillService,
    get_user_skill_service,
)

from .bundle_import_core import (
    ImportFileRow,
    ImportPreviewResponse,
    build_preview,
    file_rows,
    load_source,
    require_bundle,
    require_import_enabled,
)
from .routes import MySkillResponse, _user_skill_error, require_reach_confirmed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["skills"])


class ImportApplyResponse(BaseModel):
    skill: MySkillResponse
    files: List[ImportFileRow]


async def _stored_name(
    service: UserSkillService, bundle: ParsedBundle, target: Optional[SkillDefinition]
) -> str:
    """The activation name the skill will have once imported."""
    skill_id = target.skill_id if target else await service.plan_import_skill_id(bundle)
    return slugify_skill_name(skill_id)


@router.post("/mine/import/preview", response_model=ImportPreviewResponse)
async def preview_skill_import(
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
    path: Optional[str] = Form(None),
    skill_id: Optional[str] = Form(None, alias="skillId"),
    user: User = Depends(get_current_user_from_session),
) -> ImportPreviewResponse:
    """What importing this bundle would do, against ``skillId`` when given."""
    require_import_enabled()
    service = get_user_skill_service()
    target: Optional[SkillDefinition] = None
    if skill_id:
        try:
            target = await service.require_owned(skill_id, user)
        except UserSkillError as e:
            raise _user_skill_error(e)

    load, source = await load_source(file, url, path)
    if load.bundle is None:
        return ImportPreviewResponse(source=source, candidates=load.candidates)

    bundle = load.bundle
    stored_name = await _stored_name(service, bundle, target)
    return build_preview(bundle, source, target, stored_name=stored_name)


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
    require_import_enabled()
    logger.info(f"User {user.name} importing a skill bundle")
    load, source = await load_source(file, url, path)
    bundle = require_bundle(load)
    rows = file_rows(bundle, None)
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
    confirm_reach: bool = Form(False, alias="confirmReach"),
    user: User = Depends(get_current_user_from_session),
) -> ImportApplyResponse:
    """Refresh an owned skill from its bundle. Never cuts a version (D5).

    Without ``confirmReach``, 409s with the reach summary when the skill runs
    unpinned in a shared Agent or project (``require_reach_confirmed``).
    """
    require_import_enabled()
    logger.info(f"User {user.name} re-importing a skill bundle")
    service = get_user_skill_service()
    try:
        target = await service.require_owned(skill_id, user)
    except UserSkillError as e:
        raise _user_skill_error(e)
    if not confirm_reach:
        await require_reach_confirmed(skill_id, user)

    load, source = await load_source(file, url, path)
    bundle = require_bundle(load)
    rows = file_rows(bundle, target)
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
