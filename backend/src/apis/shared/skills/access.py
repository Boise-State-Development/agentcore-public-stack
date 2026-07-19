"""Per-user skill access resolution shared by app_api and the runtime.

Single source of truth for "which skills can this user reach", so the
user-facing skills API (``app_api/skills``) and the inference path
(``inference_api/chat``) can never drift. Lives in ``apis.shared`` per the
import-boundary rule (both consume it; neither may import the other).

Two tiers feed the result (Skills v2 §5):

- **catalog** — admin-authored skills the user's RBAC roles grant
  (``granted_skills``, ``"*"`` honored over the catalog only).
- **own** — skills the user authored themselves (``owner_id == user_id``,
  resolved through the ``SkillOwnerIndex`` GSI). Ownership is its own grant;
  it needs no role.

Selection (``enabled_skills``) narrows this set per turn; it never widens it.

A third tier exists only *through an Agent* — **invoke-through** (§6/D7):
skills the Agent's owner authored resolve for anyone the Agent is shared with.
It deliberately does NOT live in ``resolve_accessible_skill_ids``, because that
function feeds the plain-chat picker and the design-time bindable palette; a
skill reachable only by invoking someone's Agent must never appear there. See
:func:`resolve_invocable_skill_ids`.
"""

import logging
from typing import List, Optional, Set

from apis.shared.auth.models import User

logger = logging.getLogger(__name__)


async def resolve_owned_skill_ids(user: User) -> List[str]:
    """Resolve the ACTIVE skills a user authored (the user-authored tier).

    Ownership is the grant — a user always reaches their own skills without
    any RBAC role. Never raises; on failure the user simply sees none.
    """
    if not getattr(user, "user_id", None):
        return []
    try:
        from apis.shared.skills.models import SkillStatus
        from apis.shared.skills.repository import get_skill_catalog_repository

        skills = await get_skill_catalog_repository().list_skills_by_owner(
            user.user_id, status=SkillStatus.ACTIVE.value
        )
        return [s.skill_id for s in skills]
    except Exception:
        logger.warning("Failed to resolve owned skills", exc_info=True)
        return []


async def resolve_accessible_skill_ids(user: User) -> List[str]:
    """Resolve every skill a user can reach: RBAC-granted catalog ∪ own.

    A ``"*"`` wildcard grant expands to every *catalog* skill id — never to
    another user's authored skills (see ``freshness.get_all_skill_ids``).
    Never raises — on any failure the user simply gets no skills (the agent
    runs without the disclosure plugin, the skills list renders empty).
    """
    catalog: List[str] = []
    try:
        from apis.shared.rbac.service import get_app_role_service
        from apis.shared.skills.freshness import get_all_skill_ids

        skills = await get_app_role_service().get_accessible_skills(user)
        if "*" in skills:
            catalog = sorted(await get_all_skill_ids())
        else:
            catalog = list(skills)
    except Exception:
        logger.warning("Failed to resolve accessible skills", exc_info=True)

    owned = await resolve_owned_skill_ids(user)

    # Preserve catalog order, then append owned ids the catalog didn't already
    # grant (a user may hold a role that grants a skill they also authored).
    seen = set(catalog)
    return catalog + [sid for sid in owned if sid not in seen]


async def resolve_invocable_skill_ids(
    invoker: User, requested_ids: List[str], agent_owner_id: Optional[str]
) -> Set[str]:
    """Which of ``requested_ids`` may resolve for ``invoker`` through this Agent (§6).

    The **invoke-through** predicate. A skill bound on an Agent resolves when any
    of three clauses holds:

    1. **Catalog grant** — the invoker's RBAC roles reach the skill; or
    2. **Ownership** — the invoker authored it; or
    3. **Invoke-through** — the *Agent's owner* authored it, and the invoker has
       share-access to the Agent.

    Clauses 1+2 are exactly ``resolve_accessible_skill_ids``. Note this is
    deliberately NOT a bare RBAC permission check (the removed
    ``AppRoleService.can_access_skill``): that returned ``True`` for a ``"*"``
    wildcard role against *any* id, including another user's private authored
    skill, and it had no ownership clause at all (so an author binding their own
    skill was blocked on their own invocation). Routing through the shared
    resolver expands ``"*"`` over the catalog only.

    Clause 3's **share-access half is the caller's precondition**: this is
    reached only after ``get_assistant_with_access_check`` has already admitted
    the invoker to the Agent, so owner-match is the only part left to test here.
    That owner-match is what blocks **chain-sharing** — a skill merely shared
    *to* the Agent's owner cannot be laundered to a wider audience by binding it
    and re-sharing the Agent. Invoke-through extends the owner's *own* skills
    and nothing else.

    A ``system``-owned Agent gets no clause 3: catalog skills are governed by
    RBAC alone, and an owner-match on ``"system"`` would hand the whole catalog
    to anyone who could invoke one.

    Returns the allowed subset as a set; the caller decides what a miss means
    (the binding resolver blocks the turn with the offending id, D5). Never
    raises — clause 3 degrades to "not granted" on any lookup failure.
    """
    allowed = set(await resolve_accessible_skill_ids(invoker))

    missing = [sid for sid in requested_ids if sid not in allowed]
    if not missing or not agent_owner_id:
        return allowed

    from apis.shared.skills.models import SYSTEM_OWNER_ID

    if agent_owner_id == SYSTEM_OWNER_ID:
        return allowed

    try:
        from apis.shared.skills.repository import get_skill_catalog_repository

        records = await get_skill_catalog_repository().batch_get_skills(missing)
        allowed |= {r.skill_id for r in records if r.owner_id == agent_owner_id}
    except Exception:
        logger.warning("Failed to resolve invoke-through skills", exc_info=True)

    return allowed
