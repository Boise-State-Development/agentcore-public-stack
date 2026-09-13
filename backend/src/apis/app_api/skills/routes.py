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

**Access model.** These routes require only an authenticated session. There is
no per-user capability gate: ``SKILLS_ENABLED`` decides whether the feature
exists in this environment, and a role's ``grantedSkills`` decides *which*
catalog skills a user can reach. Both surfaces are already self-limiting —
``GET /skills/`` returns only what ``resolve_accessible_skill_ids`` grants
(so a user with no grants gets an empty list and the SPA renders no picker),
and every ``/skills/mine/*`` route is owner-scoped, so a user only ever sees
skills they authored.

The ``skills`` RBAC capability that used to gate these routes was removed: it
kept the surfaces admin-only during the v2 rollout, but it could not be granted
from the admin roles UI (that form builds ``grantedTools`` from the tool
catalog, and a capability id is not a tool), so an admin who granted a catalog
skill to a role would find it silently invisible to that role's users with no
way to fix it in-product.
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
from apis.shared.skills.resource_types import (
    resource_download_headers,
    safe_download_content_type,
)

from .service import get_skill_catalog_service
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
    """Return the raw bytes of one of an owned skill's supporting files.

    Hardened identically to the admin read route: the media type is re-derived
    from the filename and the body is served ``attachment`` + ``nosniff`` +
    inert CSP, so a resource can never become a script-bearing document on the
    SPA's origin (see ``apis.shared.skills.resource_types``).
    """
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
        media_type=safe_download_content_type(ref.filename),
        headers=resource_download_headers(ref.filename),
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


# -----------------------------------------------------------------------------
# One accessible skill (read-only detail)
#
# ⚠️ REGISTRATION ORDER IS LOAD-BEARING. These routes must stay BELOW every
# ``/mine`` route in this module. Starlette matches in registration order and
# ``SKILL_ID_PATTERN`` happily matches the literal string ``mine`` — declare
# ``/{skill_id}`` first and ``GET /skills/mine`` becomes a lookup for a skill
# called "mine", which 404s for every user in the product.
# -----------------------------------------------------------------------------


class SkillDetailResponse(BaseModel):
    """One skill the user can reach, with everything the picker line omits.

    The fat sibling of ``UserSkillResponse``. ``GET /skills/`` stays thin on
    purpose — it is a first-load payload, and putting every granted skill's
    SKILL.md body on it would buy nothing for the list and cost on every load.
    This is fetched once, for the one skill the user opened.

    ``instructions`` is served to anyone the skill is granted to. It is not a
    secret from them: it is the text their own turns load on dispatch, so the
    page is showing the user what they are already talking to.

    Deliberately absent: ``ownerId`` (``isOwned`` is the only part of it this
    surface needs, and a raw owner id would leak one user's identity to
    another) and ``allowedAppRoles`` (an admin-display projection — see the
    RBAC note in CLAUDE.md — which would expose the role topology to any user
    holding the skill).
    """

    skill_id: str = Field(..., alias="skillId")
    display_name: str = Field(..., alias="displayName")
    description: str
    instructions: str = ""
    compose: List[str] = Field(default_factory=list)
    allowed_tools: List[str] = Field(default_factory=list, alias="allowedTools")
    skill_metadata: Dict = Field(default_factory=dict, alias="skillMetadata")
    resources: List[SkillResourceRef] = Field(default_factory=list)
    status: str = SkillStatus.ACTIVE.value
    category: Optional[str] = None
    user_enabled: Optional[bool] = Field(None, alias="userEnabled")
    is_enabled: bool = Field(..., alias="isEnabled")
    #: True when the caller authored this skill — drives the SPA's "Edit in My
    #: Skills" affordance. Ownership is its own grant (``access.py``), so this
    #: can be true for a user with no skill-granting role at all.
    is_owned: bool = Field(..., alias="isOwned")
    created_at: Optional[str] = Field(None, alias="createdAt")
    updated_at: Optional[str] = Field(None, alias="updatedAt")

    model_config = {"populate_by_name": True}


async def _require_accessible_skill(skill_id: str, user: User) -> SkillDefinition:
    """Load a skill the user can reach, or raise 404.

    Access is ``resolve_accessible_skill_ids`` — the *same* resolution that
    builds the picker and that the runtime uses to decide what a turn may
    activate. A skill the user cannot reach and a skill that does not exist
    both 404, so this never discloses the existence of a skill someone else
    was granted.

    Status is checked too: ``GET /skills/`` filters to ACTIVE, so a drilled-in
    DRAFT or DISABLED catalog skill would otherwise be reachable by id from a
    surface that never listed it.
    """
    accessible = await resolve_accessible_skill_ids(user)
    if skill_id not in accessible:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")

    skill = await get_skill_catalog_service().get_skill(skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")

    status = skill.status.value if hasattr(skill.status, "value") else skill.status
    if status != SkillStatus.ACTIVE.value and skill.owner_id != user.user_id:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")

    return skill


@router.get("/{skill_id}", response_model=SkillDetailResponse)
async def get_accessible_skill(
    skill_id: str,
    user: User = Depends(get_current_user_from_session),
) -> SkillDetailResponse:
    """Read one skill the current user can reach, catalog or self-authored."""
    logger.info(f"User {user.name} reading skill '{skill_id}'")

    skill = await _require_accessible_skill(skill_id, user)

    repo = get_skill_catalog_repository()
    preferences = (await repo.get_user_preferences(user.user_id)).skill_preferences

    status = skill.status.value if hasattr(skill.status, "value") else skill.status
    return SkillDetailResponse(
        skill_id=skill.skill_id,
        display_name=skill.display_name,
        description=skill.description,
        instructions=skill.instructions,
        compose=list(skill.compose),
        allowed_tools=list(skill.allowed_tools),
        skill_metadata=dict(skill.skill_metadata),
        resources=list(skill.resources),
        status=str(status),
        category=skill.category,
        user_enabled=preferences.get(skill.skill_id),
        # Skills v2 D6 opt-in: untouched is OFF. Same default as the picker —
        # the two must agree or the page would contradict the list it came from.
        is_enabled=preferences.get(skill.skill_id, False),
        is_owned=skill.owner_id == user.user_id,
        created_at=skill.created_at.isoformat() if skill.created_at else None,
        updated_at=skill.updated_at.isoformat() if skill.updated_at else None,
    )


@router.get("/{skill_id}/resources/{filename}")
async def read_accessible_skill_resource(
    skill_id: str,
    filename: str,
    user: User = Depends(get_current_user_from_session),
):
    """Return the raw bytes of one supporting file on an accessible skill.

    The read counterpart of ``/mine/{id}/resources/{filename}``, scoped by
    *access* rather than ownership so a user granted a catalog skill can open
    its reference files — the level-3 half of the same progressive disclosure
    whose level-2 body this page already renders. Read-only by construction:
    there is no accessible-scoped upload or delete.

    Hardened identically to the owner and admin read routes: the media type is
    re-derived from the filename and the body is served ``attachment`` +
    ``nosniff`` + inert CSP, so a resource can never become a script-bearing
    document on the SPA's origin (``apis.shared.skills.resource_types``).
    """
    await _require_accessible_skill(skill_id, user)

    try:
        ref, content = await get_skill_catalog_service().read_resource(
            skill_id, filename
        )
    except ValueError as e:
        raise _resource_value_error(e)

    return Response(
        content=content,
        media_type=safe_download_content_type(ref.filename),
        headers=resource_download_headers(ref.filename),
    )
