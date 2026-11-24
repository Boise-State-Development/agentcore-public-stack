"""Version history for My Skills (docs/specs/skill-version-history.md §6.1).

Three routes, owner-scoped through ``UserSkillService`` and 404 after
authentication while ``SKILL_VERSION_HISTORY_ENABLED`` is off:

* ``GET /skills/mine/{skill_id}/versions`` — a page of history, newest first.
* ``GET /skills/mine/{skill_id}/versions/{number}`` — one version and its diff
  from the live skill.
* ``POST /skills/mine/{skill_id}/versions/{number}/restore`` — put it back as a
  new version. Asks for the reach confirmation first, as any edit does.

A version's author is reported only as ``createdByYou``: a version a project
editor's pin cut must not name them to the skill's owner (spec D7).
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from apis.shared.auth import User, get_current_user_from_session
from apis.shared.security.log_sanitize import scrub_log
from apis.shared.skills.user_service import UserSkillError, get_user_skill_service

from .routes import MySkillResponse, _user_skill_error, require_reach_confirmed
from .version_history_core import (
    RestoreRequest,
    VersionDetailResponse,
    VersionListResponse,
    VersionSummaryResponse,
    detail_response,
    history_error,
    page_size,
    require_history_enabled,
    summarize,
    summary_response,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["skills"])


class MyRestoreResponse(BaseModel):
    skill: MySkillResponse
    # The restore's own version; None when keeping it failed (the restore still landed).
    version: Optional[VersionSummaryResponse] = None
    # False when the version predates full import sources, so the live one was kept.
    import_source_restored: bool = Field(True, alias="importSourceRestored")

    model_config = {"populate_by_name": True}


def _translate(e: Exception) -> HTTPException:
    mapped = history_error(e)
    if mapped is not None:
        return mapped
    if isinstance(e, UserSkillError):
        return _user_skill_error(e)
    if isinstance(e, ValueError):
        return HTTPException(status_code=400, detail=str(e))
    raise e


@router.get(
    "/mine/{skill_id}/versions",
    response_model=VersionListResponse,
    response_model_exclude_none=True,
)
async def list_my_skill_versions(
    skill_id: str,
    limit: Optional[int] = Query(None, ge=1, le=50),
    before: Optional[int] = Query(None, ge=1, description="Continue below this version"),
    user: User = Depends(get_current_user_from_session),
) -> VersionListResponse:
    """A page of an owned skill's history, newest first."""
    require_history_enabled()
    try:
        live, rows, cursor = await get_user_skill_service().list_versions(
            skill_id, user, limit=page_size(limit), before=before
        )
    except Exception as e:  # noqa: BLE001 - mapped, or re-raised
        raise _translate(e)
    return VersionListResponse(
        versions=[
            summary_response(row, live, created_by_you=row.created_by == user.user_id)
            for row in rows
        ],
        next_cursor=cursor,
    )


@router.get(
    "/mine/{skill_id}/versions/{number}",
    response_model=VersionDetailResponse,
    response_model_exclude_none=True,
)
async def get_my_skill_version(
    skill_id: str,
    number: int,
    user: User = Depends(get_current_user_from_session),
) -> VersionDetailResponse:
    """One version of an owned skill, with what restoring it would change."""
    require_history_enabled()
    try:
        live, version = await get_user_skill_service().get_version(skill_id, number, user)
    except Exception as e:  # noqa: BLE001 - mapped, or re-raised
        raise _translate(e)
    return detail_response(version, live, created_by_you=version.created_by == user.user_id)


@router.post(
    "/mine/{skill_id}/versions/{number}/restore",
    response_model=MyRestoreResponse,
    response_model_exclude_none=True,
)
async def restore_my_skill_version(
    skill_id: str,
    number: int,
    body: RestoreRequest,
    user: User = Depends(get_current_user_from_session),
) -> MyRestoreResponse:
    """Put version ``number`` back as a new version. Moves no pins (spec D4).

    Without ``confirmReach``, 409s with the reach summary when the skill runs
    unpinned in a shared Agent or project (``require_reach_confirmed``).
    """
    require_history_enabled()
    service = get_user_skill_service()
    try:
        await service.require_owned(skill_id, user)
    except UserSkillError as e:
        raise _user_skill_error(e)
    if not body.confirm_reach:
        await require_reach_confirmed(skill_id, user)
    logger.info(f"User {scrub_log(user.email)} restoring version {number} of {scrub_log(skill_id)}")
    try:
        result = await service.restore_version(
            skill_id, number, user, expected_updated_at=body.expected_updated_at
        )
    except Exception as e:  # noqa: BLE001 - mapped, or re-raised
        raise _translate(e)
    return MyRestoreResponse(
        skill=MySkillResponse.from_skill(result.skill),
        version=summary_response(summarize(result.version), result.skill, created_by_you=True)
        if result.version
        else None,
        import_source_restored=result.import_source_restored,
    )
