"""Admin API routes for skill catalog management.

Mirrors apis/app_api/admin/tools/routes.py. All routes require admin access.
There is no /discover endpoint — skills are authored, not discovered; the
create/edit form populates its tool picker from GET /admin/tools.

SECURITY — catalog scope: ``admin.skills`` governs the *admin catalog*
(``owner_id == "system"``). User-authored skills live in the same table but are
governed by ownership, and their instruction bodies are instruction-trusted
content that steers their owner's agent. Every per-object route here must
therefore apply the catalog predicate (``get_catalog_skill`` /
``require_catalog_skill``) and report a non-catalog row as 404 — matching
``GET /admin/skills/``, which is scoped the same way. Enforced by
tests/apis/app_api/skills/test_admin_skill_routes.py.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from apis.shared.auth import User, require_admin_scope
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

# Every route in this package is guarded by this one scope, so the
# permission boundary is the package boundary. Enforced by
# tests/architecture/test_admin_scope_coverage.py.
require_skills_admin = require_admin_scope("admin.skills")


def _skill_value_error(e: ValueError) -> HTTPException:
    """Map a service ValueError to 404 (missing / not a catalog skill) or 400.

    The catalog predicate raises a uniform "not found" for both "no such skill"
    and "user-authored skill", so this mapping never discloses the existence of
    a private skill to an admin who cannot govern it.
    """
    status = 404 if "not found" in str(e).lower() else 400
    return HTTPException(status_code=status, detail=str(e))


@router.get("/", response_model=AdminSkillListResponse)
async def admin_list_all_skills(
    status: Optional[str] = Query(
        None, description="Filter by status (active, draft, disabled)"
    ),
    admin: User = Depends(require_skills_admin),
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
    admin: User = Depends(require_skills_admin),
):
    """Get a specific catalog skill by ID, with its directly-granting roles.

    Catalog-scoped: a user-authored skill reports 404, exactly as it is absent
    from the list route.
    """
    logger.info("Admin getting skill")

    service = get_skill_catalog_service()
    skill = await service.get_catalog_skill(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")

    roles = await service.get_roles_for_skill(skill_id)
    allowed_roles = [r.role_id for r in roles if r.grant_type == "direct"]

    return AdminSkillResponse.from_skill_definition(skill, allowed_roles)


@router.post("/", response_model=AdminSkillResponse)
async def admin_create_skill(
    request: SkillCreateRequest,
    admin: User = Depends(require_skills_admin),
):
    """Create a new skill catalog entry.

    This only creates the catalog entry; use the role endpoints to grant it to
    AppRoles.
    """
    logger.info("Admin creating skill")

    service = get_skill_catalog_service()

    try:
        skill = SkillDefinition(
            skill_id=request.skill_id,
            display_name=request.display_name,
            description=request.description,
            instructions=request.instructions,
            compose=request.compose,
            status=request.status,
            category=request.category,
        )
        created = await service.create_skill(skill, admin)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Drop the all-skill-ids snapshot so the new skill is recognized by
    # ``skills.access.resolve_accessible_skill_ids`` on the very next chat
    # turn in this process.
    from apis.shared.skills.freshness import invalidate as invalidate_freshness

    invalidate_freshness(created.skill_id)

    return AdminSkillResponse.from_skill_definition(created)


@router.put("/{skill_id}", response_model=AdminSkillResponse)
async def admin_update_skill(
    skill_id: str,
    request: SkillUpdateRequest,
    admin: User = Depends(require_skills_admin),
):
    """Update catalog skill metadata. Re-validates bound tools when they change.

    Catalog-scoped: a user-authored skill reports 404 and is never rewritten
    here — its instructions are instruction-trusted content owned by its author.
    """
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
    admin: User = Depends(require_skills_admin),
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
    admin: User = Depends(require_skills_admin),
):
    """Get AppRoles that grant access to this skill (direct/inherited)."""
    logger.info("Admin getting roles for skill")

    service = get_skill_catalog_service()
    skill = await service.get_catalog_skill(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")

    roles = await service.get_roles_for_skill(skill_id)
    return SkillRolesResponse(skill_id=skill_id, roles=roles)


@router.put("/{skill_id}/roles")
async def set_skill_roles(
    skill_id: str,
    request: SetSkillRolesRequest,
    admin: User = Depends(require_skills_admin),
):
    """Replace which AppRoles grant access to this skill (bidirectional sync)."""
    logger.info("Admin setting roles for skill")

    service = get_skill_catalog_service()
    try:
        await service.set_roles_for_skill(skill_id, request.app_role_ids, admin)
        return {"message": f"Roles updated for skill '{skill_id}'"}
    except ValueError as e:
        raise _skill_value_error(e)


@router.post("/{skill_id}/roles/add")
async def add_roles_to_skill(
    skill_id: str,
    request: AddRemoveSkillRolesRequest,
    admin: User = Depends(require_skills_admin),
):
    """Add AppRoles to skill access (preserves existing)."""
    logger.info("Admin adding roles to skill")

    service = get_skill_catalog_service()
    try:
        await service.add_roles_to_skill(skill_id, request.app_role_ids, admin)
        return {"message": f"Roles added to skill '{skill_id}'"}
    except ValueError as e:
        raise _skill_value_error(e)


@router.post("/{skill_id}/roles/remove")
async def remove_roles_from_skill(
    skill_id: str,
    request: AddRemoveSkillRolesRequest,
    admin: User = Depends(require_skills_admin),
):
    """Remove AppRoles from skill access."""
    logger.info("Admin removing roles from skill")

    service = get_skill_catalog_service()
    try:
        await service.remove_roles_from_skill(skill_id, request.app_role_ids, admin)
        return {"message": f"Roles removed from skill '{skill_id}'"}
    except ValueError as e:
        raise _skill_value_error(e)


# =============================================================================
# Reference-File Endpoints (S3-backed supporting reference files — PR-4)
# =============================================================================


@router.get("/{skill_id}/resources", response_model=SkillResourcesResponse)
async def list_skill_resources(
    skill_id: str,
    admin: User = Depends(require_skills_admin),
):
    """List a catalog skill's reference-file manifest (no bytes)."""
    logger.info("Admin listing skill reference files")

    service = get_skill_catalog_service()
    try:
        resources = await service.list_resources(skill_id)
    except ValueError as e:
        raise _skill_value_error(e)

    return SkillResourcesResponse(skill_id=skill_id, resources=resources)


@router.post("/{skill_id}/resources", response_model=SkillResourcesResponse)
async def upload_skill_resource(
    skill_id: str,
    file: UploadFile = File(...),
    kind: str = Form("reference"),
    admin: User = Depends(require_skills_admin),
):
    """Upload (or replace) one supporting file for a catalog skill.

    Bytes are stored in the standard agentskills.io bundle layout
    (``references/`` | ``scripts/`` | ``assets/`` per ``kind``); the catalog
    row's manifest is updated atomically. Re-uploading the same filename
    replaces it. ``script`` files are stored inert (never executed). Returns the
    skill's updated manifest.
    """
    logger.info("Admin uploading skill reference file")

    service = get_skill_catalog_service()
    content = await file.read()
    try:
        await service.require_catalog_skill(skill_id)
        resources = await service.add_resource(
            skill_id,
            filename=file.filename or "",
            content=content,
            content_type=file.content_type or "",
            admin=admin,
            kind=kind,
        )
    except ValueError as e:
        raise _skill_value_error(e)

    from apis.shared.skills.freshness import invalidate as invalidate_freshness

    invalidate_freshness(skill_id)

    return SkillResourcesResponse(skill_id=skill_id, resources=resources)


@router.get("/{skill_id}/resources/{filename}")
async def read_skill_resource(
    skill_id: str,
    filename: str,
    admin: User = Depends(require_skills_admin),
):
    """Return the raw bytes of one catalog-skill reference file."""
    logger.info("Admin reading skill reference file")

    service = get_skill_catalog_service()
    try:
        await service.require_catalog_skill(skill_id)
        ref, content = await service.read_resource(skill_id, filename)
    except ValueError as e:
        raise _skill_value_error(e)

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
    admin: User = Depends(require_skills_admin),
):
    """Delete one reference file from a catalog skill. Returns the manifest."""
    logger.info("Admin deleting skill reference file")

    service = get_skill_catalog_service()
    try:
        await service.require_catalog_skill(skill_id)
        resources = await service.delete_resource(skill_id, filename, admin)
    except ValueError as e:
        raise _skill_value_error(e)

    from apis.shared.skills.freshness import invalidate as invalidate_freshness

    invalidate_freshness(skill_id)

    return SkillResourcesResponse(skill_id=skill_id, resources=resources)
