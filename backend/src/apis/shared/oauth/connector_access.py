"""Who may use a connector: the one visibility rule every connector route reads.

A connector (OAuth provider) restricts itself with ``allowed_roles``, a list of
AppRole ids. An empty list means unrestricted. A non-empty list admits a user
who holds one of those roles **or inherits from one**.

Inheritance counts because tool access already counts it. A role that sets
``inheritsFrom: ["system_admin"]`` gets ``system_admin``'s tool grants (see
``AppRoleAdminService._compute_effective_permissions``), so its users see the
tools that need ``system_admin``'s connectors. Before this module the connector
check compared directly held roles only, so those tools were listed and then
refused at their connector: the Tools page probed each one's status and got a
403 per connector.

The rule mirrors ``_compute_effective_permissions`` exactly: one level of
inheritance, and only from an enabled parent. ``app_roles`` on the resolved
permissions is deliberately left as the directly matched roles: admin checks
read it (``"system_admin" in permissions.app_roles``), and admin power does not
inherit.
"""

import logging
from typing import FrozenSet, Iterable

from apis.shared.oauth.models import OAuthProvider
from apis.shared.rbac.models import AppRole, UserEffectivePermissions
from apis.shared.rbac.service import AppRoleService

logger = logging.getLogger(__name__)


def connector_visible_to_user(
    provider: OAuthProvider, role_ids: Iterable[str]
) -> bool:
    """True when an enabled connector is usable by a user with these role ids.

    ``role_ids`` must come from :func:`connector_role_ids`, which adds the
    roles the user inherits from; passing ``permissions.app_roles`` straight
    through is the bug this module exists to fix.
    """
    if not provider.enabled:
        return False
    if not provider.allowed_roles:
        return True
    return bool(set(provider.allowed_roles) & set(role_ids))


async def connector_role_ids(
    permissions: UserEffectivePermissions, role_service: AppRoleService
) -> FrozenSet[str]:
    """The user's matched roles plus each one's enabled direct parents.

    Role records come through the role service's cache, which resolving the
    permissions has already warmed, so this is normally free. A role that
    cannot be read contributes only itself: a failed lookup must narrow
    access, never widen it.
    """
    role_ids = set(permissions.app_roles)
    for role_id in permissions.app_roles:
        role = await _get_role(role_service, role_id)
        if role is None or not role.enabled:
            continue
        for parent_id in role.inherits_from:
            parent = await _get_role(role_service, parent_id)
            if parent is not None and parent.enabled:
                role_ids.add(parent_id)
    return frozenset(role_ids)


async def _get_role(role_service: AppRoleService, role_id: str) -> AppRole | None:
    try:
        role = await role_service.get_role(role_id)
    except Exception:
        logger.warning(
            "Role lookup failed for %s; connector access uses directly held roles",
            role_id,
            exc_info=True,
        )
        return None
    return role if isinstance(role, AppRole) else None
