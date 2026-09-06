"""Quota resolver with intelligent caching."""

from typing import Optional, Dict, Tuple, List
from datetime import datetime, timedelta, timezone
import logging
from apis.shared.auth.models import User
from .models import QuotaTier, QuotaAssignment, ResolvedQuota
from .repository import QuotaRepository
from apis.shared.security.log_sanitize import scrub_log

logger = logging.getLogger(__name__)

# Import AppRoleService lazily to avoid circular imports
_app_role_service = None


def _get_app_role_service():
    """Lazy import of AppRoleService to avoid circular imports."""
    global _app_role_service
    if _app_role_service is None:
        try:
            from apis.shared.rbac.service import get_app_role_service
            _app_role_service = get_app_role_service()
        except ImportError:
            logger.warning("AppRoleService not available, AppRole quota assignments disabled")
            _app_role_service = False  # Mark as unavailable
    return _app_role_service if _app_role_service else None


class QuotaResolver:
    """
    Resolves user quota tier with intelligent caching.

    Supports overrides, direct user, AppRole, JWT role, email domain, and default tier assignments.
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
        self._domain_assignments_cache: Optional[Tuple[list, datetime]] = None

    async def resolve_user_quota(self, user: User) -> Optional[ResolvedQuota]:
        """
        Resolve quota tier for a user using priority-based matching with caching.

        Priority order (highest to lowest):
        1. Active override (highest priority)
        2. Direct user assignment (priority ~300)
        3. AppRole assignment (priority ~250)
        4. JWT role assignment (priority ~200)
        5. Email domain assignment (priority ~150)
        6. Default tier (priority ~100)
        """
        cache_key = self._get_cache_key(user)

        # Check cache
        if cache_key in self._cache:
            resolved, cached_at = self._cache[cache_key]
            if datetime.now(timezone.utc) - cached_at < timedelta(seconds=self.cache_ttl):
                logger.debug(f"Cache hit for user {user.user_id}")
                return resolved

        # Cache miss - resolve from database
        logger.debug(f"Cache miss for user {user.user_id}, resolving...")
        resolved = await self._resolve_from_db(user)

        # Cache result
        self._cache[cache_key] = (resolved, datetime.now(timezone.utc))

        return resolved

    async def _resolve_from_db(self, user: User) -> Optional[ResolvedQuota]:
        """
        Resolve quota from database using targeted GSI queries.
        ZERO table scans (uses overrides and email domains).
        """

        # 1. Check for active override (highest priority)
        override = await self.repository.get_active_override(user.user_id)
        if override:
            tier = self._override_to_tier(override)
            return ResolvedQuota(
                user_id=user.user_id,
                tier=tier,
                matched_by="override",
                assignment=None,  # Overrides don't have assignments
                override=override
            )

        # 2. Check for direct user assignment (GSI2: UserAssignmentIndex)
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

        # 3. Check AppRole assignments (GSI6: AppRoleAssignmentIndex)
        app_role_service = _get_app_role_service()
        if app_role_service:
            try:
                user_permissions = await app_role_service.resolve_user_permissions(user)
                if user_permissions and user_permissions.app_roles:
                    app_role_assignments: List[QuotaAssignment] = []
                    for app_role_id in user_permissions.app_roles:
                        # Targeted query per app role (O(log n) per role)
                        assignments = await self.repository.query_app_role_assignments(app_role_id)
                        app_role_assignments.extend(assignments)

                    if app_role_assignments:
                        # Sort by priority (descending) and take highest enabled
                        app_role_assignments.sort(key=lambda a: a.priority, reverse=True)
                        for assignment in app_role_assignments:
                            if assignment.enabled:
                                tier = await self.repository.get_tier(assignment.tier_id)
                                if tier and tier.enabled:
                                    return ResolvedQuota(
                                        user_id=user.user_id,
                                        tier=tier,
                                        matched_by=f"app_role:{assignment.app_role_id}",
                                        assignment=assignment
                                    )
            except Exception as e:
                logger.warning(f"Error resolving AppRole quota for user {user.user_id}: {e}")

        # 4. Check JWT role assignments (GSI3: RoleAssignmentIndex)
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

        # 5. Check email domain assignments
        if user.email and '@' in user.email:
            domain_assignments = await self._get_cached_domain_assignments()
            user_domain = user.email.split('@')[1]

            # Sort by priority and find matching domain
            for assignment in sorted(domain_assignments, key=lambda a: a.priority, reverse=True):
                if assignment.enabled and self._matches_email_domain(user_domain, assignment.email_domain):
                    tier = await self.repository.get_tier(assignment.tier_id)
                    if tier and tier.enabled:
                        return ResolvedQuota(
                            user_id=user.user_id,
                            tier=tier,
                            matched_by=f"email_domain:{assignment.email_domain}",
                            assignment=assignment
                        )

        # 6. Fall back to default tier (GSI1: AssignmentTypeIndex)
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
            logger.info(f"Invalidated cache for user {scrub_log(user_id)}")
        else:
            # Clear entire cache
            self._cache.clear()
            self._domain_assignments_cache = None
            logger.info("Invalidated entire quota cache")

    def _override_to_tier(self, override) -> QuotaTier:
        """Convert override to a tier for use in quota checking"""
        from .models import QuotaOverride  # Import here to avoid circular dependency

        if override.override_type == "unlimited":
            return QuotaTier(
                tier_id=f"override_{override.override_id}",
                tier_name="Unlimited Override",
                monthly_cost_limit=float('inf'),
                action_on_limit="warn",
                soft_limit_percentage=80.0,
                enabled=True,
                created_at=override.created_at,
                updated_at=override.created_at,
                created_by=override.created_by
            )
        else:  # custom_limit
            return QuotaTier(
                tier_id=f"override_{override.override_id}",
                tier_name="Custom Override",
                monthly_cost_limit=override.monthly_cost_limit or 0,
                daily_cost_limit=override.daily_cost_limit,
                action_on_limit="block",
                soft_limit_percentage=80.0,
                enabled=True,
                created_at=override.created_at,
                updated_at=override.created_at,
                created_by=override.created_by
            )

    async def _get_cached_domain_assignments(self) -> list:
        """Get domain assignments with separate cache"""
        if self._domain_assignments_cache:
            assignments, cached_at = self._domain_assignments_cache
            if datetime.now(timezone.utc) - cached_at < timedelta(seconds=self.cache_ttl):
                return assignments

        # Cache miss - query domain assignments
        assignments = await self.repository.list_assignments_by_type(
            assignment_type="email_domain",
            enabled_only=True
        )
        self._domain_assignments_cache = (assignments, datetime.now(timezone.utc))
        return assignments

    def _matches_email_domain(self, user_domain: str, pattern: str) -> bool:
        """
        Enhanced email domain matching.

        Supported patterns:
        - Exact: "university.edu"
        - Wildcard subdomain: "*.university.edu"
        - Regex: "regex:^(cs|eng)\\.university\\.edu$"
        - Multiple: "university.edu,college.edu"
        """
        if not pattern:
            return False

        # Exact match
        if pattern == user_domain:
            return True

        # Wildcard subdomain (*.example.com)
        if pattern.startswith('*.'):
            base_domain = pattern[2:]
            return user_domain == base_domain or user_domain.endswith('.' + base_domain)

        # Regex pattern (prefix with "regex:")
        if pattern.startswith('regex:'):
            import re
            regex_pattern = pattern[6:]
            try:
                return bool(re.match(regex_pattern, user_domain))
            except re.error:
                logger.error(f"Invalid regex pattern: {regex_pattern}")
                return False

        # Multiple domains (comma-separated)
        if ',' in pattern:
            domains = [d.strip() for d in pattern.split(',')]
            return any(self._matches_email_domain(user_domain, d) for d in domains)

        return False
