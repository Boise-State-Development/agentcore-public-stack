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
is the tools-axis twin of the model-axis consolidation in
:mod:`.model_access`, which every model check (here, the ``/models`` catalog
and ``account_tools``) reads.

The public set is *not* merged into ``UserEffectivePermissions``. That
object is per-user cached and its ``tools`` list is order-sensitive
(it reaches the model's ``toolConfig``, where a flip re-writes the
prompt-cache prefix). Unioning at the predicate instead keeps the
catalog's own TTL cache as the freshness boundary for an ``isPublic``
toggle and leaves the cached permission object untouched.

**A direct user grant (``UserGrant``) IS merged into the cached object.**
It is the opposite trade: there is no other cache to lean on, and six
app-api routes read ``permissions.tools`` / ``permissions.models`` straight
off the resolved object rather than through a predicate here, so a union at
the predicate would miss them. The grant is read once per cache fill,
concurrently with the role lookups so a cold resolve pays no extra latency
on the turn path, and it has no switch of its own: a grant row can only
exist where an admin wrote one through the (flag-gated) admin surface, so an
absent row is the off state.
"""

import asyncio
import logging
from typing import List, Set, Optional, Union

from apis.shared.auth.models import User
from apis.shared.models.models import ManagedModel
from apis.shared.tools.freshness import get_public_tool_ids
from apis.shared.tools.scoped_ids import base_tool_id

from .models import AppRole, UserEffectivePermissions, UserGrant
from .repository import AppRoleRepository
from .cache import AppRoleCache, get_app_role_cache, roles_fingerprint
from .model_access import grants_model_access
from apis.shared.timestamps import utc_now_iso

logger = logging.getLogger(__name__)


class _LookupRecord:
    """Sentinel type: ``can_access_model`` should find the catalog row itself."""


LOOKUP_RECORD = _LookupRecord()


async def _lookup_model_record(model_id: str) -> Optional[ManagedModel]:
    """The catalog row for ``model_id``; ``None`` for no row or an unreadable catalog.

    Fails open to the role grants, like ``resolve_effective_model``: a catalog
    outage must not refuse every model call.
    """
    from apis.shared.models.managed_models import find_managed_model_by_model_id

    try:
        return await find_managed_model_by_model_id(model_id)
    except Exception:
        logger.warning(
            "Model catalog unavailable; checking model access on role grants only",
            exc_info=True,
        )
        return None


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

        # The direct grant is independent of the roles, so its read runs
        # alongside them: a cold resolve is already on the turn path, and
        # putting this read *after* the role loop would add its round trip
        # to time-to-first-token.
        grant_task = asyncio.ensure_future(self._load_active_grant(user.user_id))

        # Steps 2–3: the AppRoles that match the user's JWT roles
        try:
            matching_roles = await self._resolve_matching_roles(user.roles or [], user.name)
        except BaseException:
            grant_task.cancel()
            raise

        # Step 4: Merge permissions — the roles, then the user's own grant on top
        permissions = self._merge_permissions(
            user.user_id, matching_roles, grant=await grant_task
        )

        # Step 5: Cache and return
        await self.cache.set_user_permissions(user.user_id, fingerprint, permissions)

        logger.debug(
            f"Resolved permissions for {user.name}: "
            f"roles={permissions.app_roles}, "
            f"tools={len(permissions.tools)}, "
            f"models={len(permissions.models)}"
        )

        return permissions

    async def _resolve_matching_roles(
        self, jwt_roles: List[str], user_name: str
    ) -> List[AppRole]:
        """Steps 2–3 of ``resolve_user_permissions``: the enabled AppRoles the
        JWT roles map to, or ``[default]`` when none matched."""
        matching_roles: List[AppRole] = []

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
                    f"No matching roles for user {user_name}, using default role"
                )

        return matching_roles

    async def get_role(self, role_id: str) -> Optional[AppRole]:
        """A role record, through the same cache the permission resolution uses.

        Public because the Marketplace's default pins (D9) need a role's ``priority`` and
        ``displayName`` to order and label a resolved shelf, and re-reading DynamoDB per
        pin read would drop a cache the request path already warmed. It returns the record,
        not a permission decision — pins are resolved by their own query and never enter
        ``UserEffectivePermissions``.
        """
        return await self._get_role_with_cache(role_id)

    async def _load_active_grant(self, user_id: str) -> Optional[UserGrant]:
        """The user's direct grant if one exists and is in force, else ``None``.

        Fails open to "no grant": a read error must not refuse the roles the
        user already holds, and the worst case of swallowing it is that an
        exception grant lands a cache TTL late. The type check guards the
        test seam — an ``AsyncMock`` repository without this method returns
        a mock, which must read as no grant rather than as a grant.
        """
        try:
            grant = await self.repository.get_user_grant(user_id)
        except Exception:
            logger.warning(
                f"Direct grant lookup failed for {user_id}; resolving on roles only",
                exc_info=True,
            )
            return None
        if not isinstance(grant, UserGrant):
            return None
        if not grant.is_active(utc_now_iso()):
            logger.debug(f"Direct grant for {user_id} has expired; ignoring")
            return None
        return grant

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
        self,
        user_id: str,
        roles: List[AppRole],
        grant: Optional[UserGrant] = None,
    ) -> UserEffectivePermissions:
        """
        Merge permissions from multiple AppRoles, plus the user's direct grant.

        Merge rules:
        - Tools: Union (user gets access to all tools from all roles)
        - Models: Union (user gets access to all models from all roles)
        - Admin scopes: Union (but no ``"*"`` — there is no wildcard on this
          axis; full admin is the ``system_admin`` role, not a scope)
        - Quota Tier: Highest priority role's tier wins
        - Direct grant: unioned into tools/models/skills exactly like one more
          role, and recorded on ``direct_*`` so a display can attribute it.
          A ``"*"`` in a stored grant is ignored (the write path refuses it;
          this is belt and braces). It touches no other axis: quota tier has
          its own per-user assignment in the quota resolver, and admin
          scopes are delegated by role only.
        """
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

        direct_tools: List[str] = []
        direct_models: List[str] = []
        direct_skills: List[str] = []
        if grant is not None:
            direct_tools = sorted(set(grant.granted_tools) - {"*"})
            direct_models = sorted(set(grant.granted_models) - {"*"})
            direct_skills = sorted(set(grant.granted_skills) - {"*"})
            if "*" not in all_tools:
                all_tools.update(direct_tools)
            if "*" not in all_models:
                all_models.update(direct_models)
            if "*" not in all_skills:
                all_skills.update(direct_skills)

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
            direct_tools=direct_tools,
            direct_models=direct_models,
            direct_skills=direct_skills,
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
        """Check if user can access a specific tool, bare or scoped.

        ``tool_id`` may be a bare catalog id or a scoped ``base::tool`` id
        referencing one tool within an MCP server (an Agent's ``binding.ref``
        carries either). A scoped id is accessible when its **base server id**
        is granted: scoping narrows a grant, it never widens one, so a role
        that grants the whole server necessarily admits any subset of it.

        This is the same predicate ``filter_requested_tools`` applies on the
        ``enabled_tools`` axis — keep the two in agreement, or a subset the
        picker admits will be denied on the bindings axis (and vice versa).
        """
        allowed = await self._tool_grant_set(user)

        # Wildcard grants access to all
        if "*" in allowed:
            return True

        return tool_id in allowed or base_tool_id(tool_id) in allowed

    async def can_access_model(
        self,
        user: User,
        model_id: str,
        *,
        record: Union[Optional[ManagedModel], _LookupRecord] = LOOKUP_RECORD,
    ) -> bool:
        """Check if user can run ``model_id``, by the one shared rule.

        See :mod:`.model_access` for the rule, including what an id with no
        catalog row gets. Pass ``record`` when the caller already holds the
        catalog row (``EffectiveModel.record`` from retirement resolution —
        ``None`` meaning "no row"), so the turn path pays no second lookup;
        otherwise the row is found in the cached catalog.
        """
        permissions = await self.resolve_user_permissions(user)
        if isinstance(record, _LookupRecord):
            record = await _lookup_model_record(model_id)
        return grants_model_access(
            model_id, record, set(permissions.models), set(user.roles or [])
        )

    async def filter_accessible_models(
        self, user: User, models: List[ManagedModel]
    ) -> List[ManagedModel]:
        """The catalog rows ``user`` may run, by the same rule as ``can_access_model``."""
        permissions = await self.resolve_user_permissions(user)
        model_permissions = set(permissions.models)
        user_roles = set(user.roles or [])
        return [
            model
            for model in models
            if grants_model_access(model.model_id, model, model_permissions, user_roles)
        ]

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
