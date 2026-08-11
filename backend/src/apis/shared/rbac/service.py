"""AppRoleService for resolving and checking AppRole-based permissions.

**A tool is granted by a role grant *or* by its own ``isPublic`` flag.**
Both are real grants and every gate here reads both, via the one
``_tool_grant_set`` helper. They used to disagree: ``isPublic`` was
honoured only by the tool *picker*
(``ToolCatalogService._compute_granted_by``) while the checks below read
role ``grantedTools`` alone, so a public-but-ungranted tool listed for
everyone and then failed at use — silently dropped from a scheduled run,
and a hard block on any Agent that bound it. Non-admins hit it; anyone
holding ``"*"`` never did. Keep the two sides reading the same flag; this
is the tools-axis twin of the ``_grants_access`` consolidation in
``admin/services/model_access.py``.

The public set is *not* merged into ``UserEffectivePermissions``. That
object is per-user cached and its ``tools`` list is order-sensitive
(it reaches the model's ``toolConfig``, where a flip re-writes the
prompt-cache prefix). Unioning at the predicate instead keeps the
catalog's own TTL cache as the freshness boundary for an ``isPublic``
toggle and leaves the cached permission object untouched.
"""

import logging
from typing import List, Set, Optional

from apis.shared.auth.models import User
from apis.shared.tools.freshness import get_public_tool_ids
from apis.shared.tools.scoped_ids import base_tool_id

from .models import AppRole, UserEffectivePermissions
from .repository import AppRoleRepository
from .cache import AppRoleCache, get_app_role_cache, roles_fingerprint
from apis.shared.timestamps import utc_now_iso

logger = logging.getLogger(__name__)


class AppRoleService:
    """
    Service for resolving and checking AppRole-based permissions.

    This is the main entry point for authorization checks.
    """

    def __init__(
        self,
        repository: Optional[AppRoleRepository] = None,
        cache: Optional[AppRoleCache] = None,
    ):
        """Initialize service with repository and cache."""
        self.repository = repository or AppRoleRepository()
        self.cache = cache or get_app_role_cache()

    async def resolve_user_permissions(
        self, user: User
    ) -> UserEffectivePermissions:
        """
        Resolve effective permissions for a user based on their JWT roles.

        This is the main entry point for authorization checks.

        Algorithm:
        1. Check user cache
        2. For each JWT role, find matching AppRoles
        3. If *nothing* matched, fall back to the ``default`` role
        4. Merge permissions (union for tools/models, highest priority for quota)
        5. Cache and return

        **``default`` is a fallback, not a universal role.** Step 3
        consults it only when the user matched *zero* AppRoles; it is never
        merged alongside a matched role. Prod's ``default`` additionally
        carries no ``jwtRoleMappings`` at all, so granting something there
        reaches only users who match nothing else — never the cohort roles
        (prod: ``faculty``/``staff``/``student``/``demo_day``). Comments
        across this repo previously framed feature GA as "one grant to
        ``default``, no redeploy"; that was wrong and has been corrected.
        Reaching everyone means granting to each cohort role, or changing
        ``default`` to merge as a baseline rather than substitute.

        Related: the admin roles UI builds its ``grantedTools`` control from
        the tool catalog (``admin/roles/pages/role-form.page.ts``,
        ``availableTools()``) with no free-text entry. Anything that is not a
        catalog tool — a feature-capability id, say — therefore cannot be
        granted from the UI at all, only by hand-writing DynamoDB items. That
        is what made the short-lived ``skills`` capability gate inoperable and
        got it removed; weigh it before routing a new grant through this axis.

        Args:
            user: Authenticated user with JWT roles

        Returns:
            UserEffectivePermissions with merged permissions
        """
        # Step 1: Check cache. Keyed on the role set as well as the subject —
        # two callers can share a user_id but carry different roles (see
        # ``roles_fingerprint``), and they must not read each other's entry.
        fingerprint = roles_fingerprint(user.roles)
        cached = await self.cache.get_user_permissions(user.user_id, fingerprint)
        if cached:
            logger.debug(f"Cache hit for user permissions: {user.user_id}")
            return cached

        # Step 2: Get all AppRoles that match user's JWT roles
        matching_roles: List[AppRole] = []
        jwt_roles = user.roles or []

        for jwt_role in jwt_roles:
            # Check JWT mapping cache
            role_ids = await self.cache.get_jwt_mapping(jwt_role)

            if role_ids is None:
                # Cache miss - query database
                role_ids = await self.repository.get_roles_for_jwt_role(jwt_role)
                await self.cache.set_jwt_mapping(jwt_role, role_ids)
                logger.debug(
                    f"JWT mapping cache miss for {jwt_role}, found {len(role_ids)} roles"
                )

            # Get full role objects
            for role_id in role_ids:
                role = await self._get_role_with_cache(role_id)
                if role and role.enabled:
                    matching_roles.append(role)

        # Step 3: If no roles matched, use default role
        if not matching_roles:
            default_role = await self._get_role_with_cache("default")
            if default_role and default_role.enabled:
                matching_roles = [default_role]
                logger.debug(
                    f"No matching roles for user {user.name}, using default role"
                )

        # Step 4: Merge permissions
        permissions = self._merge_permissions(user.user_id, matching_roles)

        # Step 5: Cache and return
        await self.cache.set_user_permissions(user.user_id, fingerprint, permissions)

        logger.debug(
            f"Resolved permissions for {user.name}: "
            f"roles={permissions.app_roles}, "
            f"tools={len(permissions.tools)}, "
            f"models={len(permissions.models)}"
        )

        return permissions

    async def get_role(self, role_id: str) -> Optional[AppRole]:
        """A role record, through the same cache the permission resolution uses.

        Public because the Marketplace's default pins (D9) need a role's ``priority`` and
        ``displayName`` to order and label a resolved shelf, and re-reading DynamoDB per
        pin read would drop a cache the request path already warmed. It returns the record,
        not a permission decision — pins are resolved by their own query and never enter
        ``UserEffectivePermissions``.
        """
        return await self._get_role_with_cache(role_id)

    async def _get_role_with_cache(self, role_id: str) -> Optional[AppRole]:
        """Get role from cache or database."""
        cached = await self.cache.get_role(role_id)
        if cached:
            return cached

        role = await self.repository.get_role(role_id)
        if role:
            await self.cache.set_role(role)
        return role

    def _merge_permissions(
        self, user_id: str, roles: List[AppRole]
    ) -> UserEffectivePermissions:
        """
        Merge permissions from multiple AppRoles.

        Merge rules:
        - Tools: Union (user gets access to all tools from all roles)
        - Models: Union (user gets access to all models from all roles)
        - Admin scopes: Union (but no ``"*"`` — there is no wildcard on this
          axis; full admin is the ``system_admin`` role, not a scope)
        - Quota Tier: Highest priority role's tier wins
        """
        if not roles:
            return UserEffectivePermissions(
                user_id=user_id,
                app_roles=[],
                tools=[],
                models=[],
                skills=[],
                admin_scopes=[],
                quota_tier=None,
                resolved_at=utc_now_iso(),
            )

        # Collect all tools, models and skills (union)
        all_tools: Set[str] = set()
        all_models: Set[str] = set()
        all_skills: Set[str] = set()
        all_admin_scopes: Set[str] = set()

        for role in roles:
            if role.effective_permissions:
                # Handle wildcard
                if "*" in role.effective_permissions.tools:
                    all_tools.add("*")
                else:
                    all_tools.update(role.effective_permissions.tools)

                if "*" in role.effective_permissions.models:
                    all_models.add("*")
                else:
                    all_models.update(role.effective_permissions.models)

                if "*" in role.effective_permissions.skills:
                    all_skills.add("*")
                else:
                    all_skills.update(role.effective_permissions.skills)

                # No wildcard handling: `"*"` is not a valid admin scope, so a
                # stray one is carried through as an unknown scope that matches
                # nothing rather than silently granting every admin surface.
                all_admin_scopes.update(role.effective_permissions.admin_scopes)

        # Determine quota tier (highest priority wins)
        sorted_roles = sorted(roles, key=lambda r: r.priority, reverse=True)
        quota_tier = None
        for role in sorted_roles:
            if (
                role.effective_permissions
                and role.effective_permissions.quota_tier
            ):
                quota_tier = role.effective_permissions.quota_tier
                break

        return UserEffectivePermissions(
            user_id=user_id,
            app_roles=[r.role_id for r in roles],
            # Sorted for a deterministic order across processes (set iteration
            # varies with hash randomization). These lists reach the model's
            # system prompt / tool config, where an order flip between turns
            # invalidates the Bedrock prompt cache.
            tools=sorted(all_tools),
            models=sorted(all_models),
            skills=sorted(all_skills),
            admin_scopes=sorted(all_admin_scopes),
            quota_tier=quota_tier,
            resolved_at=utc_now_iso(),
        )

    async def _tool_grant_set(self, user: User) -> Set[str]:
        """The tool ids ``user`` may invoke: their role grant ∪ every public tool.

        The one place the two grant sources combine. Every tool gate below
        goes through it so they cannot drift apart again — a ``"*"`` in the
        result still short-circuits as before.
        """
        permissions = await self.resolve_user_permissions(user)
        granted = set(permissions.tools)
        if "*" in granted:
            return granted
        return granted | set(await get_public_tool_ids())

    async def can_access_tool(self, user: User, tool_id: str) -> bool:
        """Check if user can access a specific tool.

        Exact-match on the id by design: callers pass a bare catalog id
        (an Agent's ``binding.ref``, validated against the author's palette
        at design time), never a scoped ``base::tool`` id.
        """
        allowed = await self._tool_grant_set(user)

        # Wildcard grants access to all
        if "*" in allowed:
            return True

        return tool_id in allowed

    async def can_access_model(self, user: User, model_id: str) -> bool:
        """Check if user can access a specific model."""
        permissions = await self.resolve_user_permissions(user)

        # Wildcard grants access to all
        if "*" in permissions.models:
            return True

        return model_id in permissions.models

    async def get_accessible_tools(self, user: User) -> List[str]:
        """Get list of tool IDs user can access (role grant ∪ public tools).

        Sorted, like the lists ``_merge_permissions`` builds: any tool list
        that reaches a prompt must be deterministic across processes.
        """
        return sorted(await self._tool_grant_set(user))

    async def filter_requested_tools(
        self, user: User, requested: List[str]
    ) -> List[str]:
        """Intersect a client-requested tool list with the user's RBAC grant.

        Client-supplied ``enabled_tools`` (from the SPA tool picker, a
        "Run now" body, or a schedule-creation request) must never *grant*
        access the caller's AppRole does not already carry — the picker is a
        UI convenience, not a security boundary. This narrows the request to
        what the user may actually invoke, preserving the caller's order and
        scoping (mirrors ``_apply_enabled_skills_filter``'s narrow-never-grant
        contract on the skills axis).

        A ``"*"`` grant passes everything through. A scoped id (``base::tool``)
        is allowed when its base server id is granted, so a role that grants a
        whole MCP server still admits that server's per-tool selections — and
        equally when that server is public, since a public tool is a grant.
        """
        allowed = await self._tool_grant_set(user)
        if "*" in allowed:
            return list(requested)
        return [
            tool_id
            for tool_id in requested
            if tool_id in allowed or base_tool_id(tool_id) in allowed
        ]

    async def get_accessible_models(self, user: User) -> List[str]:
        """Get list of model IDs user can access."""
        permissions = await self.resolve_user_permissions(user)
        return permissions.models

    async def get_accessible_skills(self, user: User) -> List[str]:
        """Get list of skill IDs user can access."""
        permissions = await self.resolve_user_permissions(user)
        return permissions.skills

    async def get_user_quota_tier(self, user: User) -> Optional[str]:
        """Get the quota tier for a user based on their roles."""
        permissions = await self.resolve_user_permissions(user)
        return permissions.quota_tier


# Global service instance (singleton)
_service_instance: Optional[AppRoleService] = None


def get_app_role_service() -> AppRoleService:
    """Get or create the global AppRoleService instance."""
    global _service_instance
    if _service_instance is None:
        _service_instance = AppRoleService()
    return _service_instance
