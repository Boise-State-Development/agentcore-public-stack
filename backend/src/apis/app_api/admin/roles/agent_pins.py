"""Admin API for a role's default pinned Agents (Marketplace D9, D10 — Phase 6).

Mounted under ``/admin/roles/{role_id}/agent-pins`` because **the AppRole record is the
source of truth** (CLAUDE.md's RBAC rule). If an Agent-side "which roles pin this?" picker
is ever added, it must write *through* to each role's pin items and derive the field back
on read — the ``set_roles_for_skill`` pattern — never a role list persisted on the Agent,
which would silently grant nothing.

Two things this surface must keep saying out loud:

**⚠️ ``default`` is a substitute, not a baseline (D9.6).** ``resolve_user_permissions``
consults the ``default`` role only when the user matched *zero* AppRoles, and never merges
it alongside a matched role. Pins seeded there reach only users who match nothing else — in
prod, close to nobody. ``fallbackOnly`` on the response is what lets the console say so
instead of letting an admin assume it means "everyone". ``unmapped`` is the same warning
for any role that carries no ``jwtRoleMappings`` at all.

**Removing a role pin unpins for everyone who had not pinned it themselves (D9.1).** Pins
resolve live; there is no fan-out and no "new members only". The console's removal dialog
has to say that plainly.

**Locking is bounded by friction, not by a cap (#748).** A locked seed cannot be dismissed
by the member who receives it, and an admin weighing "seed" against "seed locked" otherwise
has no reason not to lock. ``lockedElsewhere`` on the response tells the console how many
locked seeds other roles already hold, because a user's shelf is the *union* across every
role they match and a lock from any one of them wins. That union cannot be capped: role
membership resolves per user from Entra claims, so it is not knowable at write time, and
enforcing it at read time would silently drop an admin's lock for some users.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from apis.app_api.admin.agents.routes import require_marketplace_admin
from apis.app_api.agent_designer.services.role_pin_service import resolve_role_pins
from apis.shared.assistants.models import (
    RoleAgentPinsResponse,
    RoleAgentPinsUpdateRequest,
)
from apis.shared.assistants.role_pins import (
    count_locked_outside,
    list_role_pins,
    put_role_pins,
)
from apis.shared.auth import User
from apis.shared.rbac.admin_service import get_app_role_admin_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/roles", tags=["admin-role-agent-pins"])


async def _load_role(role_id: str):
    role = await get_app_role_admin_service().get_role(role_id)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Role '{role_id}' not found"
        )
    return role


async def _respond(role, admin: User) -> RoleAgentPinsResponse:
    pins = await list_role_pins(role.role_id)
    rows, unavailable = await resolve_role_pins(role, pins, admin)
    # #748 — what other roles lock. Advisory context, not a limit; see
    # ``count_locked_outside`` for why the union cannot be capped.
    locked_elsewhere, locked_elsewhere_roles = await count_locked_outside(role.role_id)
    return RoleAgentPinsResponse(
        role_id=role.role_id,
        role_label=role.display_name or role.role_id,
        # D9.6 — computed here rather than hardcoded in the SPA, so the honest label
        # travels with the data to any other surface that renders it.
        fallback_only=role.role_id == "default",
        unmapped=not role.jwt_role_mappings,
        locked_elsewhere=locked_elsewhere,
        locked_elsewhere_roles=locked_elsewhere_roles,
        pins=rows,
        unavailable=unavailable,
    )


@router.get("/{role_id}/agent-pins", response_model=RoleAgentPinsResponse)
async def get_role_agent_pins(
    role_id: str, admin: User = Depends(require_marketplace_admin)
):
    """This role's default pins, with the D9.5 assignment-time diff already applied.

    Every row carries whether the role's members can *reach* the Agent and whether the
    role grants what it binds. Rows whose Agent no longer exists are named in
    ``unavailable`` rather than pruned — a read must not rewrite curation.
    """
    try:
        return await _respond(await _load_role(role_id), admin)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error loading default pins for role {role_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to load default pins: {str(e)}")


@router.put("/{role_id}/agent-pins", response_model=RoleAgentPinsResponse)
async def put_role_agent_pins(
    role_id: str,
    request: RoleAgentPinsUpdateRequest,
    admin: User = Depends(require_marketplace_admin),
):
    """Replace this role's default pins, in order (D9).

    A whole-list PUT: ``order`` belongs to the list, so a per-row save would let two edits
    interleave into an order nobody chose.

    **Warnings do not block the save.** An unreachable Agent or a capability the role does
    not grant is reported on the response — an admin may legitimately seed something whose
    author is about to publish it, and refusing would teach people to route around the
    console. What *is* refused is a list that cannot mean what it says: past
    ``MAX_ROLE_PINS``, or naming the same Agent twice.
    """
    try:
        role = await _load_role(role_id)
        await put_role_pins(role_id, request.pins, updated_by=admin.user_id)
        logger.info(
            f"Admin {admin.email} set {len(request.pins)} default pin(s) on role {role_id}",
            extra={
                "event": "role_agent_pins_updated",
                "role_id": role_id,
                "pin_count": len(request.pins),
                "admin_user_id": admin.user_id,
            },
        )
        # No cache to invalidate: pins are read live on every ``GET /agents/pins`` (D9.1),
        # and they are deliberately absent from ``EffectivePermissions``, so nothing that
        # the AppRole cache holds has changed.
        return await _respond(role, admin)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error saving default pins for role {role_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to save default pins: {str(e)}")
