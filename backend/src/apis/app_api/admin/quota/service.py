"""Business logic for quota admin operations."""

from typing import Optional, List
from datetime import datetime, timedelta
import uuid
import logging
from apis.shared.auth.models import User
from apis.app_api.costs.aggregator import CostAggregator
from agents.strands_agent.quota.repository import QuotaRepository
from agents.strands_agent.quota.resolver import QuotaResolver
from agents.strands_agent.quota.models import QuotaTier, QuotaAssignment
from .models import (
    QuotaTierCreate,
    QuotaTierUpdate,
    QuotaAssignmentCreate,
    QuotaAssignmentUpdate,
    UserQuotaInfo
)

logger = logging.getLogger(__name__)


class QuotaAdminService:
    """Service layer for quota administration"""

    def __init__(
        self,
        repository: QuotaRepository,
        resolver: QuotaResolver,
        cost_aggregator: CostAggregator
    ):
        self.repository = repository
        self.resolver = resolver
        self.cost_aggregator = cost_aggregator

    # ========== Quota Tiers ==========

    async def create_tier(
        self,
        tier_data: QuotaTierCreate,
        admin_user: User
    ) -> QuotaTier:
        """Create a new quota tier"""
        # Check if tier already exists
        existing = await self.repository.get_tier(tier_data.tier_id)
        if existing:
            raise ValueError(f"Tier with ID '{tier_data.tier_id}' already exists")

        now = datetime.utcnow().isoformat() + 'Z'

        tier = QuotaTier(
            tier_id=tier_data.tier_id,
            tier_name=tier_data.tier_name,
            description=tier_data.description,
            monthly_cost_limit=tier_data.monthly_cost_limit,
            daily_cost_limit=tier_data.daily_cost_limit,
            period_type=tier_data.period_type,
            action_on_limit=tier_data.action_on_limit,
            enabled=tier_data.enabled,
            created_at=now,
            updated_at=now,
            created_by=admin_user.user_id
        )

        created = await self.repository.create_tier(tier)
        logger.info(f"Created tier {tier.tier_id} by {admin_user.user_id}")

        # Invalidate resolver cache since tier configuration changed
        self.resolver.invalidate_cache()

        return created

    async def get_tier(self, tier_id: str) -> Optional[QuotaTier]:
        """Get tier by ID"""
        return await self.repository.get_tier(tier_id)

    async def list_tiers(self, enabled_only: bool = False) -> List[QuotaTier]:
        """List all tiers"""
        return await self.repository.list_tiers(enabled_only=enabled_only)

    async def update_tier(
        self,
        tier_id: str,
        updates: QuotaTierUpdate,
        admin_user: User
    ) -> Optional[QuotaTier]:
        """Update tier (partial)"""
        # Get existing tier
        existing = await self.repository.get_tier(tier_id)
        if not existing:
            return None

        # Convert to dict and filter None values
        update_dict = updates.model_dump(by_alias=True, exclude_none=True)

        # Add audit fields
        update_dict["updatedAt"] = datetime.utcnow().isoformat() + 'Z'

        updated = await self.repository.update_tier(tier_id, update_dict)

        if updated:
            logger.info(f"Updated tier {tier_id} by {admin_user.user_id}")
            # Invalidate cache
            self.resolver.invalidate_cache()

        return updated

    async def delete_tier(
        self,
        tier_id: str,
        admin_user: User
    ) -> bool:
        """Delete tier (with validation)"""
        # Check if tier exists
        tier = await self.repository.get_tier(tier_id)
        if not tier:
            return False

        # Check if tier is in use by any assignments
        all_assignments = await self.repository.list_all_assignments()
        tier_assignments = [a for a in all_assignments if a.tier_id == tier_id]

        if tier_assignments:
            raise ValueError(
                f"Cannot delete tier '{tier_id}': it is assigned to {len(tier_assignments)} assignment(s). "
                f"Delete or reassign those assignments first."
            )

        success = await self.repository.delete_tier(tier_id)

        if success:
            logger.info(f"Deleted tier {tier_id} by {admin_user.user_id}")
            # Invalidate cache
            self.resolver.invalidate_cache()

        return success

    # ========== Quota Assignments ==========

    async def create_assignment(
        self,
        assignment_data: QuotaAssignmentCreate,
        admin_user: User
    ) -> QuotaAssignment:
        """Create a new quota assignment"""
        # Validate tier exists
        tier = await self.repository.get_tier(assignment_data.tier_id)
        if not tier:
            raise ValueError(f"Tier '{assignment_data.tier_id}' not found")

        # Validate assignment criteria
        if assignment_data.assignment_type.value == "direct_user" and not assignment_data.user_id:
            raise ValueError("user_id required for direct_user assignment")
        elif assignment_data.assignment_type.value == "jwt_role" and not assignment_data.jwt_role:
            raise ValueError("jwt_role required for jwt_role assignment")

        # Check for duplicate direct user assignment
        if assignment_data.assignment_type.value == "direct_user" and assignment_data.user_id:
            existing = await self.repository.query_user_assignment(assignment_data.user_id)
            if existing:
                raise ValueError(
                    f"User '{assignment_data.user_id}' already has a direct assignment (ID: {existing.assignment_id}). "
                    f"Update or delete the existing assignment first."
                )

        now = datetime.utcnow().isoformat() + 'Z'
        assignment_id = str(uuid.uuid4())

        assignment = QuotaAssignment(
            assignment_id=assignment_id,
            tier_id=assignment_data.tier_id,
            assignment_type=assignment_data.assignment_type,
            user_id=assignment_data.user_id,
            jwt_role=assignment_data.jwt_role,
            priority=assignment_data.priority,
            enabled=assignment_data.enabled,
            created_at=now,
            updated_at=now,
            created_by=admin_user.user_id
        )

        created = await self.repository.create_assignment(assignment)
        logger.info(
            f"Created {assignment.assignment_type.value} assignment {assignment_id} "
            f"for tier {assignment.tier_id} by {admin_user.user_id}"
        )

        # Invalidate cache for affected users
        if assignment_data.assignment_type.value == "direct_user" and assignment_data.user_id:
            self.resolver.invalidate_cache(assignment_data.user_id)
        else:
            # For role/default assignments, invalidate all cache
            self.resolver.invalidate_cache()

        return created

    async def get_assignment(self, assignment_id: str) -> Optional[QuotaAssignment]:
        """Get assignment by ID"""
        return await self.repository.get_assignment(assignment_id)

    async def list_assignments(
        self,
        assignment_type: Optional[str] = None,
        enabled_only: bool = False
    ) -> List[QuotaAssignment]:
        """List assignments (optionally filtered by type)"""
        if assignment_type:
            return await self.repository.list_assignments_by_type(
                assignment_type,
                enabled_only=enabled_only
            )
        else:
            return await self.repository.list_all_assignments(enabled_only=enabled_only)

    async def update_assignment(
        self,
        assignment_id: str,
        updates: QuotaAssignmentUpdate,
        admin_user: User
    ) -> Optional[QuotaAssignment]:
        """Update assignment (partial)"""
        existing = await self.repository.get_assignment(assignment_id)
        if not existing:
            return None

        # Validate tier if being changed
        if updates.tier_id:
            tier = await self.repository.get_tier(updates.tier_id)
            if not tier:
                raise ValueError(f"Tier '{updates.tier_id}' not found")

        # Convert to dict and filter None values
        update_dict = updates.model_dump(by_alias=True, exclude_none=True)

        updated = await self.repository.update_assignment(assignment_id, update_dict)

        if updated:
            logger.info(f"Updated assignment {assignment_id} by {admin_user.user_id}")

            # Invalidate cache for affected users
            if existing.assignment_type.value == "direct_user" and existing.user_id:
                self.resolver.invalidate_cache(existing.user_id)
            else:
                self.resolver.invalidate_cache()

        return updated

    async def delete_assignment(
        self,
        assignment_id: str,
        admin_user: User
    ) -> bool:
        """Delete assignment"""
        assignment = await self.repository.get_assignment(assignment_id)
        if not assignment:
            return False

        success = await self.repository.delete_assignment(assignment_id)

        if success:
            logger.info(f"Deleted assignment {assignment_id} by {admin_user.user_id}")

            # Invalidate cache
            if assignment.assignment_type.value == "direct_user" and assignment.user_id:
                self.resolver.invalidate_cache(assignment.user_id)
            else:
                self.resolver.invalidate_cache()

        return success

    # ========== User Quota Inspector ==========

    async def get_user_quota_info(
        self,
        user_id: str,
        email: str,
        roles: List[str]
    ) -> UserQuotaInfo:
        """Get comprehensive quota information for a user (for admin inspection)"""
        # Create User object for resolution
        user = User(
            user_id=user_id,
            email=email,
            name="",  # Not needed for resolution
            roles=roles
        )

        # Resolve quota
        resolved = await self.resolver.resolve_user_quota(user)

        # Get current usage
        now = datetime.utcnow()
        period = now.strftime("%Y-%m")

        try:
            summary = await self.cost_aggregator.get_user_cost_summary(
                user_id=user_id,
                period=period
            )
            current_usage = summary.totalCost
        except Exception as e:
            logger.error(f"Error getting cost summary: {e}")
            current_usage = 0.0

        # Calculate quota info
        if resolved:
            tier = resolved.tier
            limit = (
                tier.daily_cost_limit
                if tier.period_type == "daily" and tier.daily_cost_limit
                else tier.monthly_cost_limit
            )
            percentage_used = (current_usage / limit * 100) if limit > 0 else 0
            remaining = max(0, limit - current_usage)
        else:
            tier = None
            limit = None
            percentage_used = 0
            remaining = None

        # Get recent block events
        cutoff_time = (now - timedelta(days=1)).isoformat() + 'Z'
        recent_events = await self.repository.get_user_events(
            user_id=user_id,
            limit=100,
            start_time=cutoff_time
        )
        recent_blocks = len([e for e in recent_events if e.event_type == "block"])
        last_block_time = recent_events[0].timestamp if recent_events else None

        return UserQuotaInfo(
            user_id=user_id,
            email=email,
            roles=roles,
            tier=tier,
            assignment=resolved.assignment if resolved else None,
            matched_by=resolved.matched_by if resolved else None,
            current_period=period,
            current_usage=current_usage,
            quota_limit=limit,
            percentage_used=percentage_used,
            remaining=remaining,
            recent_blocks=recent_blocks,
            last_block_time=last_block_time
        )

    # ========== Quota Overrides ==========

    async def create_override(self, override_data, admin_user: User):
        """Create a new quota override"""
        from agents.strands_agent.quota.models import QuotaOverride
        import uuid

        override_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat() + 'Z'

        override = QuotaOverride(
            override_id=override_id,
            user_id=override_data.user_id,
            override_type=override_data.override_type,
            monthly_cost_limit=override_data.monthly_cost_limit,
            daily_cost_limit=override_data.daily_cost_limit,
            valid_from=override_data.valid_from,
            valid_until=override_data.valid_until,
            reason=override_data.reason,
            created_by=admin_user.email,
            created_at=now,
            enabled=True
        )

        created = await self.repository.create_override(override)
        logger.info(f"Created override {override_id} for user {override_data.user_id}")

        # Invalidate cache for this user
        self.resolver.invalidate_cache(user_id=override_data.user_id)

        return created

    async def get_override(self, override_id: str):
        """Get override by ID"""
        return await self.repository.get_override(override_id)

    async def list_overrides(self, user_id: Optional[str] = None, active_only: bool = False):
        """List overrides with optional filters"""
        return await self.repository.list_overrides(
            user_id=user_id,
            active_only=active_only
        )

    async def update_override(self, override_id: str, updates):
        """Update an override"""
        # Build updates dict from Pydantic model
        updates_dict = updates.model_dump(exclude_unset=True, by_alias=True)

        if not updates_dict:
            return await self.repository.get_override(override_id)

        # Get existing override to invalidate cache
        existing = await self.repository.get_override(override_id)
        if not existing:
            return None

        updated = await self.repository.update_override(override_id, updates_dict)

        if updated:
            logger.info(f"Updated override {override_id}")
            # Invalidate cache for affected user
            self.resolver.invalidate_cache(user_id=existing.user_id)

        return updated

    async def delete_override(self, override_id: str) -> bool:
        """Delete an override"""
        # Get existing override to invalidate cache
        existing = await self.repository.get_override(override_id)
        if not existing:
            return False

        success = await self.repository.delete_override(override_id)

        if success:
            logger.info(f"Deleted override {override_id}")
            # Invalidate cache for affected user
            self.resolver.invalidate_cache(user_id=existing.user_id)

        return success

    # ========== Quota Events ==========

    async def get_events(
        self,
        user_id: Optional[str] = None,
        tier_id: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 50
    ):
        """
        Get quota events with filters.

        Note: Current implementation only supports filtering by user_id or tier_id.
        Multi-filter support requires additional GSI or filtering logic.
        """
        if user_id:
            # Filter by user
            events = await self.repository.get_user_events(
                user_id=user_id,
                limit=limit
            )
        elif tier_id:
            # Filter by tier
            events = await self.repository.get_tier_events(
                tier_id=tier_id,
                limit=limit
            )
        else:
            # No filter - not efficient, return empty for now
            # TODO: Add pagination/cursor support for all events
            logger.warning("get_events called without user_id or tier_id filter")
            events = []

        # Apply event_type filter in memory if specified
        if event_type and events:
            events = [e for e in events if e.event_type == event_type]

        return events[:limit]
