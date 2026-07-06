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
* GA for a capability = grant its id to the ``default`` role. One config
  change, no redeploy (scheduled-agent-runs.md §6, "GA path").

If capabilities outgrow this (per-capability metadata, UI surfacing), the
RBAC gap ledger already names the real fix: extend the grant vocabulary
with a first-class axis (agentic-platform-primitives.md §1, RBAC row).
"""

from __future__ import annotations

from apis.shared.auth.models import User
from apis.shared.rbac.service import get_app_role_service

#: Gates the headless-runs surface ("Run now" today; schedule CRUD in
#: Phase B). Granted to the beta cohort's role; GA = grant to ``default``.
SCHEDULED_RUNS_CAPABILITY = "scheduled-runs"


async def user_has_capability(user: User, capability_id: str) -> bool:
    """True iff ``user`` resolves the capability through their AppRoles.

    Wildcard tool grants satisfy every capability (see module docstring).
    """
    return await get_app_role_service().can_access_tool(user, capability_id)
