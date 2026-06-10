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
from apis.shared.skills.resource_store import (
    SkillResourceStore,
    compute_content_hash,
    get_skill_resource_store,
)
from apis.shared.tools.models import ToolStatus
from apis.shared.tools.scoped_ids import (
    base_tool_id,
    base_tool_ids,
    parse_scoped_tool_id,
)
from apis.shared.tools.repository import (
    ToolCatalogRepository,
    get_tool_catalog_repository,
)

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
    provides catalog CRUD, bound-tool validation against the tool catalog, and
    bidirectional sync between skills and AppRoles.
    """

    def __init__(
        self,
        repository: Optional[SkillCatalogRepository] = None,
        tool_repository: Optional[ToolCatalogRepository] = None,
        app_role_service: Optional[AppRoleService] = None,
        app_role_admin_service: Optional[AppRoleAdminService] = None,
        resource_store: Optional[SkillResourceStore] = None,
    ):
        """Initialize with dependencies."""
        self.repository = repository or get_skill_catalog_repository()
        self.tool_repository = tool_repository or get_tool_catalog_repository()
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

    async def _validate_bound_tools(self, bound_tool_ids: List[str]) -> None:
        """
        Validate that every bound tool exists in the catalog and is ACTIVE.

        A skill folds its bound catalog tools behind the meta-tools at runtime,
        so binding an unknown or disabled tool would silently drop it. Reject
        such bindings up front (spec §6).

        A scoped id (``tool_id::mcp_tool_name``) binds a single tool of an MCP
        server. Its base catalog tool must exist + be active, and — when the
        server has a curated tool list — the named tool must be one it exposes.
        Servers whose tools are discovered live have no curated list, so the
        name can't be validated statically and is accepted.

        Raises:
            ValueError: If any bound tool is unknown, non-active, scoped onto a
                tool the server doesn't expose, or scoped onto a non-MCP tool.
        """
        if not bound_tool_ids:
            return

        # Dedupe while preserving the admin's set for clear error messages.
        requested = list(dict.fromkeys(bound_tool_ids))
        base_ids = base_tool_ids(requested)
        found = await self.tool_repository.batch_get_tools(base_ids)
        by_id = {t.tool_id: t for t in found}

        unknown = [tid for tid in requested if base_tool_id(tid) not in by_id]
        disabled = [
            tid
            for tid in requested
            if base_tool_id(tid) in by_id
            and by_id[base_tool_id(tid)].status != ToolStatus.ACTIVE.value
        ]

        # Per-tool bindings: the named tool must belong to the server (when the
        # server exposes a curated list) and the base must be an MCP server.
        unexposed: List[str] = []
        not_mcp: List[str] = []
        for tid in requested:
            base, tool_name = parse_scoped_tool_id(tid)
            if tool_name is None or base not in by_id or tid in disabled:
                continue
            tool = by_id[base]
            if tool.protocol not in ("mcp", "mcp_external"):
                not_mcp.append(tid)
                continue
            names = tool.curated_tool_names()
            if names is not None and tool_name not in names:
                unexposed.append(tid)

        problems = []
        if unknown:
            problems.append(f"unknown tool(s): {', '.join(sorted(unknown))}")
        if disabled:
            problems.append(f"non-active tool(s): {', '.join(sorted(disabled))}")
        if not_mcp:
            problems.append(
                f"per-tool binding on non-MCP tool(s): {', '.join(sorted(not_mcp))}"
            )
        if unexposed:
            problems.append(
                f"tool(s) not exposed by their server: {', '.join(sorted(unexposed))}"
            )
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
    ) -> List[SkillResourceRef]:
        """Upload (or replace) one reference file and update the manifest.

        Bytes are stored content-addressed in S3 (dedupe); the manifest on
        the catalog row is updated atomically (single row write). Re-uploading
        the same filename replaces its manifest entry; any S3 object that is
        no longer referenced afterward is garbage-collected.

        Returns the skill's updated manifest.

        Raises:
            ValueError: If the skill is missing, the filename is invalid, the
                file is too large, or the per-skill file cap is exceeded.
        """
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
            skill_id=skill_id, content=content, content_type=resolved_type
        )
        new_ref = SkillResourceRef(
            filename=filename,
            content_hash=digest,
            size=len(content),
            content_type=resolved_type,
            s3_key=s3_key,
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
        """Write the manifest to the catalog row (single atomic item write)."""
        await self.repository.update_skill(
            skill_id, {"resources": resources}, admin_user_id=admin.user_id
        )

    def _gc_orphaned(
        self,
        old_resources: List[SkillResourceRef],
        new_resources: List[SkillResourceRef],
    ) -> None:
        """Delete S3 objects no longer referenced by the new manifest.

        Objects are content-addressed, so a key still referenced by another
        manifest entry (identical content under a different filename) is
        retained. Best-effort: a failed cleanup never fails the write — the
        manifest is already consistent; an orphaned object is only wasted
        storage.
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
