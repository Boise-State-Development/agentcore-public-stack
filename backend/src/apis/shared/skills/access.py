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
"""

import logging
from typing import List

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
