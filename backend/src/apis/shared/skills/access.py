"""Per-user skill access resolution shared by app_api and the runtime.

Single source of truth for "which skills do this user's RBAC roles grant",
so the user-facing skills API (``app_api/skills``) and the inference path
(``inference_api/chat``) can never drift. Lives in ``apis.shared`` per the
import-boundary rule (both consume it; neither may import the other).
"""

import logging
from typing import List

from apis.shared.auth.models import User

logger = logging.getLogger(__name__)


async def resolve_accessible_skill_ids(user: User) -> List[str]:
    """Resolve the skills a user's RBAC roles grant (admin/DB-backed).

    A ``"*"`` wildcard grant expands to every known skill id. Never raises —
    on any failure the user simply gets no skills (the SkillAgent degrades
    to chat, the skills list renders empty).
    """
    try:
        from apis.shared.rbac.service import get_app_role_service
        from apis.shared.skills.freshness import get_all_skill_ids

        skills = await get_app_role_service().get_accessible_skills(user)
        if "*" in skills:
            return sorted(await get_all_skill_ids())
        return list(skills)
    except Exception:
        logger.warning("Failed to resolve accessible skills", exc_info=True)
        return []
