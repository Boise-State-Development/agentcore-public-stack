"""Feature-capability checks riding the AppRole grant system.

A *capability* is a feature-level permission ("may this user use scheduled
runs?") rather than a resource-level one ("may this user call this tool?").
Rather than adding a net-new allowlist table or a new grant axis for the
first capability, capability ids are granted through the mature **tools
grant axis**: an admin adds the capability id to a role's ``grantedTools``
(e.g. a ``scheduled_runs_beta`` role granting ``scheduled-runs``), and this
module checks it via the same cached RBAC resolution path every tool check
uses (scheduled-agent-runs.md §6 — "reuses the mature RBAC resolution path,
no net-new allowlist table").

Consequences to be aware of:

* A wildcard tools grant (``*`` — the seeded ``system_admin`` role) holds
  every capability implicitly. Admins are in every beta by construction.
* Capability ids share a namespace with tool ids. They never collide with
  a real tool in practice (no tool in the catalog is named like a feature),
  and a granted capability id simply matches no tool at agent-build time —
  but pick ids that read as features, not tools.
* GA for a capability is **not** one grant to the ``default`` role, despite
  what earlier comments here claimed. ``default`` is a *fallback* — resolution
  falls back to it only when a user matches zero roles (``service.py``
  "Step 3"), and in prod it carries no JWT mappings at all. Granting a
  capability there reaches only unmapped users. Reaching everyone means
  granting to each cohort role (prod: ``faculty``/``staff``/``student``/
  ``demo_day``), or changing ``default`` to merge as a baseline.
* A capability id cannot be granted from the admin roles UI: that form builds
  ``grantedTools`` from the tool catalog, and a capability is not a tool. Any
  capability gate is therefore operable only by hand-writing DynamoDB items —
  weigh that before adding one.

If capabilities outgrow this (per-capability metadata, UI surfacing), the
RBAC gap ledger already names the real fix: extend the grant vocabulary
with a first-class axis (agentic-platform-primitives.md §1, RBAC row).
"""

from __future__ import annotations

from apis.shared.auth.models import User
from apis.shared.rbac.service import get_app_role_service

#: Was intended to gate the headless-runs surface. **Currently unused**: the
#: RBAC gate on ``/schedules`` + ``/runs`` was dropped (it 403'd in prod), so
#: scheduled runs ship kill-switch-only. Kept as the worked example for the
#: grant mechanism above — note the GA caveats in the module docstring before
#: wiring it, or anything like it, to a route.
SCHEDULED_RUNS_CAPABILITY = "scheduled-runs"

#: NOTE: there is deliberately no ``skills`` capability. The user-facing skills
#: surfaces were gated on one during the v2 rollout and it was removed — see the
#: "Access model" note in ``apis.app_api.skills.routes``. The short version: a
#: capability id cannot be granted from the admin roles UI (that form builds
#: ``grantedTools`` from the tool catalog), so the gate was unoperable in
#: product. Skills are governed by ``SKILLS_ENABLED`` per environment and by a
#: role's ``grantedSkills`` per cohort, which is a complete model on its own.


async def user_has_capability(user: User, capability_id: str) -> bool:
    """True iff ``user`` resolves the capability through their AppRoles.

    Wildcard tool grants satisfy every capability (see module docstring).
    """
    return await get_app_role_service().can_access_tool(user, capability_id)
