"""
Skill Catalog Service

Service for skill catalog operations with AppRole integration. Mirrors
``ToolCatalogService``: CRUD over skill metadata, bidirectional role sync
(updating ``granted_skills`` on AppRoles), and ``allowedAppRoles`` hydration.

Skills are pure knowledge bundles (Skills v2): they carry no bound tools, so
there is no tool-catalog validation here.
"""

import logging
import re
from typing import Dict, List, Optional, Tuple

from apis.shared.auth.models import User
from apis.shared.rbac.admin_service import (
    AppRoleAdminService,
    get_app_role_admin_service,
)
from apis.shared.rbac.service import AppRoleService, get_app_role_service
from apis.shared.skills.models import (
    SkillDefinition,
    SkillResourceRef,
    SkillRoleAssignment,
)
from apis.shared.skills.repository import (
    SkillCatalogRepository,
    get_skill_catalog_repository,
)
from apis.shared.skills.bundle import generate_skill_md
from apis.shared.skills.resource_store import (
    SkillResourceStore,
    SkillResourceStoreError,
    compute_content_hash,
    get_skill_resource_store,
)

VALID_RESOURCE_KINDS = ("reference", "script", "asset")

logger = logging.getLogger(__name__)

# Reference-file guardrails. Reference files are small read-only docs
# (markdown/text), not bulk assets — keep both axes modest so a skill's
# manifest stays well inside the DynamoDB item limit and the agent's
# progressive-disclosure budget (PR-6) stays bounded.
MAX_RESOURCE_BYTES = 1_048_576  # 1 MiB per file
MAX_RESOURCES_PER_SKILL = 50
# Safe, flat filenames only — no path separators, no traversal. Mirrors the
# skill_id discipline: visible, predictable object keys.
_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class SkillCatalogService:
    """
    Service for skill catalog operations.

    Skill access is determined by AppRoles (granted_skills). This service
    provides catalog CRUD and bidirectional sync between skills and AppRoles.
    """

    def __init__(
        self,
        repository: Optional[SkillCatalogRepository] = None,
        app_role_service: Optional[AppRoleService] = None,
        app_role_admin_service: Optional[AppRoleAdminService] = None,
        resource_store: Optional[SkillResourceStore] = None,
    ):
        """Initialize with dependencies."""
        self.repository = repository or get_skill_catalog_repository()
        self.app_role_service = app_role_service or get_app_role_service()
        self.app_role_admin_service = (
            app_role_admin_service or get_app_role_admin_service()
        )
        self.resource_store = resource_store or get_skill_resource_store()

    # =========================================================================
    # Admin Methods - Skill CRUD
    # =========================================================================

    async def get_all_skills(
        self, status: Optional[str] = None, include_roles: bool = True
    ) -> List[SkillDefinition]:
        """
        Get all skills in the catalog.

        Args:
            status: Optional status filter
            include_roles: If True, populate allowed_app_roles field

        Returns:
            List of SkillDefinition objects
        """
        skills = await self.repository.list_skills(status=status)

        if include_roles:
            for skill in skills:
                roles = await self.get_roles_for_skill(skill.skill_id)
                skill.allowed_app_roles = [
                    r.role_id for r in roles if r.grant_type == "direct"
                ]

        return skills

    async def get_skill(self, skill_id: str) -> Optional[SkillDefinition]:
        """Get a specific skill by ID."""
        return await self.repository.get_skill(skill_id)

    async def create_skill(
        self, skill: SkillDefinition, admin: User
    ) -> SkillDefinition:
        """
        Create a new skill catalog entry.

        Args:
            skill: Skill definition to create
            admin: Admin user performing the action

        Returns:
            Created SkillDefinition

        Raises:
            ValueError: If the skill already exists
        """
        skill.created_by = admin.user_id
        skill.updated_by = admin.user_id

        created = await self.repository.create_skill(skill)
        self._write_skill_md(created)

        logger.info(
            f"Admin {admin.email} created skill: {skill.skill_id}",
            extra={
                "event": "skill_created",
                "skill_id": skill.skill_id,
                "admin_user_id": admin.user_id,
                "admin_email": admin.email,
            },
        )

        return created

    async def update_skill(
        self, skill_id: str, updates: Dict, admin: User
    ) -> Optional[SkillDefinition]:
        """
        Update a skill's metadata.

        Args:
            skill_id: Skill identifier
            updates: Fields to update (snake_case attribute names)
            admin: Admin user performing the action

        Returns:
            Updated SkillDefinition or None if not found
        """
        updated = await self.repository.update_skill(
            skill_id, updates, admin_user_id=admin.user_id
        )

        if updated:
            # Keep the SKILL.md projection in sync with the row (best-effort).
            # A pure resource-manifest update also lands here, but regenerating
            # is cheap and idempotent.
            self._write_skill_md(updated)
            logger.info(
                f"Admin {admin.email} updated skill: {skill_id}",
                extra={
                    "event": "skill_updated",
                    "skill_id": skill_id,
                    "admin_user_id": admin.user_id,
                    "admin_email": admin.email,
                    "changes": list(updates.keys()),
                },
            )

        return updated

    async def delete_skill(
        self, skill_id: str, admin: User, soft: bool = True
    ) -> bool:
        """
        Delete a skill from the catalog.

        By default performs a soft delete (status -> DISABLED). A hard delete
        removes the catalog row.

        Args:
            skill_id: Skill identifier
            admin: Admin user performing the action
            soft: If True, disable instead of deleting

        Returns:
            True if deleted/disabled, False if not found
        """
        existing = await self.repository.get_skill(skill_id)
        if existing is None:
            return False

        if soft:
            result = await self.repository.soft_delete_skill(skill_id, admin.user_id)
            deleted = result is not None
        else:
            deleted = await self.repository.delete_skill(skill_id)

        if deleted:
            logger.info(
                f"Admin {admin.email} deleted skill: {skill_id}",
                extra={
                    "event": "skill_deleted",
                    "skill_id": skill_id,
                    "admin_user_id": admin.user_id,
                    "admin_email": admin.email,
                    "soft_delete": soft,
                },
            )

        return deleted

    # =========================================================================
    # Admin Methods - Reference Files (S3-backed)
    # =========================================================================

    async def list_resources(self, skill_id: str) -> List[SkillResourceRef]:
        """Return a skill's reference-file manifest.

        Raises:
            ValueError: If the skill does not exist (mapped to 404 by route).
        """
        skill = await self.repository.get_skill(skill_id)
        if skill is None:
            raise ValueError(f"Skill '{skill_id}' not found")
        return list(skill.resources)

    async def add_resource(
        self,
        skill_id: str,
        filename: str,
        content: bytes,
        content_type: str,
        admin: User,
        kind: str = "reference",
    ) -> List[SkillResourceRef]:
        """Upload (or replace) one supporting file and update the manifest.

        Bytes are stored in the standard agentskills.io bundle layout
        (``skills/{id}/{references|scripts|assets}/{filename}``); the manifest on
        the catalog row is updated atomically (single row write). Re-uploading
        the same filename replaces its manifest entry (and overwrites its object
        in place); a file whose ``(kind, filename)`` key changed is
        garbage-collected. ``script`` files are accept-and-inert (stored,
        listed, never executed — D5).

        Returns the skill's updated manifest.

        Raises:
            ValueError: If the skill is missing, the kind or filename is invalid,
                the file is too large, or the per-skill file cap is exceeded.
        """
        if kind not in VALID_RESOURCE_KINDS:
            raise ValueError(
                f"Invalid resource kind '{kind}'. Expected one of "
                f"{', '.join(VALID_RESOURCE_KINDS)}."
            )

        skill = await self.repository.get_skill(skill_id)
        if skill is None:
            raise ValueError(f"Skill '{skill_id}' not found")

        self._validate_filename(filename)
        if len(content) > MAX_RESOURCE_BYTES:
            raise ValueError(
                f"Reference file '{filename}' is {len(content)} bytes; "
                f"the limit is {MAX_RESOURCE_BYTES} bytes."
            )
        if not content:
            raise ValueError(f"Reference file '{filename}' is empty.")

        existing = list(skill.resources)
        # Adding a NEW filename must respect the per-skill cap; replacing an
        # existing filename is always allowed.
        is_new = all(r.filename != filename for r in existing)
        if is_new and len(existing) >= MAX_RESOURCES_PER_SKILL:
            raise ValueError(
                f"Skill '{skill_id}' already has the maximum of "
                f"{MAX_RESOURCES_PER_SKILL} reference files."
            )

        resolved_type = content_type or "application/octet-stream"
        digest = compute_content_hash(content)
        s3_key = self.resource_store.put(
            skill_id=skill_id,
            filename=filename,
            content=content,
            content_type=resolved_type,
            kind=kind,
        )
        new_ref = SkillResourceRef(
            filename=filename,
            content_hash=digest,
            size=len(content),
            content_type=resolved_type,
            s3_key=s3_key,
            kind=kind,
        )

        new_resources = [r for r in existing if r.filename != filename]
        new_resources.append(new_ref)
        new_resources.sort(key=lambda r: r.filename)

        await self._persist_resources(skill_id, new_resources, admin)
        self._gc_orphaned(existing, new_resources)

        logger.info(
            f"Admin {admin.email} uploaded reference file to skill {skill_id}",
            extra={
                "event": "skill_resource_added",
                "skill_id": skill_id,
                # NB: not "filename" — that key is reserved on LogRecord and
                # raises KeyError when the record is actually emitted.
                "resource_filename": filename,
                "size": len(content),
                "admin_user_id": admin.user_id,
            },
        )
        return new_resources

    async def read_resource(
        self, skill_id: str, filename: str
    ) -> Tuple[SkillResourceRef, bytes]:
        """Return one reference file's manifest entry and its bytes.

        Raises:
            ValueError: If the skill or the named file does not exist.
        """
        skill = await self.repository.get_skill(skill_id)
        if skill is None:
            raise ValueError(f"Skill '{skill_id}' not found")

        ref = next((r for r in skill.resources if r.filename == filename), None)
        if ref is None:
            raise ValueError(
                f"Reference file '{filename}' not found on skill '{skill_id}'"
            )

        content = self.resource_store.get(ref.s3_key)
        return ref, content

    async def delete_resource(
        self, skill_id: str, filename: str, admin: User
    ) -> List[SkillResourceRef]:
        """Remove one reference file from the manifest (and GC its object).

        Returns the skill's updated manifest.

        Raises:
            ValueError: If the skill or the named file does not exist.
        """
        skill = await self.repository.get_skill(skill_id)
        if skill is None:
            raise ValueError(f"Skill '{skill_id}' not found")

        existing = list(skill.resources)
        if all(r.filename != filename for r in existing):
            raise ValueError(
                f"Reference file '{filename}' not found on skill '{skill_id}'"
            )

        new_resources = [r for r in existing if r.filename != filename]
        await self._persist_resources(skill_id, new_resources, admin)
        self._gc_orphaned(existing, new_resources)

        logger.info(
            f"Admin {admin.email} deleted reference file from skill {skill_id}",
            extra={
                "event": "skill_resource_deleted",
                "skill_id": skill_id,
                # NB: not "filename" — reserved on LogRecord (see add_resource).
                "resource_filename": filename,
                "admin_user_id": admin.user_id,
            },
        )
        return new_resources

    @staticmethod
    def _validate_filename(filename: str) -> None:
        """Reject path traversal / unsafe filenames up front."""
        if not _FILENAME_PATTERN.match(filename or ""):
            raise ValueError(
                f"Invalid reference filename '{filename}'. Use letters, "
                "digits, '.', '_' and '-' only (no path separators), "
                "1-128 characters."
            )

    async def _persist_resources(
        self,
        skill_id: str,
        resources: List[SkillResourceRef],
        admin: User,
    ) -> None:
        """Write the manifest to the catalog row (single atomic item write).

        The SKILL.md projection is NOT rewritten here: its frontmatter does not
        list resources, so a manifest change never alters it.
        """
        await self.repository.update_skill(
            skill_id, {"resources": resources}, admin_user_id=admin.user_id
        )

    def _write_skill_md(self, skill: SkillDefinition) -> None:
        """Write the skill's SKILL.md projection to S3 (best-effort).

        The DynamoDB row is the source of truth; this projection exists only so
        the S3 prefix is a valid, portable agentskills.io bundle. When storage
        is unconfigured (local dev) or the write fails, log and continue — never
        fail the catalog write on the projection.
        """
        if not self.resource_store.enabled:
            return
        try:
            content = generate_skill_md(
                skill_id=skill.skill_id,
                description=skill.description,
                instructions=skill.instructions,
                allowed_tools=skill.allowed_tools,
                skill_metadata=skill.skill_metadata,
            )
            self.resource_store.put_skill_md(skill_id=skill.skill_id, content=content)
        except SkillResourceStoreError:
            logger.warning(
                "skill-resources: SKILL.md projection failed for skill=%s "
                "(row is source of truth; continuing)",
                skill.skill_id,
                exc_info=True,
            )

    def _gc_orphaned(
        self,
        old_resources: List[SkillResourceRef],
        new_resources: List[SkillResourceRef],
    ) -> None:
        """Delete S3 objects no longer referenced by the new manifest.

        Keys are now path-based (``.../{kind}/{filename}``), so a removed file —
        or one whose kind changed — orphans its old key. Best-effort: a failed
        cleanup never fails the write; an orphaned object is only wasted storage.
        """
        old_keys = {r.s3_key for r in old_resources}
        new_keys = {r.s3_key for r in new_resources}
        for key in old_keys - new_keys:
            self.resource_store.delete(key)

    # =========================================================================
    # Admin Methods - Role Sync
    # =========================================================================

    async def get_roles_for_skill(self, skill_id: str) -> List[SkillRoleAssignment]:
        """
        Get all AppRoles that grant access to a skill.

        Reuses the ToolRoleMappingIndex GSI on the AppRoles table with a
        ``SKILL#`` partition value.

        Args:
            skill_id: Skill identifier

        Returns:
            List of SkillRoleAssignment objects
        """
        role_infos = await self.app_role_admin_service.repository.get_roles_for_skill(
            skill_id
        )

        assignments = []
        for info in role_infos:
            role_id = info.get("roleId")
            if not role_id:
                continue

            role = await self.app_role_admin_service.get_role(role_id)
            if not role:
                continue

            grant_type = "direct" if skill_id in role.granted_skills else "inherited"
            inherited_from = None

            if grant_type == "inherited":
                for parent_id in role.inherits_from:
                    parent = await self.app_role_admin_service.get_role(parent_id)
                    if parent and skill_id in parent.effective_permissions.skills:
                        inherited_from = parent_id
                        break

            assignments.append(
                SkillRoleAssignment(
                    role_id=role_id,
                    display_name=role.display_name,
                    grant_type=grant_type,
                    inherited_from=inherited_from,
                    enabled=role.enabled,
                )
            )

        return assignments

    async def set_roles_for_skill(
        self, skill_id: str, app_role_ids: List[str], admin: User
    ) -> None:
        """
        Set which AppRoles grant access to a skill (bidirectional sync).

        Updates the grantedSkills field on each affected AppRole. Roles not in
        the list have this skill removed from their grantedSkills.

        Args:
            skill_id: Skill identifier
            app_role_ids: AppRole IDs that should grant this skill
            admin: Admin user performing the action
        """
        skill = await self.get_skill(skill_id)
        if not skill:
            raise ValueError(f"Skill '{skill_id}' not found")

        current_roles = await self.get_roles_for_skill(skill_id)
        current_role_ids = {
            r.role_id for r in current_roles if r.grant_type == "direct"
        }
        new_role_ids = set(app_role_ids)

        to_add = new_role_ids - current_role_ids
        to_remove = current_role_ids - new_role_ids

        for role_id in to_add:
            await self._add_skill_to_role(role_id, skill_id, admin)
        for role_id in to_remove:
            await self._remove_skill_from_role(role_id, skill_id, admin)

        logger.info(
            f"Admin {admin.email} set roles for skill {skill_id}",
            extra={
                "event": "skill_roles_updated",
                "skill_id": skill_id,
                "admin_user_id": admin.user_id,
                "roles_added": list(to_add),
                "roles_removed": list(to_remove),
            },
        )

    async def add_roles_to_skill(
        self, skill_id: str, app_role_ids: List[str], admin: User
    ) -> None:
        """Add AppRoles to skill access (preserves existing)."""
        for role_id in app_role_ids:
            await self._add_skill_to_role(role_id, skill_id, admin)

    async def remove_roles_from_skill(
        self, skill_id: str, app_role_ids: List[str], admin: User
    ) -> None:
        """Remove AppRoles from skill access."""
        for role_id in app_role_ids:
            await self._remove_skill_from_role(role_id, skill_id, admin)

    async def _add_skill_to_role(
        self, role_id: str, skill_id: str, admin: User
    ) -> None:
        """Add a skill to a role's grantedSkills."""
        role = await self.app_role_admin_service.get_role(role_id)
        if not role:
            raise ValueError(f"Role '{role_id}' not found")

        if skill_id not in role.granted_skills:
            from apis.shared.rbac.models import AppRoleUpdate

            updates = AppRoleUpdate(granted_skills=role.granted_skills + [skill_id])
            await self.app_role_admin_service.update_role(role_id, updates, admin)

    async def _remove_skill_from_role(
        self, role_id: str, skill_id: str, admin: User
    ) -> None:
        """Remove a skill from a role's grantedSkills."""
        role = await self.app_role_admin_service.get_role(role_id)
        if not role:
            raise ValueError(f"Role '{role_id}' not found")

        if skill_id in role.granted_skills:
            from apis.shared.rbac.models import AppRoleUpdate

            new_skills = [s for s in role.granted_skills if s != skill_id]
            updates = AppRoleUpdate(granted_skills=new_skills)
            await self.app_role_admin_service.update_role(role_id, updates, admin)


# Global service instance
_service_instance: Optional[SkillCatalogService] = None


def get_skill_catalog_service() -> SkillCatalogService:
    """Get or create the global SkillCatalogService instance."""
    global _service_instance
    if _service_instance is None:
        _service_instance = SkillCatalogService()
    return _service_instance
