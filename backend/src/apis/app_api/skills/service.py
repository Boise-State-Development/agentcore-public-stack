"""
Skill Catalog Service

Service for skill catalog operations with AppRole integration. Mirrors
``ToolCatalogService``: CRUD over skill metadata, bidirectional role sync
(updating ``granted_skills`` on AppRoles), and ``allowedAppRoles`` hydration.

Skill-specific: ``create_skill``/``update_skill`` validate that every
``bound_tool_id`` exists in the tool catalog and is ACTIVE (spec §6), since a
skill folds those catalog tools behind the meta-tools at runtime.
"""

import logging
from typing import Dict, List, Optional

from apis.shared.auth.models import User
from apis.shared.rbac.admin_service import (
    AppRoleAdminService,
    get_app_role_admin_service,
)
from apis.shared.rbac.service import AppRoleService, get_app_role_service
from apis.shared.skills.models import (
    SkillDefinition,
    SkillRoleAssignment,
)
from apis.shared.skills.repository import (
    SkillCatalogRepository,
    get_skill_catalog_repository,
)
from apis.shared.tools.models import ToolStatus
from apis.shared.tools.repository import (
    ToolCatalogRepository,
    get_tool_catalog_repository,
)

logger = logging.getLogger(__name__)


class SkillCatalogService:
    """
    Service for skill catalog operations.

    Skill access is determined by AppRoles (granted_skills). This service
    provides catalog CRUD, bound-tool validation against the tool catalog, and
    bidirectional sync between skills and AppRoles.
    """

    def __init__(
        self,
        repository: Optional[SkillCatalogRepository] = None,
        tool_repository: Optional[ToolCatalogRepository] = None,
        app_role_service: Optional[AppRoleService] = None,
        app_role_admin_service: Optional[AppRoleAdminService] = None,
    ):
        """Initialize with dependencies."""
        self.repository = repository or get_skill_catalog_repository()
        self.tool_repository = tool_repository or get_tool_catalog_repository()
        self.app_role_service = app_role_service or get_app_role_service()
        self.app_role_admin_service = (
            app_role_admin_service or get_app_role_admin_service()
        )

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

    async def _validate_bound_tools(self, bound_tool_ids: List[str]) -> None:
        """
        Validate that every bound tool exists in the catalog and is ACTIVE.

        A skill folds its bound catalog tools behind the meta-tools at runtime,
        so binding an unknown or disabled tool would silently drop it. Reject
        such bindings up front (spec §6).

        Raises:
            ValueError: If any bound tool is unknown or not ACTIVE.
        """
        if not bound_tool_ids:
            return

        # Dedupe while preserving the admin's set for clear error messages.
        requested = list(dict.fromkeys(bound_tool_ids))
        found = await self.tool_repository.batch_get_tools(requested)
        by_id = {t.tool_id: t for t in found}

        unknown = [tid for tid in requested if tid not in by_id]
        disabled = [
            tid
            for tid in requested
            if tid in by_id and by_id[tid].status != ToolStatus.ACTIVE.value
        ]

        problems = []
        if unknown:
            problems.append(f"unknown tool(s): {', '.join(sorted(unknown))}")
        if disabled:
            problems.append(f"non-active tool(s): {', '.join(sorted(disabled))}")
        if problems:
            raise ValueError(
                "Cannot bind " + "; ".join(problems) + ". "
                "Bound tools must exist in the catalog and be active."
            )

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
            ValueError: If a bound tool is unknown/disabled, or the skill exists
        """
        await self._validate_bound_tools(skill.bound_tool_ids)

        skill.created_by = admin.user_id
        skill.updated_by = admin.user_id

        created = await self.repository.create_skill(skill)

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

        Raises:
            ValueError: If the new bound tools are unknown/disabled
        """
        if "bound_tool_ids" in updates and updates["bound_tool_ids"] is not None:
            await self._validate_bound_tools(updates["bound_tool_ids"])

        updated = await self.repository.update_skill(
            skill_id, updates, admin_user_id=admin.user_id
        )

        if updated:
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
