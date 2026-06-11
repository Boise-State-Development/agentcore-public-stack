"""User-facing skills API: list accessible skills + per-skill preferences.

The skills parallel of ``apis/app_api/tools/routes.py``. Returns only the
ACTIVE skills the user's RBAC roles grant — the same resolution
(``apis.shared.skills.access``) and the same ACTIVE filter the SkillAgent
applies at runtime, so what the user sees in the picker is exactly what the
agent can activate. Preferences are a global per-user map (skill_id ->
enabled); a skill absent from the map is enabled by default.

Admin skill management routes are in ``apis.app_api.admin.skills.routes``.
"""

import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from apis.shared.auth import User, get_current_user_from_session
from apis.shared.skills.access import resolve_accessible_skill_ids
from apis.shared.skills.models import SkillStatus
from apis.shared.skills.repository import get_skill_catalog_repository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["skills"])


class UserSkillResponse(BaseModel):
    """A single skill as shown in the user's skills picker."""

    skill_id: str = Field(..., alias="skillId")
    display_name: str = Field(..., alias="displayName")
    description: str
    category: Optional[str] = None
    bound_tool_count: int = Field(..., alias="boundToolCount")
    user_enabled: Optional[bool] = Field(None, alias="userEnabled")
    is_enabled: bool = Field(..., alias="isEnabled")

    model_config = {"populate_by_name": True}


class UserSkillsResponse(BaseModel):
    """Response model for GET /skills/."""

    skills: List[UserSkillResponse]
    total_count: int = Field(..., alias="totalCount")

    model_config = {"populate_by_name": True}


class SkillPreferencesRequest(BaseModel):
    """Request body for PUT /skills/preferences."""

    preferences: Dict[str, bool] = Field(
        ..., description="Map of skill_id -> enabled state"
    )


@router.get("/", response_model=UserSkillsResponse)
async def get_user_skills(
    user: User = Depends(get_current_user_from_session),
) -> UserSkillsResponse:
    """
    Get the ACTIVE skills the current user's roles grant, with the user's
    enabled/disabled preferences merged.
    """
    logger.info(f"User {user.name} getting skills with preferences")

    accessible_ids = await resolve_accessible_skill_ids(user)
    if not accessible_ids:
        return UserSkillsResponse(skills=[], total_count=0)

    repo = get_skill_catalog_repository()
    records = await repo.batch_get_skills(accessible_ids)
    preferences = (await repo.get_user_preferences(user.user_id)).skill_preferences

    skills = [
        UserSkillResponse(
            skill_id=record.skill_id,
            display_name=record.display_name,
            description=record.description,
            category=record.category,
            bound_tool_count=len(record.bound_tool_ids),
            user_enabled=preferences.get(record.skill_id),
            is_enabled=preferences.get(record.skill_id, True),
        )
        for record in records
        if record.status == SkillStatus.ACTIVE
    ]
    skills.sort(key=lambda s: s.display_name.lower())

    return UserSkillsResponse(skills=skills, total_count=len(skills))


@router.put("/preferences")
async def update_skill_preferences(
    request: SkillPreferencesRequest,
    user: User = Depends(get_current_user_from_session),
):
    """
    Save the user's per-skill enabled/disabled preferences.

    Only accepts preferences for skills the user has access to.
    """
    logger.info(f"User {user.name} updating skill preferences")

    accessible = set(await resolve_accessible_skill_ids(user))
    unknown = sorted(sid for sid in request.preferences if sid not in accessible)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Preferences include skills you don't have access to: {unknown}",
        )

    repo = get_skill_catalog_repository()
    await repo.save_user_preferences(user.user_id, request.preferences)
    return {"message": "Preferences saved successfully"}
