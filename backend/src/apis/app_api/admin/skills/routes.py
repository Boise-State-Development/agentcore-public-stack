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
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from apis.shared.audit.models import AuditAction
from apis.shared.auth import User, require_admin_scope
from apis.shared.blocking_io import run_blocking
from apis.shared.skills.audit import record_skill_event
from apis.shared.skills.bundle_export import (
    SkillExportFileError,
    SkillExportVersionNotFoundError,
    build_skill_zip,
    export_response_headers,
    load_export_record,
)
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
from apis.shared.skills.resource_types import (
    resource_download_headers,
    safe_download_content_type,
)
from apis.app_api.skills.bundle_import_core import (
    ImportFileRow,
    ImportPreviewResponse,
    build_preview,
    file_rows,
    load_source,
    require_bundle,
    require_import_enabled,
)
from apis.app_api.skills.service import (
    SkillImportConflictError,
    SkillNameConflictError,
    get_skill_catalog_service,
)
from apis.shared.skills.bundle import slugify_skill_name
from apis.app_api.skills.skill_md_parse import parse_uploaded_skill_md
from apis.shared.skills.skill_md_import import SkillMdParseResponse

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


# Registered before every ``/{skill_id}`` route so ``import`` is never read as an id.
@router.post("/import/parse", response_model=SkillMdParseResponse)
async def admin_parse_skill_md(
    file: UploadFile = File(...),
    admin: User = Depends(require_skills_admin),
) -> SkillMdParseResponse:
    """Read one SKILL.md into the create form's fields and diagnostics. Writes nothing."""
    return await parse_uploaded_skill_md(file)


# =============================================================================
# Bundle import (docs/specs/skill-bundle-import.md) — the catalog twin of
# /skills/mine/import*, sharing its loader and preview (``bundle_import_core``).
# 404 after auth while SKILL_BUNDLE_IMPORT_ENABLED is off.
# =============================================================================


class AdminImportApplyResponse(BaseModel):
    skill: AdminSkillResponse
    files: List[ImportFileRow]


def _import_error(e: ValueError) -> HTTPException:
    if isinstance(e, (SkillImportConflictError, SkillNameConflictError)):
        return HTTPException(status_code=409, detail=str(e))
    return _skill_value_error(e)


@router.post("/import/preview", response_model=ImportPreviewResponse)
async def admin_preview_skill_import(
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
    path: Optional[str] = Form(None),
    skill_id: Optional[str] = Form(None, alias="skillId"),
    admin: User = Depends(require_skills_admin),
) -> ImportPreviewResponse:
    """What importing this bundle would do, against catalog skill ``skillId`` when given."""
    require_import_enabled()
    service = get_skill_catalog_service()
    target: Optional[SkillDefinition] = None
    if skill_id:
        try:
            target = await service.require_catalog_skill(skill_id)
        except ValueError as e:
            raise _skill_value_error(e)

    load, source = await load_source(file, url, path)
    if load.bundle is None:
        return ImportPreviewResponse(source=source, candidates=load.candidates)

    bundle = load.bundle
    planned_id = target.skill_id if target else await service.plan_import_skill_id(bundle)
    return build_preview(bundle, source, target, stored_name=slugify_skill_name(planned_id))


@router.post("/import", response_model=AdminImportApplyResponse)
async def admin_create_skill_from_import(
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
    path: Optional[str] = Form(None),
    display_name: Optional[str] = Form(None, alias="displayName", max_length=100),
    expected_bundle_hash: Optional[str] = Form(None, alias="expectedBundleHash"),
    admin: User = Depends(require_skills_admin),
) -> AdminImportApplyResponse:
    """Create a draft catalog skill from a bundle; grant it to roles afterwards."""
    require_import_enabled()
    logger.info("Admin importing a skill bundle")
    load, source = await load_source(file, url, path)
    bundle = require_bundle(load)
    rows = file_rows(bundle, None)
    try:
        skill = await get_skill_catalog_service().import_bundle(
            admin,
            bundle,
            source=source,
            display_name=display_name,
            expected_bundle_hash=expected_bundle_hash,
        )
    except ValueError as e:
        raise _import_error(e)

    from apis.shared.skills.freshness import invalidate as invalidate_freshness

    invalidate_freshness(skill.skill_id)
    return AdminImportApplyResponse(
        skill=AdminSkillResponse.from_skill_definition(skill), files=rows
    )


@router.post("/{skill_id}/import", response_model=AdminImportApplyResponse)
async def admin_reimport_skill(
    skill_id: str,
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
    path: Optional[str] = Form(None),
    expected_bundle_hash: Optional[str] = Form(None, alias="expectedBundleHash"),
    expected_updated_at: Optional[str] = Form(None, alias="expectedUpdatedAt"),
    admin: User = Depends(require_skills_admin),
) -> AdminImportApplyResponse:
    """Refresh a catalog skill from its bundle. Catalog-scoped; never cuts a version."""
    require_import_enabled()
    logger.info("Admin re-importing a skill bundle")
    service = get_skill_catalog_service()
    try:
        target = await service.require_catalog_skill(skill_id)
    except ValueError as e:
        raise _skill_value_error(e)

    load, source = await load_source(file, url, path)
    bundle = require_bundle(load)
    rows = file_rows(bundle, target)
    try:
        skill = await service.import_bundle(
            admin,
            bundle,
            source=source,
            skill_id=skill_id,
            expected_bundle_hash=expected_bundle_hash,
            expected_updated_at=expected_updated_at,
        )
    except ValueError as e:
        raise _import_error(e)

    from apis.shared.skills.freshness import invalidate as invalidate_freshness

    invalidate_freshness(skill_id)
    roles = await service.get_roles_for_skill(skill_id)
    allowed_roles = [r.role_id for r in roles if r.grant_type == "direct"]
    return AdminImportApplyResponse(
        skill=AdminSkillResponse.from_skill_definition(skill, allowed_roles), files=rows
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
            allowed_tools=request.allowed_tools,
            skill_metadata=request.skill_metadata,
            status=request.status,
            category=request.category,
        )
        created = await service.create_skill(skill, admin)
    except SkillNameConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
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
    path: Optional[str] = Form(None),
    admin: User = Depends(require_skills_admin),
):
    """Upload (or replace) one supporting file for a catalog skill.

    Bytes are stored in the standard agentskills.io bundle layout
    (``references/`` | ``scripts/`` | ``assets/`` per ``kind``); the catalog
    row's manifest is updated atomically. Re-uploading the same filename
    replaces it. ``script`` files are stored inert (never executed). Returns the
    skill's updated manifest.

    ``path`` (optional form field) places the file below its kind directory,
    e.g. ``legal/nda.md``: at most 4 folders, each segment by the filename rule.
    Without it the file sits at the top of the directory under its filename.
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
            path=path,
        )
    except ValueError as e:
        raise _skill_value_error(e)

    from apis.shared.skills.freshness import invalidate as invalidate_freshness

    invalidate_freshness(skill_id)

    return SkillResourcesResponse(skill_id=skill_id, resources=resources)


@router.get("/{skill_id}/resources/{filename:path}")
async def read_skill_resource(
    skill_id: str,
    filename: str,
    admin: User = Depends(require_skills_admin),
):
    """Return the raw bytes of one catalog-skill reference file.

    SECURITY — the served media type is re-derived from the filename
    (``safe_download_content_type``) instead of reflecting the stored
    ``content_type``, and the response is ``attachment`` + ``nosniff`` + an
    inert CSP. app-api shares an origin with the SPA, so an ``inline`` response
    carrying a stored ``text/html`` would execute as a document in the reader's
    authenticated session. Re-deriving at serve time also neutralizes rows
    written before the upload allowlist existed, with no data migration.
    """
    logger.info("Admin reading skill reference file")

    service = get_skill_catalog_service()
    try:
        await service.require_catalog_skill(skill_id)
        ref, content = await service.read_resource(skill_id, filename)
    except ValueError as e:
        raise _skill_value_error(e)

    return Response(
        content=content,
        media_type=safe_download_content_type(ref.filename),
        headers=resource_download_headers(ref.filename),
    )


@router.get("/{skill_id}/export")
async def export_skill(
    skill_id: str,
    version: Optional[int] = Query(None, ge=1, description="Export this frozen version"),
    admin: User = Depends(require_skills_admin),
):
    """Download a catalog skill as an agentskills.io bundle (``{slug}.zip``).

    Catalog-scoped: a user-authored skill is 404, exactly as it is absent from
    ``GET /admin/skills/``. ``?version=n`` exports that frozen version. A file
    the manifest names that cannot be read is 409, naming it; there is never a
    partial zip. The archive is built in a worker thread, off the event loop.
    """
    service = get_skill_catalog_service()
    skill = await service.get_catalog_skill(skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' not found")
    try:
        record = await load_export_record(skill, version, service.repository)
    except SkillExportVersionNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        data = await run_blocking(build_skill_zip, record, service.resource_store.get)
    except SkillExportFileError as e:
        logger.warning("Admin skill export refused: a manifest file could not be read")
        raise HTTPException(status_code=409, detail=str(e))
    logger.info("Admin exported catalog skill")
    await record_skill_event(
        AuditAction.SKILL_EXPORTED,
        admin,
        skill_id,
        after=record,
        extra_after={"exportedVersion": version} if version else None,
    )
    return Response(
        content=data,
        media_type="application/zip",
        headers=export_response_headers(skill_id),
    )


@router.delete("/{skill_id}/resources/{filename:path}", response_model=SkillResourcesResponse)
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
