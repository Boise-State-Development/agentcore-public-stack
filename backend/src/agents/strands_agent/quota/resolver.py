"""Quota resolver with intelligent caching."""

from typing import Optional, Dict, Tuple
from datetime import datetime, timedelta
import logging
from apis.shared.auth.models import User
from .models import QuotaTier, QuotaAssignment, ResolvedQuota
from .repository import QuotaRepository

logger = logging.getLogger(__name__)


class QuotaResolver:
    """
    Resolves user quota tier with intelligent caching.

    Phase 1: Supports direct user, JWT role, and default tier assignments.
    Cache TTL: 5 minutes (reduces DynamoDB calls by ~90%)
    """

    def __init__(
        self,
        repository: QuotaRepository,
        cache_ttl_seconds: int = 300  # 5 minutes
    ):
        self.repository = repository
        self.cache_ttl = cache_ttl_seconds
        self._cache: Dict[str, Tuple[Optional[ResolvedQuota], datetime]] = {}

    async def resolve_user_quota(self, user: User) -> Optional[ResolvedQuota]:
        """
        Resolve quota tier for a user using priority-based matching with caching.

        Priority order (highest to lowest):
        1. Direct user assignment (priority ~300)
        2. JWT role assignment (priority ~200)
        3. Default tier (priority ~100)
        """
        cache_key = self._get_cache_key(user)

        # Check cache
        if cache_key in self._cache:
            resolved, cached_at = self._cache[cache_key]
            if datetime.utcnow() - cached_at < timedelta(seconds=self.cache_ttl):
                logger.debug(f"Cache hit for user {user.user_id}")
                return resolved

        # Cache miss - resolve from database
        logger.debug(f"Cache miss for user {user.user_id}, resolving...")
        resolved = await self._resolve_from_db(user)

        # Cache result
        self._cache[cache_key] = (resolved, datetime.utcnow())

        return resolved

    async def _resolve_from_db(self, user: User) -> Optional[ResolvedQuota]:
        """
        Resolve quota from database using targeted GSI queries.
        ZERO table scans.
        """

        # 1. Check for direct user assignment (GSI2: UserAssignmentIndex)
        user_assignment = await self.repository.query_user_assignment(user.user_id)
        if user_assignment and user_assignment.enabled:
            tier = await self.repository.get_tier(user_assignment.tier_id)
            if tier and tier.enabled:
                return ResolvedQuota(
                    user_id=user.user_id,
                    tier=tier,
                    matched_by="direct_user",
                    assignment=user_assignment
                )

        # 2. Check JWT role assignments (GSI3: RoleAssignmentIndex)
        if user.roles:
            role_assignments = []
            for role in user.roles:
                # Targeted query per role (O(log n) per role)
                assignments = await self.repository.query_role_assignments(role)
                role_assignments.extend(assignments)

            if role_assignments:
                # Sort by priority (descending) and take highest enabled
                role_assignments.sort(key=lambda a: a.priority, reverse=True)
                for assignment in role_assignments:
                    if assignment.enabled:
                        tier = await self.repository.get_tier(assignment.tier_id)
                        if tier and tier.enabled:
                            return ResolvedQuota(
                                user_id=user.user_id,
                                tier=tier,
                                matched_by=f"jwt_role:{assignment.jwt_role}",
                                assignment=assignment
                            )

        # 3. Fall back to default tier (GSI1: AssignmentTypeIndex)
        default_assignments = await self.repository.list_assignments_by_type(
            assignment_type="default_tier",
            enabled_only=True
        )
        if default_assignments:
            # Take highest priority default
            default_assignment = default_assignments[0]
            tier = await self.repository.get_tier(default_assignment.tier_id)
            if tier and tier.enabled:
                return ResolvedQuota(
                    user_id=user.user_id,
                    tier=tier,
                    matched_by="default_tier",
                    assignment=default_assignment
                )

        # No quota configured
        logger.warning(f"No quota configured for user {user.user_id}")
        return None

    def _get_cache_key(self, user: User) -> str:
        """
        Generate cache key from user attributes.

        Includes user_id and roles hash to auto-invalidate when these change.
        """
        roles_hash = hash(frozenset(user.roles)) if user.roles else 0
        return f"{user.user_id}:{roles_hash}"

    def invalidate_cache(self, user_id: Optional[str] = None):
        """Invalidate cache for specific user or all users"""
        if user_id:
            # Remove all cache entries for this user
            keys_to_remove = [k for k in self._cache.keys() if k.startswith(f"{user_id}:")]
            for key in keys_to_remove:
                del self._cache[key]
            logger.info(f"Invalidated cache for user {user_id}")
        else:
            # Clear entire cache
            self._cache.clear()
            logger.info("Invalidated entire quota cache")
