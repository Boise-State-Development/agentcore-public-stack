"""The role list every delegated admin's role pickers read.

Models, tools, skills, connectors, quota tiers, announcements and marketplace default
pins all ask "which roles can use this?", and a delegated admin of any of them needs the
role *names* to answer. The full ``GET /admin/roles/`` cannot serve them: it returns each
role's grants and ``grantedAdminScopes``, which is role administration's view, and that
surface is non-delegable (``admin.roles``, see ``roles/routes.py``).

So this module serves the narrow read instead — id, display name, description, enabled —
to anyone holding any delegable admin scope. It never writes. A picker's choice is saved
through its own domain's write-through (``set_roles_for_model``, ``set_roles_for_tool``,
…) under that domain's scope, because the AppRole record is the source of truth for a
grant (CLAUDE.md, "RBAC: the AppRole record is the source of truth").

Mounted on the ``/roles`` prefix **before** ``roles/routes.py`` (see ``admin/routes.py``):
that router's ``/{role_id}`` is a single-segment catch-all. The path is hyphenated, which
role ids can never be (``^[a-z][a-z0-9_]{2,49}$``), so no real role is shadowed by it.
"""

import logging

from fastapi import APIRouter, Depends

from apis.shared.auth import User, require_any_admin_scope
from apis.shared.rbac.admin_service import get_app_role_admin_service
from apis.shared.rbac.models import AppRoleOptionListResponse, AppRoleOptionResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/roles", tags=["admin-roles"])

require_role_picker = require_any_admin_scope()


@router.get("/picker-options", response_model=AppRoleOptionListResponse)
async def list_role_options(
    admin: User = Depends(require_role_picker),
) -> AppRoleOptionListResponse:
    """List every role as a picker needs it, highest priority first.

    Disabled roles are included with ``enabled: false`` so a picker can still name a
    role a resource is already granted to; offering only enabled roles is the
    picker's choice. Ordered by priority, then id, so the list is stable between loads
    and the marketplace default-pins page keeps its "highest priority first" order
    without the priority itself leaving the server.
    """
    roles = await get_app_role_admin_service().list_roles()
    ordered = sorted(roles, key=lambda r: (-r.priority, r.role_id))
    return AppRoleOptionListResponse(
        roles=[AppRoleOptionResponse.from_app_role(r) for r in ordered],
        total=len(ordered),
    )
