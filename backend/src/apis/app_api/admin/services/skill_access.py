"""
Skill Access Service

Handles skill authorization based on AppRoles. Mirrors ToolAccessService:
a user's accessible skills come from ``resolve_user_permissions(user).skills``
(with ``"*"`` wildcard), and ``filter_allowed_skills`` intersects requested
skills against the DynamoDB-backed skill catalog.

Unlike tools, skills have no dynamically-loaded ("gateway_*") runtime form —
every skill is an authored catalog entry — so there is no prefix passthrough
in the wildcard branch.
"""
import logging
from typing import List, Optional, Set

from apis.shared.auth.models import User
from apis.shared.rbac.service import AppRoleService, get_app_role_service
from apis.shared.skills.freshness import get_all_skill_ids

logger = logging.getLogger(__name__)


class SkillAccessService:
    """Service for checking skill access based on AppRoles."""

    def __init__(self, app_role_service: Optional[AppRoleService] = None):
        """Initialize with optional AppRoleService."""
        self._app_role_service = app_role_service

    @property
    def app_role_service(self) -> AppRoleService:
        """Lazy-load AppRoleService."""
        if self._app_role_service is None:
            self._app_role_service = get_app_role_service()
        return self._app_role_service

    async def get_user_allowed_skills(self, user: User) -> Set[str]:
        """
        Get the set of skill IDs the user is allowed to use.

        Returns:
            Set of skill IDs. Contains "*" if user has wildcard access.
        """
        permissions = await self.app_role_service.resolve_user_permissions(user)
        return set(permissions.skills)

    async def can_access_skill(self, user: User, skill_id: str) -> bool:
        """
        Check if a user can access a specific skill.

        Args:
            user: The user to check
            skill_id: Skill identifier

        Returns:
            True if user has access to the skill
        """
        allowed_skills = await self.get_user_allowed_skills(user)

        # Wildcard grants access to all skills
        if "*" in allowed_skills:
            return True

        # Check if specific skill is in allowed set
        return skill_id in allowed_skills

    async def filter_allowed_skills(
        self,
        user: User,
        requested_skills: Optional[List[str]] = None,
    ) -> List[str]:
        """
        Filter a list of requested skills to only those the user can access.

        If no skills are requested, returns all skills the user can access.

        The "universe of known skills" is sourced from the DynamoDB-backed
        skill catalog (TTL-cached via ``freshness.get_all_skill_ids``).

        Args:
            user: The user to check
            requested_skills: Optional list of skill IDs the user wants to use.
                              If None, returns all allowed skills.

        Returns:
            List of skill IDs the user is allowed to use from the requested set.
        """
        allowed_skills = await self.get_user_allowed_skills(user)
        has_wildcard = "*" in allowed_skills

        # Get all available skill IDs from the DynamoDB catalog
        all_skill_ids = await get_all_skill_ids()

        if requested_skills is None:
            # No specific skills requested - return all allowed
            if has_wildcard:
                return list(all_skill_ids)
            else:
                # Only return allowed skills that exist in the catalog
                return list(allowed_skills & all_skill_ids)

        # Filter requested skills to only allowed ones
        if has_wildcard:
            # Wildcard: allow all requested skills that exist in the catalog
            return [s for s in requested_skills if s in all_skill_ids]
        else:
            # Only return intersection of requested and allowed
            return [s for s in requested_skills if s in allowed_skills]

    async def check_access_and_filter(
        self,
        user: User,
        requested_skills: Optional[List[str]] = None,
        strict: bool = False,
    ) -> tuple[List[str], List[str]]:
        """
        Check skill access and return both allowed and denied skills.

        Args:
            user: The user to check
            requested_skills: Optional list of skill IDs the user wants to use
            strict: If True, raise ValueError if any requested skill is denied

        Returns:
            Tuple of (allowed_skills, denied_skills)

        Raises:
            ValueError: If strict=True and any skills are denied
        """
        if requested_skills is None:
            allowed = await self.filter_allowed_skills(user, None)
            return allowed, []

        allowed = await self.filter_allowed_skills(user, requested_skills)
        allowed_set = set(allowed)
        denied = [s for s in requested_skills if s not in allowed_set]

        if strict and denied:
            raise ValueError(
                f"User {user.email} is not authorized to use skills: {', '.join(denied)}"
            )

        if denied:
            logger.warning(
                f"User {user.email} requested unauthorized skills: {denied}"
            )

        return allowed, denied


# Singleton instance
_skill_access_service: Optional[SkillAccessService] = None


def get_skill_access_service() -> SkillAccessService:
    """Get the singleton SkillAccessService instance."""
    global _skill_access_service
    if _skill_access_service is None:
        _skill_access_service = SkillAccessService()
    return _skill_access_service
