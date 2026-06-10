"""Admin API routes for skill catalog management.

Mirrors apis/app_api/admin/tools/routes.py. All routes require admin access.
There is no /discover endpoint — skills are authored, not discovered; the
create/edit form populates its tool picker from GET /admin/tools.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response

from apis.shared.auth import User, require_admin
from apis.shared.skills.models import (
    AddRemoveSkillRolesRequest,
    AdminSkillListResponse,
    AdminSkillResponse,
    SetSkillRolesRequest,
    SkillCreateRequest,
    SkillDefinition,
    SkillResourcesResponse,
    SkillRolesResponse,
    SkillUpdateRequest,
)
from apis.app_api.skills.service import get_skill_catalog_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["admin-skills"])


@router.get("/", response_model=AdminSkillListResponse)
async def admin_list_all_skills(
    status: Optional[str] = Query(
        None, description="Filter by status (active, draft, disabled)"
    ),
    admin: User = Depends(require_admin),
):
    """List all skills in the catalog with their role assignments."""
    logger.info("Admin listing full skill catalog")

    service = get_skill_catalog_service()
    skills = await service.get_all_skills(status=status, include_roles=True)

    return AdminSkillListResponse(
        skills=[AdminSkillResponse.from_skill_definition(s) for s in skills],
        total=len(skills),
    )


@router.get("/{skill_id}", response_model=AdminSkillResponse)
async def admin_get_skill(
    skill_id: str,
    admin: User = Depends(require_admin),
):
    """Get a specific skill by ID, with its directly-granting roles."""
    logger.info("Admin getting skill")

    service = get_skill_catalog_service()
    skill = await service.get_skill(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")

    roles = await service.get_roles_for_skill(skill_id)
    allowed_roles = [r.role_id for r in roles if r.grant_type == "direct"]

    return AdminSkillResponse.from_skill_definition(skill, allowed_roles)


@router.post("/", response_model=AdminSkillResponse)
async def admin_create_skill(
    request: SkillCreateRequest,
    admin: User = Depends(require_admin),
):
    """Create a new skill catalog entry.

    Every bound_tool_id is validated against the tool catalog (must exist and
    be ACTIVE). This only creates the catalog entry; use the role endpoints to
    grant it to AppRoles.
    """
    logger.info("Admin creating skill")

    service = get_skill_catalog_service()

    try:
        skill = SkillDefinition(
            skill_id=request.skill_id,
            display_name=request.display_name,
            description=request.description,
            instructions=request.instructions,
            bound_tool_ids=request.bound_tool_ids,
            compose=request.compose,
            status=request.status,
            category=request.category,
        )
        created = await service.create_skill(skill, admin)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Drop the all-skill-ids snapshot so the new skill is recognized by
    # SkillAccessService on the very next chat turn in this process.
    from apis.shared.skills.freshness import invalidate as invalidate_freshness

    invalidate_freshness(created.skill_id)

    return AdminSkillResponse.from_skill_definition(created)


@router.put("/{skill_id}", response_model=AdminSkillResponse)
async def admin_update_skill(
    skill_id: str,
    request: SkillUpdateRequest,
    admin: User = Depends(require_admin),
):
    """Update skill metadata. Re-validates bound tools when they change."""
    logger.info("Admin updating skill")

    service = get_skill_catalog_service()
    updates = request.model_dump(exclude_unset=True, by_alias=False)

    try:
        updated = await service.update_skill(skill_id, updates, admin)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not updated:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")

    from apis.shared.skills.freshness import invalidate as invalidate_freshness

    invalidate_freshness(skill_id)

    return AdminSkillResponse.from_skill_definition(updated)


@router.delete("/{skill_id}")
async def admin_delete_skill(
    skill_id: str,
    hard: bool = Query(
        False, description="If true, permanently delete instead of soft delete"
    ),
    admin: User = Depends(require_admin),
):
    """Delete a skill. Soft (disable) by default; hard=true permanently deletes."""
    logger.info("Admin deleting skill")

    service = get_skill_catalog_service()
    deleted = await service.delete_skill(skill_id, admin, soft=not hard)

    if not deleted:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")

    from apis.shared.skills.freshness import invalidate as invalidate_freshness

    invalidate_freshness(skill_id)

    action = "deleted" if hard else "disabled"
    return {"message": f"Skill '{skill_id}' {action} successfully"}


# =============================================================================
# Role Assignment Endpoints
# =============================================================================


@router.get("/{skill_id}/roles", response_model=SkillRolesResponse)
async def get_skill_roles(
    skill_id: str,
    admin: User = Depends(require_admin),
):
    """Get AppRoles that grant access to this skill (direct/inherited)."""
    logger.info("Admin getting roles for skill")

    service = get_skill_catalog_service()
    skill = await service.get_skill(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")

    roles = await service.get_roles_for_skill(skill_id)
    return SkillRolesResponse(skill_id=skill_id, roles=roles)


@router.put("/{skill_id}/roles")
async def set_skill_roles(
    skill_id: str,
    request: SetSkillRolesRequest,
    admin: User = Depends(require_admin),
):
    """Replace which AppRoles grant access to this skill (bidirectional sync)."""
    logger.info("Admin setting roles for skill")

    service = get_skill_catalog_service()
    try:
        await service.set_roles_for_skill(skill_id, request.app_role_ids, admin)
        return {"message": f"Roles updated for skill '{skill_id}'"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{skill_id}/roles/add")
async def add_roles_to_skill(
    skill_id: str,
    request: AddRemoveSkillRolesRequest,
    admin: User = Depends(require_admin),
):
    """Add AppRoles to skill access (preserves existing)."""
    logger.info("Admin adding roles to skill")

    service = get_skill_catalog_service()
    try:
        await service.add_roles_to_skill(skill_id, request.app_role_ids, admin)
        return {"message": f"Roles added to skill '{skill_id}'"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{skill_id}/roles/remove")
async def remove_roles_from_skill(
    skill_id: str,
    request: AddRemoveSkillRolesRequest,
    admin: User = Depends(require_admin),
):
    """Remove AppRoles from skill access."""
    logger.info("Admin removing roles from skill")

    service = get_skill_catalog_service()
    try:
        await service.remove_roles_from_skill(skill_id, request.app_role_ids, admin)
        return {"message": f"Roles removed from skill '{skill_id}'"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# =============================================================================
# Reference-File Endpoints (S3-backed supporting reference files — PR-4)
# =============================================================================


def _resource_value_error(e: ValueError) -> HTTPException:
    """Map a service ValueError to 404 (missing) or 400 (validation)."""
    status = 404 if "not found" in str(e).lower() else 400
    return HTTPException(status_code=status, detail=str(e))


@router.get("/{skill_id}/resources", response_model=SkillResourcesResponse)
async def list_skill_resources(
    skill_id: str,
    admin: User = Depends(require_admin),
):
    """List a skill's reference-file manifest (no bytes)."""
    logger.info("Admin listing skill reference files")

    service = get_skill_catalog_service()
    try:
        resources = await service.list_resources(skill_id)
    except ValueError as e:
        raise _resource_value_error(e)

    return SkillResourcesResponse(skill_id=skill_id, resources=resources)


@router.post("/{skill_id}/resources", response_model=SkillResourcesResponse)
async def upload_skill_resource(
    skill_id: str,
    file: UploadFile = File(...),
    admin: User = Depends(require_admin),
):
    """Upload (or replace) one supporting reference file for a skill.

    Bytes are stored content-addressed in S3; the catalog row's manifest is
    updated atomically. Re-uploading the same filename replaces it. Returns
    the skill's updated manifest.
    """
    logger.info("Admin uploading skill reference file")

    service = get_skill_catalog_service()
    content = await file.read()
    try:
        resources = await service.add_resource(
            skill_id,
            filename=file.filename or "",
            content=content,
            content_type=file.content_type or "",
            admin=admin,
        )
    except ValueError as e:
        raise _resource_value_error(e)

    from apis.shared.skills.freshness import invalidate as invalidate_freshness

    invalidate_freshness(skill_id)

    return SkillResourcesResponse(skill_id=skill_id, resources=resources)


@router.get("/{skill_id}/resources/{filename}")
async def read_skill_resource(
    skill_id: str,
    filename: str,
    admin: User = Depends(require_admin),
):
    """Return the raw bytes of one reference file with its content type."""
    logger.info("Admin reading skill reference file")

    service = get_skill_catalog_service()
    try:
        ref, content = await service.read_resource(skill_id, filename)
    except ValueError as e:
        raise _resource_value_error(e)

    return Response(
        content=content,
        media_type=ref.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{ref.filename}"',
        },
    )


@router.delete("/{skill_id}/resources/{filename}", response_model=SkillResourcesResponse)
async def delete_skill_resource(
    skill_id: str,
    filename: str,
    admin: User = Depends(require_admin),
):
    """Delete one reference file from a skill. Returns the updated manifest."""
    logger.info("Admin deleting skill reference file")

    service = get_skill_catalog_service()
    try:
        resources = await service.delete_resource(skill_id, filename, admin)
    except ValueError as e:
        raise _resource_value_error(e)

    from apis.shared.skills.freshness import invalidate as invalidate_freshness

    invalidate_freshness(skill_id)

    return SkillResourcesResponse(skill_id=skill_id, resources=resources)
