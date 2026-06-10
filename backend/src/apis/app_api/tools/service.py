"""
Tool Catalog Service

Service for tool catalog operations with AppRole integration.
Provides CRUD operations, user access computation, and bidirectional role sync.
"""

import logging
from typing import Dict, List, Optional

from apis.shared.auth.models import User
from apis.shared.rbac.models import UserEffectivePermissions
from apis.shared.rbac.service import AppRoleService, get_app_role_service
from apis.shared.rbac.admin_service import AppRoleAdminService, get_app_role_admin_service

from apis.shared.tools.models import (
    ToolDefinition,
    ToolProtocol,
    UserToolAccess,
    UserToolServerTool,
    UserToolPreference,
    ToolCategory,
    ToolRoleAssignment,
)
from apis.shared.tools.repository import ToolCatalogRepository, get_tool_catalog_repository
from apis.shared.tools.scoped_ids import SCOPE_DELIMITER, parse_scoped_tool_id

logger = logging.getLogger(__name__)


class ToolCatalogService:
    """
    Service for tool catalog operations.

    Tool access is determined by AppRoles. This service provides:
    - Catalog management (CRUD for tool metadata)
    - User preference management
    - Access computation using AppRoleService
    - Bidirectional sync between tools and AppRoles
    """

    def __init__(
        self,
        repository: Optional[ToolCatalogRepository] = None,
        app_role_service: Optional[AppRoleService] = None,
        app_role_admin_service: Optional[AppRoleAdminService] = None,
        gateway_target_service: Optional[object] = None,
    ):
        """Initialize with dependencies."""
        self.repository = repository or get_tool_catalog_repository()
        self.app_role_service = app_role_service or get_app_role_service()
        self.app_role_admin_service = app_role_admin_service or get_app_role_admin_service()
        # Lazily defaulted — only protocol='mcp' tools touch the Gateway, and we
        # don't want to construct a bedrock-agentcore-control client for every
        # ToolCatalogService. Injectable for tests.
        self._gateway_target_service = gateway_target_service

    def _get_gateway_target_service(self):
        """Return the Gateway target service, defaulting to the singleton."""
        if self._gateway_target_service is None:
            from apis.shared.tools.gateway_target_service import (
                get_gateway_target_service,
            )
            self._gateway_target_service = get_gateway_target_service()
        return self._gateway_target_service

    # =========================================================================
    # User-Facing Methods
    # =========================================================================

    async def get_user_accessible_tools(self, user: User) -> List[UserToolAccess]:
        """
        Get tools accessible to a user based on their AppRole permissions.

        This is the main entry point for the GET /tools endpoint.

        Args:
            user: Authenticated user

        Returns:
            List of UserToolAccess objects with user's accessible tools
        """
        # Get effective permissions from AppRoleService
        permissions = await self.app_role_service.resolve_user_permissions(user)

        # Get all active tools from catalog
        all_tools = await self._get_all_active_tools()

        # Get user preferences
        prefs = await self.repository.get_user_preferences(user.user_id)

        accessible = []
        for tool in all_tools:
            granted_by = self._compute_granted_by(tool, permissions)

            if not granted_by:
                continue

            user_enabled = prefs.tool_preferences.get(tool.tool_id)
            default_enabled = (
                user_enabled if user_enabled is not None else tool.enabled_by_default
            )

            # Surface an MCP server's individual tools so the UI can enable a
            # subset. Empty for non-MCP tools or servers with no curated list.
            # Each sub-tool's effective state: its own scoped preference, else
            # the server-level preference, else the catalog default.
            cfg = tool.mcp_config or tool.mcp_gateway_config
            server_tools = []
            for entry in getattr(cfg, "tools", None) or []:
                scoped_key = f"{tool.tool_id}{SCOPE_DELIMITER}{entry.name}"
                scoped_pref = prefs.tool_preferences.get(scoped_key)
                server_tools.append(
                    UserToolServerTool(
                        name=entry.name,
                        description=entry.description,
                        needs_approval=entry.needs_approval,
                        enabled=scoped_pref if scoped_pref is not None else default_enabled,
                    )
                )

            # The server row is "on" when any of its tools are on (or, for a
            # server with no curated list, the server-level preference applies).
            is_enabled = (
                any(st.enabled for st in server_tools)
                if server_tools
                else default_enabled
            )

            accessible.append(
                UserToolAccess(
                    tool_id=tool.tool_id,
                    display_name=tool.display_name,
                    description=tool.description,
                    category=tool.category,
                    protocol=tool.protocol,
                    status=tool.status,
                    granted_by=granted_by,
                    enabled_by_default=tool.enabled_by_default,
                    user_enabled=user_enabled,
                    is_enabled=is_enabled,
                    server_tools=server_tools,
                )
            )

        return sorted(
            accessible,
            key=lambda t: (t.category if isinstance(t.category, str) else t.category.value, t.display_name),
        )

    def _compute_granted_by(
        self, tool: ToolDefinition, permissions: UserEffectivePermissions
    ) -> List[str]:
        """Compute which sources grant access to this tool."""
        granted_by = []

        if tool.is_public:
            granted_by.append("public")

        if "*" in permissions.tools or tool.tool_id in permissions.tools:
            granted_by.extend(permissions.app_roles)

        return list(set(granted_by))

    async def get_categories(self, user: User) -> List[str]:
        """Get unique categories for user's accessible tools."""
        tools = await self.get_user_accessible_tools(user)
        categories = set()
        for tool in tools:
            cat = tool.category
            if isinstance(cat, ToolCategory):
                categories.add(cat.value)
            else:
                categories.add(cat)
        return sorted(categories)

    async def save_user_preferences(
        self, user: User, preferences: Dict[str, bool]
    ) -> UserToolPreference:
        """
        Save user's tool preferences.

        Validates that user has access to the tools being configured.

        Args:
            user: Authenticated user
            preferences: Map of tool_id -> enabled state

        Returns:
            Updated UserToolPreference

        Raises:
            ValueError: If user tries to configure tools they don't have access to
        """
        # Get accessible tools. A preference key may be scoped
        # (`tool_id::mcp_tool_name`) to enable a single tool of an MCP server;
        # validate the base catalog id for access (RBAC) and the tool name
        # against the server's curated list when it has one.
        accessible = await self.get_user_accessible_tools(user)
        accessible_by_id = {t.tool_id: t for t in accessible}

        invalid_tools = set()
        unexposed_tools = set()
        for key in preferences:
            base, tool_name = parse_scoped_tool_id(key)
            if base not in accessible_by_id:
                invalid_tools.add(key)
                continue
            if tool_name is not None:
                server_names = {st.name for st in accessible_by_id[base].server_tools}
                if server_names and tool_name not in server_names:
                    unexposed_tools.add(key)

        if invalid_tools:
            raise ValueError(
                f"Cannot configure tools user doesn't have access to: {invalid_tools}"
            )
        if unexposed_tools:
            raise ValueError(
                f"Cannot configure tools not exposed by their server: {unexposed_tools}"
            )

        # Save preferences
        return await self.repository.save_user_preferences(user.user_id, preferences)

    # =========================================================================
    # Admin Methods - Tool CRUD
    # =========================================================================

    async def get_all_tools(
        self, status: Optional[str] = None, include_roles: bool = True
    ) -> List[ToolDefinition]:
        """
        Get all tools in the catalog.

        Args:
            status: Optional status filter
            include_roles: If True, populate allowed_app_roles field

        Returns:
            List of ToolDefinition objects
        """
        tools = await self._get_all_active_tools(status=status)

        if include_roles:
            for tool in tools:
                roles = await self.get_roles_for_tool(tool.tool_id)
                tool.allowed_app_roles = [r.role_id for r in roles if r.grant_type == "direct"]

        return tools

    async def get_tool(self, tool_id: str) -> Optional[ToolDefinition]:
        """Get a specific tool by ID."""
        return await self.repository.get_tool(tool_id)

    def _validate_auth_config(self, tool: ToolDefinition) -> None:
        """
        Validate that auth configurations don't conflict.

        Raises:
            ValueError: If forward_auth_token and requires_oauth_provider are both set,
                or if forward_auth_token is set with a non-'none' MCP auth type.
        """
        if tool.forward_auth_token and tool.requires_oauth_provider:
            raise ValueError(
                "Cannot enable both 'forward_auth_token' and 'requires_oauth_provider'. "
                "Both use the Authorization header and are mutually exclusive."
            )

        if tool.forward_auth_token and tool.mcp_config:
            auth_type = tool.mcp_config.auth_type
            if isinstance(auth_type, str):
                is_none = auth_type == "none"
            else:
                from apis.shared.tools.models import MCPAuthType
                is_none = auth_type == MCPAuthType.NONE
            if not is_none:
                raise ValueError(
                    "When 'forward_auth_token' is enabled, MCP auth type must be 'none'. "
                    "The OIDC token will use the Authorization header."
                )

    def _validate_protocol_config(self, tool: ToolDefinition) -> None:
        """Validate that the protocol-specific config matches the protocol.

        v1 only gates the Gateway protocol (mcp ⟺ mcp_gateway_config); the
        mcp_external/a2a pairings are left untouched to avoid changing existing
        create flows.

        Raises:
            ValueError: If protocol is 'mcp' without a gateway config, or a
                gateway config is set on a non-'mcp' protocol.
        """
        is_gateway = tool.protocol == ToolProtocol.MCP_GATEWAY
        if is_gateway and tool.mcp_gateway_config is None:
            raise ValueError(
                "protocol 'mcp' (MCP Gateway) requires mcp_gateway_config"
            )
        if not is_gateway and tool.mcp_gateway_config is not None:
            raise ValueError(
                "mcp_gateway_config is only valid for protocol 'mcp' (MCP Gateway)"
            )

    async def create_tool(
        self, tool: ToolDefinition, admin: User
    ) -> ToolDefinition:
        """
        Create a new tool catalog entry.

        For protocol='mcp' (MCP Gateway), the live Gateway target is created in
        AWS *first*; the catalog row is persisted only on success, and the
        AWS-assigned target_id/gateway_arn are stamped onto the stored config so
        update/delete can reconcile the target later. If the AWS target is
        created but persisting the row fails, the orphaned target id is logged
        loudly (v1 repair is manual).

        Args:
            tool: Tool definition to create
            admin: Admin user performing the action

        Returns:
            Created ToolDefinition

        Raises:
            ValueError: If auth or protocol configuration is invalid
            GatewayTargetConflictError: If the Gateway target name already exists
            botocore.exceptions.ClientError / RuntimeError: On AWS failure
        """
        self._validate_auth_config(tool)
        self._validate_protocol_config(tool)

        # Create the live Gateway target before persisting the row, so the
        # catalog never references a target that doesn't exist.
        if tool.protocol == ToolProtocol.MCP_GATEWAY:
            info = self._get_gateway_target_service().create_target(
                tool.mcp_gateway_config
            )
            tool.mcp_gateway_config.target_id = info.target_id
            tool.mcp_gateway_config.gateway_arn = info.gateway_arn

        tool.created_by = admin.user_id
        tool.updated_by = admin.user_id

        try:
            created = await self.repository.create_tool(tool)
        except Exception:
            if (
                tool.protocol == ToolProtocol.MCP_GATEWAY
                and tool.mcp_gateway_config
                and tool.mcp_gateway_config.target_id
            ):
                logger.error(
                    "ORPHANED GATEWAY TARGET: created Gateway target '%s' but "
                    "failed to persist catalog row for tool '%s'. Manual cleanup "
                    "required (delete-gateway-target).",
                    tool.mcp_gateway_config.target_id,
                    tool.tool_id,
                    extra={
                        "event": "gateway_target_orphaned",
                        "orphaned_target_id": tool.mcp_gateway_config.target_id,
                        "tool_id": tool.tool_id,
                    },
                )
            raise

        logger.info(
            f"Admin {admin.email} created tool: {tool.tool_id}",
            extra={
                "event": "tool_created",
                "tool_id": tool.tool_id,
                "admin_user_id": admin.user_id,
                "admin_email": admin.email,
            },
        )

        return created

    async def update_tool(
        self, tool_id: str, updates: Dict, admin: User
    ) -> Optional[ToolDefinition]:
        """
        Update a tool's metadata.

        Args:
            tool_id: Tool identifier
            updates: Fields to update
            admin: Admin user performing the action

        Returns:
            Updated ToolDefinition or None if not found

        Raises:
            ValueError: If the resulting auth configuration is invalid
        """
        # Fetch the existing row once if any field that needs it is changing.
        needs_existing = any(
            k in updates
            for k in (
                "forward_auth_token",
                "requires_oauth_provider",
                "mcp_config",
                "protocol",
                "mcp_gateway_config",
            )
        )
        existing = (
            await self.repository.get_tool(tool_id) if needs_existing else None
        )

        # Pre-validate auth config if relevant fields are being updated
        if existing and (
            "forward_auth_token" in updates
            or "requires_oauth_provider" in updates
            or "mcp_config" in updates
        ):
            # Build a preview of the updated tool for validation
            preview = ToolDefinition(
                tool_id=existing.tool_id,
                display_name=existing.display_name,
                description=existing.description,
                protocol=existing.protocol,
                forward_auth_token=updates.get("forward_auth_token", existing.forward_auth_token),
                requires_oauth_provider=updates.get("requires_oauth_provider", existing.requires_oauth_provider),
                mcp_config=updates.get("mcp_config", existing.mcp_config),
            )
            self._validate_auth_config(preview)

        # Reconcile the live Gateway target before persisting the row.
        gateway_reconciled = False
        if existing is not None and (
            "protocol" in updates or "mcp_gateway_config" in updates
        ):
            gateway_reconciled = self._reconcile_gateway_target_for_update(
                existing, updates
            )

        try:
            updated = await self.repository.update_tool(
                tool_id, updates, admin_user_id=admin.user_id
            )
        except Exception:
            if gateway_reconciled:
                logger.error(
                    "Gateway target for tool '%s' was updated in AWS but the "
                    "catalog row update failed; state diverged.",
                    tool_id,
                    extra={"event": "gateway_target_diverged", "tool_id": tool_id},
                )
            raise

        if updated:
            logger.info(
                f"Admin {admin.email} updated tool: {tool_id}",
                extra={
                    "event": "tool_updated",
                    "tool_id": tool_id,
                    "admin_user_id": admin.user_id,
                    "admin_email": admin.email,
                    "changes": list(updates.keys()),
                },
            )

        return updated

    def _reconcile_gateway_target_for_update(
        self, existing: ToolDefinition, updates: Dict
    ) -> bool:
        """Reconcile the live Gateway target for an update, mutating `updates`.

        Returns True if an AWS target call was made. Raises ValueError on an
        unsupported protocol transition; the AWS-assigned target_id is preserved
        across the update.
        """
        new_protocol = updates.get("protocol", existing.protocol)
        was_gateway = existing.protocol == ToolProtocol.MCP_GATEWAY
        now_gateway = new_protocol == ToolProtocol.MCP_GATEWAY

        if was_gateway != now_gateway:
            raise ValueError(
                "Cannot change a tool's protocol to or from 'mcp' (MCP Gateway) "
                "via update; delete and recreate the tool instead."
            )

        if not now_gateway:
            return False

        new_config = updates.get("mcp_gateway_config")
        if new_config is None:
            # Gateway tool, but the target config isn't changing — leave the
            # live target (and its stored target_id) untouched.
            return False

        existing_cfg = existing.mcp_gateway_config
        target_id = existing_cfg.target_id if existing_cfg else None
        gateway = self._get_gateway_target_service()
        if target_id:
            info = gateway.update_target(target_id=target_id, config=new_config)
            new_config.target_id = target_id
            new_config.gateway_arn = info.gateway_arn or (
                existing_cfg.gateway_arn if existing_cfg else None
            )
        else:
            # No stored target (shouldn't happen for a gateway row) — create one.
            info = gateway.create_target(new_config)
            new_config.target_id = info.target_id
            new_config.gateway_arn = info.gateway_arn
        updates["mcp_gateway_config"] = new_config
        return True

    async def delete_tool(self, tool_id: str, admin: User, soft: bool = True) -> bool:
        """
        Delete a tool from the catalog.

        For protocol='mcp' (MCP Gateway), a *hard* delete removes the live
        Gateway target first (a 404 is tolerated as already-gone), then the
        catalog row. A *soft* delete only disables the catalog row and leaves
        the Gateway target in place so the tool can be re-enabled.

        Args:
            tool_id: Tool identifier
            admin: Admin user performing the action
            soft: If True, mark as disabled instead of deleting

        Returns:
            True if deleted/disabled, False if not found

        Raises:
            botocore.exceptions.ClientError: On a non-404 AWS failure deleting
                the Gateway target (the catalog row is left intact).
        """
        existing = await self.repository.get_tool(tool_id)
        if existing is None:
            return False

        # Remove the live Gateway target before the row, so we never delete the
        # catalog row while a target still points at it.
        target_deleted = False
        if (
            not soft
            and existing.protocol == ToolProtocol.MCP_GATEWAY
            and existing.mcp_gateway_config
            and existing.mcp_gateway_config.target_id
        ):
            self._get_gateway_target_service().delete_target(
                target_id=existing.mcp_gateway_config.target_id,
                config=existing.mcp_gateway_config,
            )
            target_deleted = True

        try:
            if soft:
                result = await self.repository.soft_delete_tool(tool_id, admin.user_id)
                deleted = result is not None
            else:
                deleted = await self.repository.delete_tool(tool_id)
        except Exception:
            if target_deleted:
                logger.error(
                    "Deleted Gateway target '%s' but failed to remove catalog "
                    "row for tool '%s'; state diverged (manual cleanup of the "
                    "row required).",
                    existing.mcp_gateway_config.target_id,
                    tool_id,
                    extra={"event": "gateway_target_diverged", "tool_id": tool_id},
                )
            raise

        if deleted:
            logger.info(
                f"Admin {admin.email} deleted tool: {tool_id}",
                extra={
                    "event": "tool_deleted",
                    "tool_id": tool_id,
                    "admin_user_id": admin.user_id,
                    "admin_email": admin.email,
                    "soft_delete": soft,
                },
            )

        return deleted

    # =========================================================================
    # Admin Methods - Role Sync
    # =========================================================================

    async def get_roles_for_tool(self, tool_id: str) -> List[ToolRoleAssignment]:
        """
        Get all AppRoles that grant access to a tool.

        Uses the ToolRoleMappingIndex GSI on AppRoles table.

        Args:
            tool_id: Tool identifier

        Returns:
            List of ToolRoleAssignment objects
        """
        # Query roles from repository
        role_infos = await self.app_role_admin_service.repository.get_roles_for_tool(tool_id)

        assignments = []
        for info in role_infos:
            role_id = info.get("roleId")
            if not role_id:
                continue

            # Get full role to check inheritance
            role = await self.app_role_admin_service.get_role(role_id)
            if not role:
                continue

            # Determine grant type
            grant_type = "direct" if tool_id in role.granted_tools else "inherited"
            inherited_from = None

            if grant_type == "inherited":
                # Find which parent role provides this tool
                for parent_id in role.inherits_from:
                    parent = await self.app_role_admin_service.get_role(parent_id)
                    if parent and tool_id in parent.effective_permissions.tools:
                        inherited_from = parent_id
                        break

            assignments.append(
                ToolRoleAssignment(
                    role_id=role_id,
                    display_name=role.display_name,
                    grant_type=grant_type,
                    inherited_from=inherited_from,
                    enabled=role.enabled,
                )
            )

        return assignments

    async def set_roles_for_tool(
        self, tool_id: str, app_role_ids: List[str], admin: User
    ) -> None:
        """
        Set which AppRoles grant access to a tool (bidirectional sync).

        This updates the grantedTools field on each affected AppRole.

        Args:
            tool_id: Tool identifier
            app_role_ids: List of AppRole IDs that should grant this tool
            admin: Admin user performing the action
        """
        # Verify tool exists
        tool = await self.get_tool(tool_id)
        if not tool:
            raise ValueError(f"Tool '{tool_id}' not found")

        # Get current roles that grant this tool (direct only)
        current_roles = await self.get_roles_for_tool(tool_id)
        current_role_ids = {r.role_id for r in current_roles if r.grant_type == "direct"}

        new_role_ids = set(app_role_ids)

        # Roles to add tool to
        to_add = new_role_ids - current_role_ids

        # Roles to remove tool from
        to_remove = current_role_ids - new_role_ids

        # Update each role
        for role_id in to_add:
            await self._add_tool_to_role(role_id, tool_id, admin)

        for role_id in to_remove:
            await self._remove_tool_from_role(role_id, tool_id, admin)

        logger.info(
            f"Admin {admin.email} set roles for tool {tool_id}",
            extra={
                "event": "tool_roles_updated",
                "tool_id": tool_id,
                "admin_user_id": admin.user_id,
                "roles_added": list(to_add),
                "roles_removed": list(to_remove),
            },
        )

    async def add_roles_to_tool(
        self, tool_id: str, app_role_ids: List[str], admin: User
    ) -> None:
        """Add AppRoles to tool access (preserves existing)."""
        for role_id in app_role_ids:
            await self._add_tool_to_role(role_id, tool_id, admin)

    async def remove_roles_from_tool(
        self, tool_id: str, app_role_ids: List[str], admin: User
    ) -> None:
        """Remove AppRoles from tool access."""
        for role_id in app_role_ids:
            await self._remove_tool_from_role(role_id, tool_id, admin)

    async def _add_tool_to_role(
        self, role_id: str, tool_id: str, admin: User
    ) -> None:
        """Add a tool to a role's grantedTools."""
        role = await self.app_role_admin_service.get_role(role_id)
        if not role:
            raise ValueError(f"Role '{role_id}' not found")

        if tool_id not in role.granted_tools:
            from apis.shared.rbac.models import AppRoleUpdate
            updates = AppRoleUpdate(granted_tools=role.granted_tools + [tool_id])
            await self.app_role_admin_service.update_role(role_id, updates, admin)

    async def _remove_tool_from_role(
        self, role_id: str, tool_id: str, admin: User
    ) -> None:
        """Remove a tool from a role's grantedTools."""
        role = await self.app_role_admin_service.get_role(role_id)
        if not role:
            raise ValueError(f"Role '{role_id}' not found")

        if tool_id in role.granted_tools:
            from apis.shared.rbac.models import AppRoleUpdate
            new_tools = [t for t in role.granted_tools if t != tool_id]
            updates = AppRoleUpdate(granted_tools=new_tools)
            await self.app_role_admin_service.update_role(role_id, updates, admin)

    # =========================================================================
    # Helper Methods
    # =========================================================================

    async def _get_all_active_tools(
        self, status: Optional[str] = None
    ) -> List[ToolDefinition]:
        """Get all tools from the catalog, optionally filtered by status."""
        return await self.repository.list_tools(status=status)


# Global service instance
_service_instance: Optional[ToolCatalogService] = None


def get_tool_catalog_service() -> ToolCatalogService:
    """Get or create the global ToolCatalogService instance."""
    global _service_instance
    if _service_instance is None:
        _service_instance = ToolCatalogService()
    return _service_instance
