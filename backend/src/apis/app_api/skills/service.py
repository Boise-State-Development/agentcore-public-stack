"""
Skill Catalog Service

Service for skill catalog operations with AppRole integration. Mirrors
``ToolCatalogService``: CRUD over skill metadata, bidirectional role sync
(updating ``granted_skills`` on AppRoles), and ``allowedAppRoles`` hydration.

Skills are pure knowledge bundles (Skills v2): they carry no bound tools, so
there is no tool-catalog validation here.
"""

import logging
from typing import Dict, List, Optional, Tuple

from apis.shared.auth.models import User
from apis.shared.rbac.admin_service import (
    AppRoleAdminService,
    get_app_role_admin_service,
)
from apis.shared.rbac.service import AppRoleService, get_app_role_service
from apis.shared.skills.models import (
    SYSTEM_OWNER_ID,
    SkillDefinition,
    SkillResourceRef,
    SkillRoleAssignment,
)
from apis.shared.skills.repository import (
    SkillCatalogRepository,
    get_skill_catalog_repository,
)
from apis.shared.security.log_sanitize import scrub_log
from apis.shared.skills.bundle_files import SkillBundleFiles
from apis.shared.skills.resource_store import (
    SkillResourceStore,
    get_skill_resource_store,
)

logger = logging.getLogger(__name__)


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
        self.bundle_files = SkillBundleFiles(
            repository=self.repository, resource_store=self.resource_store
        )

    # =========================================================================
    # Admin Methods - Skill CRUD
    # =========================================================================

    async def get_all_skills(
        self, status: Optional[str] = None, include_roles: bool = True
    ) -> List[SkillDefinition]:
        """
        Get all skills in the admin catalog.

        Scoped to ``owner_id == "system"``: user-authored skills (Skills v2
        PR-3) live in the same table but are governed by ownership, not by RBAC
        role grants, so they are not part of the catalog an admin curates and
        must not appear on the admin skills page.

        Args:
            status: Optional status filter
            include_roles: If True, populate allowed_app_roles field

        Returns:
            List of SkillDefinition objects
        """
        skills = await self.repository.list_skills(
            status=status, owner_id=SYSTEM_OWNER_ID
        )

        if include_roles:
            for skill in skills:
                roles = await self.get_roles_for_skill(skill.skill_id)
                skill.allowed_app_roles = [
                    r.role_id for r in roles if r.grant_type == "direct"
                ]

        return skills

    async def get_skill(self, skill_id: str) -> Optional[SkillDefinition]:
        """Get a specific skill by ID, from EITHER authorship tier.

        Tier-agnostic on purpose: it backs the catalog predicate below and the
        shared reference-file machinery, which the user tier reuses after its
        own ownership check.

        SECURITY: never expose this directly on an admin surface. ``admin.skills``
        governs the catalog (``owner_id == "system"``); a private user-authored
        skill is governed by ownership and is reachable by id here. Admin
        callers must go through :meth:`get_catalog_skill` or
        :meth:`require_catalog_skill`.
        """
        return await self.repository.get_skill(skill_id)

    async def get_catalog_skill(self, skill_id: str) -> Optional[SkillDefinition]:
        """Get a skill only if it belongs to the admin catalog.

        Returns None both when no such skill exists and when the row is a
        user-authored skill, so an admin surface cannot distinguish the two.
        This is the per-object mirror of :meth:`get_all_skills`, which is
        already scoped to ``owner_id == "system"``.
        """
        skill = await self.repository.get_skill(skill_id)
        if skill is None:
            return None
        if skill.owner_id != SYSTEM_OWNER_ID:
            logger.warning(
                "Admin skills surface reached a user-authored skill; treating "
                "as not found",
                extra={
                    "event": "admin_skill_cross_tier_denied",
                    "skill_id": scrub_log(skill_id),
                },
            )
            return None
        return skill

    async def require_catalog_skill(self, skill_id: str) -> SkillDefinition:
        """Assert a skill exists AND belongs to the admin catalog.

        Role grants and per-object admin writes are the catalog's governance
        mechanism. A user-authored skill (Skills v2 PR-3) is reached through
        ownership instead: its instructions are instruction-trusted content
        that steers its owner's agent, so ``admin.skills`` — whose remit is the
        curated catalog — must not read or rewrite one.

        Raises:
            ValueError: With a uniform ``not found`` message for both "no such
                skill" and "not a catalog skill". Callers map that to 404; a
                distinguishable error would confirm the existence of a private
                skill the caller cannot see.
        """
        skill = await self.get_catalog_skill(skill_id)
        if skill is None:
            raise ValueError(f"Skill '{skill_id}' not found")
        return skill

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
        self.bundle_files.write_skill_md(created)

        logger.info(
            f"Admin {scrub_log(admin.email)} created skill: {scrub_log(skill.skill_id)}",
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

        Scoped to the admin catalog: a user-authored skill is not updatable
        here and reports as not found (see :meth:`require_catalog_skill`).

        Args:
            skill_id: Skill identifier
            updates: Fields to update (snake_case attribute names)
            admin: Admin user performing the action

        Returns:
            Updated SkillDefinition or None if not found
        """
        if await self.get_catalog_skill(skill_id) is None:
            return None

        updated = await self.repository.update_skill(
            skill_id, updates, admin_user_id=admin.user_id
        )

        if updated:
            # Keep the SKILL.md projection in sync with the row (best-effort).
            # A pure resource-manifest update also lands here, but regenerating
            # is cheap and idempotent.
            self.bundle_files.write_skill_md(updated)
            logger.info(
                f"Admin {scrub_log(admin.email)} updated skill: {scrub_log(skill_id)}",
                extra={
                    "event": "skill_updated",
                    "skill_id": scrub_log(skill_id),
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
        removes the catalog row. Scoped to the admin catalog: a user-authored
        skill is not deletable here and reports as not found.

        Args:
            skill_id: Skill identifier
            admin: Admin user performing the action
            soft: If True, disable instead of deleting

        Returns:
            True if deleted/disabled, False if not found
        """
        existing = await self.get_catalog_skill(skill_id)
        if existing is None:
            return False

        if soft:
            result = await self.repository.soft_delete_skill(skill_id, admin.user_id)
            deleted = result is not None
        else:
            deleted = await self.repository.delete_skill(skill_id)
            if deleted:
                await self.delete_versions(skill_id)

        if deleted:
            logger.info(
                f"Admin {scrub_log(admin.email)} deleted skill: {scrub_log(skill_id)}",
                extra={
                    "event": "skill_deleted",
                    "skill_id": scrub_log(skill_id),
                    "admin_user_id": admin.user_id,
                    "admin_email": admin.email,
                    "soft_delete": soft,
                },
            )

        return deleted

    async def delete_versions(self, skill_id: str) -> None:
        """Drop a hard-deleted skill's pinned versions and their frozen files."""
        await self.bundle_files.delete_versions(skill_id)

    # =========================================================================
    # Admin Methods - Reference Files (S3-backed)
    #
    # The bundle layout itself lives in ``apis.shared.skills.bundle_files`` so
    # the user tier (and the runtime's authoring tools) write it through the
    # same code. These wrappers keep the admin surface's signatures.
    # =========================================================================

    async def list_resources(self, skill_id: str) -> List[SkillResourceRef]:
        """Return a catalog skill's reference-file manifest.

        Admin-tier entry point — the user tier has its own owner-scoped
        ``list_resources`` — so it applies the catalog predicate.

        Raises:
            ValueError: If the skill does not exist or is not a catalog skill
                (mapped to 404 by route).
        """
        skill = await self.require_catalog_skill(skill_id)
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
        """Upload (or replace) one supporting file; see ``SkillBundleFiles.add_resource``."""
        return await self.bundle_files.add_resource(
            skill_id, filename, content, content_type, admin, kind=kind
        )

    async def read_resource(
        self, skill_id: str, filename: str
    ) -> Tuple[SkillResourceRef, bytes]:
        """Return one reference file's manifest entry and its bytes."""
        return await self.bundle_files.read_resource(skill_id, filename)

    async def delete_resource(
        self, skill_id: str, filename: str, admin: User
    ) -> List[SkillResourceRef]:
        """Remove one reference file from the manifest (and GC its object)."""
        return await self.bundle_files.delete_resource(skill_id, filename, admin)

    def write_skill_md(self, skill: SkillDefinition) -> None:
        """Write the skill's SKILL.md projection to S3 (best-effort)."""
        self.bundle_files.write_skill_md(skill)

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
        await self.require_catalog_skill(skill_id)

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
            f"Admin {scrub_log(admin.email)} set roles for skill {scrub_log(skill_id)}",
            extra={
                "event": "skill_roles_updated",
                "skill_id": scrub_log(skill_id),
                "admin_user_id": admin.user_id,
                "roles_added": list(to_add),
                "roles_removed": list(to_remove),
            },
        )

    async def add_roles_to_skill(
        self, skill_id: str, app_role_ids: List[str], admin: User
    ) -> None:
        """Add AppRoles to skill access (preserves existing)."""
        await self.require_catalog_skill(skill_id)
        for role_id in app_role_ids:
            await self._add_skill_to_role(role_id, skill_id, admin)

    async def remove_roles_from_skill(
        self, skill_id: str, app_role_ids: List[str], admin: User
    ) -> None:
        """Remove AppRoles from skill access."""
        await self.require_catalog_skill(skill_id)
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
