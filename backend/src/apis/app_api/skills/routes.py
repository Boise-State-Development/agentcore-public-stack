"""User-facing skills API: accessible skills, preferences, and My Skills CRUD.

Two surfaces live here:

1. **Picker** (``GET /skills/``, ``PUT /skills/preferences``). Returns the
   ACTIVE skills the user can reach — RBAC-granted catalog skills **union**
   the skills they authored themselves — via the same resolution the runtime
   uses (``apis.shared.skills.access``), so what the user sees in the picker
   is exactly what the agent can activate. Preferences are a global per-user
   map (skill_id -> enabled).

2. **My Skills** (``/skills/mine/*``, Skills v2 PR-3). Owner-scoped CRUD over
   the user-authored tier: create/edit/delete your own skills and upload the
   supporting files of their agentskills.io bundle. Every route resolves
   ownership through ``UserSkillService``; a skill you do not own is
   indistinguishable from one that does not exist.

Admin catalog management routes are in ``apis.app_api.admin.skills.routes``.
"""

import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

from apis.shared.auth import User, get_current_user_from_session
from apis.shared.skills.access import resolve_accessible_skill_ids
from apis.shared.skills.models import (
    SkillDefinition,
    SkillResourceRef,
    SkillResourcesResponse,
    SkillStatus,
)
from apis.shared.skills.repository import get_skill_catalog_repository

from .user_service import (
    UserSkillError,
    UserSkillLimitError,
    UserSkillNotFoundError,
    get_user_skill_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["skills"])


class UserSkillResponse(BaseModel):
    """A single skill as shown in the user's skills picker."""

    skill_id: str = Field(..., alias="skillId")
    display_name: str = Field(..., alias="displayName")
    description: str
    category: Optional[str] = None
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
            user_enabled=preferences.get(record.skill_id),
            # Skills v2 D6: opt-in. An untouched skill is OFF, unlike tools.
            # This is the picker's half of the same default the runtime enforces
            # in `_apply_enabled_skills_filter` (absent selection ⇒ no skills);
            # the two must agree or the UI would show skills as active that the
            # turn never loads.
            is_enabled=preferences.get(record.skill_id, False),
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


# =============================================================================
# My Skills — the user-authored tier (Skills v2 PR-3)
# =============================================================================


class MySkillResponse(BaseModel):
    """One skill the caller authored, as shown on the My Skills page."""

    skill_id: str = Field(..., alias="skillId")
    display_name: str = Field(..., alias="displayName")
    description: str
    instructions: str = ""
    allowed_tools: List[str] = Field(default_factory=list, alias="allowedTools")
    skill_metadata: Dict = Field(default_factory=dict, alias="skillMetadata")
    resources: List[SkillResourceRef] = Field(default_factory=list)
    status: str = SkillStatus.ACTIVE.value
    category: Optional[str] = None
    created_at: Optional[str] = Field(None, alias="createdAt")
    updated_at: Optional[str] = Field(None, alias="updatedAt")

    model_config = {"populate_by_name": True}

    @classmethod
    def from_skill(cls, skill: SkillDefinition) -> "MySkillResponse":
        return cls(
            skill_id=skill.skill_id,
            display_name=skill.display_name,
            description=skill.description,
            instructions=skill.instructions,
            allowed_tools=list(skill.allowed_tools),
            skill_metadata=dict(skill.skill_metadata),
            resources=list(skill.resources),
            status=str(skill.status.value if hasattr(skill.status, "value") else skill.status),
            category=skill.category,
            created_at=skill.created_at.isoformat() if skill.created_at else None,
            updated_at=skill.updated_at.isoformat() if skill.updated_at else None,
        )


class MySkillListResponse(BaseModel):
    """Response model for GET /skills/mine."""

    skills: List[MySkillResponse]
    total_count: int = Field(..., alias="totalCount")

    model_config = {"populate_by_name": True}


class CreateMySkillRequest(BaseModel):
    """Request body for POST /skills/mine.

    No ``skillId``: ids are allocated server-side from the display name so a
    user never has to invent one — or collide with a catalog skill they cannot
    see. ``allowedTools`` is advisory metadata only and never grants a tool
    (spec D1/D4).
    """

    display_name: str = Field(..., alias="displayName", max_length=200)
    description: str = Field(..., max_length=2000)
    instructions: str = ""
    allowed_tools: List[str] = Field(default_factory=list, alias="allowedTools")
    skill_metadata: Dict = Field(default_factory=dict, alias="skillMetadata")
    category: Optional[str] = None

    model_config = {"populate_by_name": True}


class UpdateMySkillRequest(BaseModel):
    """Request body for PUT /skills/mine/{skill_id}.

    Only authored fields are writable. ``ownerId``, ``visibility`` and the
    audit fields are deliberately absent — a user cannot re-home their skill
    into the admin catalog or onto another account.
    """

    display_name: Optional[str] = Field(None, alias="displayName", max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    instructions: Optional[str] = None
    allowed_tools: Optional[List[str]] = Field(None, alias="allowedTools")
    skill_metadata: Optional[Dict] = Field(None, alias="skillMetadata")
    category: Optional[str] = None
    status: Optional[SkillStatus] = None

    model_config = {"populate_by_name": True}


def _user_skill_error(e: UserSkillError) -> HTTPException:
    """Map a user-tier service error to its HTTP status."""
    if isinstance(e, UserSkillNotFoundError):
        return HTTPException(status_code=404, detail=str(e))
    if isinstance(e, UserSkillLimitError):
        return HTTPException(status_code=409, detail=str(e))
    return HTTPException(status_code=400, detail=str(e))


def _resource_value_error(e: ValueError) -> HTTPException:
    """Map a resource-file validation error to its HTTP status."""
    status = 404 if "not found" in str(e).lower() else 400
    return HTTPException(status_code=status, detail=str(e))


@router.get("/mine", response_model=MySkillListResponse)
async def list_my_skills(
    user: User = Depends(get_current_user_from_session),
) -> MySkillListResponse:
    """List every skill the current user authored (any status)."""
    logger.info(f"User {user.name} listing authored skills")

    skills = await get_user_skill_service().list_my_skills(user)
    return MySkillListResponse(
        skills=[MySkillResponse.from_skill(s) for s in skills],
        total_count=len(skills),
    )


@router.post("/mine", response_model=MySkillResponse)
async def create_my_skill(
    request: CreateMySkillRequest,
    user: User = Depends(get_current_user_from_session),
) -> MySkillResponse:
    """Create a skill owned by the current user."""
    logger.info(f"User {user.name} creating an authored skill")

    try:
        skill = await get_user_skill_service().create_my_skill(
            user,
            display_name=request.display_name,
            description=request.description,
            instructions=request.instructions,
            allowed_tools=request.allowed_tools,
            skill_metadata=request.skill_metadata,
            category=request.category,
        )
    except UserSkillError as e:
        raise _user_skill_error(e)

    return MySkillResponse.from_skill(skill)


@router.get("/mine/{skill_id}", response_model=MySkillResponse)
async def get_my_skill(
    skill_id: str,
    user: User = Depends(get_current_user_from_session),
) -> MySkillResponse:
    """Get one of the current user's authored skills."""
    try:
        skill = await get_user_skill_service().get_my_skill(skill_id, user)
    except UserSkillError as e:
        raise _user_skill_error(e)

    return MySkillResponse.from_skill(skill)


@router.put("/mine/{skill_id}", response_model=MySkillResponse)
async def update_my_skill(
    skill_id: str,
    request: UpdateMySkillRequest,
    user: User = Depends(get_current_user_from_session),
) -> MySkillResponse:
    """Update one of the current user's authored skills."""
    logger.info(f"User {user.name} updating an authored skill")

    updates = request.model_dump(exclude_unset=True, exclude_none=True)
    try:
        skill = await get_user_skill_service().update_my_skill(skill_id, updates, user)
    except UserSkillError as e:
        raise _user_skill_error(e)

    return MySkillResponse.from_skill(skill)


@router.delete("/mine/{skill_id}")
async def delete_my_skill(
    skill_id: str,
    user: User = Depends(get_current_user_from_session),
):
    """Delete one of the current user's authored skills and its bundle files."""
    logger.info(f"User {user.name} deleting an authored skill")

    try:
        await get_user_skill_service().delete_my_skill(skill_id, user)
    except UserSkillError as e:
        raise _user_skill_error(e)

    return {"message": f"Skill '{skill_id}' deleted"}


# -----------------------------------------------------------------------------
# My Skills — bundle files
# -----------------------------------------------------------------------------


@router.get("/mine/{skill_id}/resources", response_model=SkillResourcesResponse)
async def list_my_skill_resources(
    skill_id: str,
    user: User = Depends(get_current_user_from_session),
):
    """List an owned skill's supporting-file manifest (no bytes)."""
    try:
        resources = await get_user_skill_service().list_resources(skill_id, user)
    except UserSkillError as e:
        raise _user_skill_error(e)

    return SkillResourcesResponse(skill_id=skill_id, resources=resources)


@router.post("/mine/{skill_id}/resources", response_model=SkillResourcesResponse)
async def upload_my_skill_resource(
    skill_id: str,
    file: UploadFile = File(...),
    kind: str = Form("reference"),
    user: User = Depends(get_current_user_from_session),
):
    """Upload (or replace) one supporting file on an owned skill.

    Bytes land in the standard agentskills.io bundle layout (``references/`` |
    ``scripts/`` | ``assets/`` per ``kind``). ``script`` files are stored inert
    — listed and readable, never executed (spec D5).
    """
    logger.info(f"User {user.name} uploading a skill bundle file")

    content = await file.read()
    try:
        resources = await get_user_skill_service().add_resource(
            skill_id,
            filename=file.filename or "",
            content=content,
            content_type=file.content_type or "",
            user=user,
            kind=kind,
        )
    except UserSkillError as e:
        raise _user_skill_error(e)
    except ValueError as e:
        raise _resource_value_error(e)

    return SkillResourcesResponse(skill_id=skill_id, resources=resources)


@router.get("/mine/{skill_id}/resources/{filename}")
async def read_my_skill_resource(
    skill_id: str,
    filename: str,
    user: User = Depends(get_current_user_from_session),
):
    """Return the raw bytes of one of an owned skill's supporting files."""
    try:
        ref, content = await get_user_skill_service().read_resource(
            skill_id, filename, user
        )
    except UserSkillError as e:
        raise _user_skill_error(e)
    except ValueError as e:
        raise _resource_value_error(e)

    return Response(
        content=content,
        media_type=ref.content_type or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{ref.filename}"'},
    )


@router.delete(
    "/mine/{skill_id}/resources/{filename}", response_model=SkillResourcesResponse
)
async def delete_my_skill_resource(
    skill_id: str,
    filename: str,
    user: User = Depends(get_current_user_from_session),
):
    """Delete one supporting file from an owned skill. Returns the manifest."""
    logger.info(f"User {user.name} deleting a skill bundle file")

    try:
        resources = await get_user_skill_service().delete_resource(
            skill_id, filename, user
        )
    except UserSkillError as e:
        raise _user_skill_error(e)
    except ValueError as e:
        raise _resource_value_error(e)

    return SkillResourcesResponse(skill_id=skill_id, resources=resources)
