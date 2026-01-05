"""Business logic for user admin operations."""

import asyncio
import logging
import base64
import json
from typing import Optional, List
from datetime import datetime

from apis.shared.users.repository import UserRepository
from apis.shared.users.models import UserProfile, UserListItem
from apis.app_api.costs.aggregator import CostAggregator
from agents.main_agent.quota.resolver import QuotaResolver
from agents.main_agent.quota.repository import QuotaRepository
from apis.shared.auth.models import User

from .models import (
    UserListResponse,
    UserListItemResponse,
    UserDetailResponse,
    UserProfileResponse,
    QuotaStatusResponse,
    CostSummaryResponse,
    QuotaEventSummary
)

logger = logging.getLogger(__name__)


class UserAdminService:
    """Service for user admin operations."""

    def __init__(
        self,
        user_repository: UserRepository,
        cost_aggregator: CostAggregator,
        quota_resolver: QuotaResolver,
        quota_repository: QuotaRepository
    ):
        self._user_repo = user_repository
        self._cost_aggregator = cost_aggregator
        self._quota_resolver = quota_resolver
        self._quota_repo = quota_repository

    @property
    def enabled(self) -> bool:
        """Check if user admin service is enabled."""
        return self._user_repo.enabled

    async def list_users(
        self,
        status: str = "active",
        domain: Optional[str] = None,
        limit: int = 25,
        cursor: Optional[str] = None
    ) -> UserListResponse:
        """List users with filters and pagination."""
        if not self.enabled:
            return UserListResponse(users=[], next_cursor=None)

        # Decode cursor if provided
        last_key = None
        if cursor:
            try:
                last_key = json.loads(base64.b64decode(cursor).decode())
            except Exception:
                pass

        # Query based on filters
        if domain:
            users, next_key = await self._user_repo.list_users_by_domain(
                domain=domain,
                limit=limit,
                last_evaluated_key=last_key
            )
        else:
            users, next_key = await self._user_repo.list_users_by_status(
                status=status,
                limit=limit,
                last_evaluated_key=last_key
            )

        # Convert to response models
        user_responses = [
            UserListItemResponse(
                user_id=u.user_id,
                email=u.email,
                name=u.name,
                status=u.status.value if hasattr(u.status, 'value') else str(u.status),
                last_login_at=u.last_login_at,
                email_domain=u.email_domain
            )
            for u in users
        ]

        # Encode next cursor
        next_cursor = None
        if next_key:
            next_cursor = base64.b64encode(json.dumps(next_key).encode()).decode()

        return UserListResponse(
            users=user_responses,
            next_cursor=next_cursor
        )

    async def search_by_email(self, email: str) -> Optional[UserListItemResponse]:
        """Search for user by exact email."""
        if not self.enabled:
            return None

        profile = await self._user_repo.get_user_by_email(email)
        if not profile:
            return None

        return UserListItemResponse(
            user_id=profile.user_id,
            email=profile.email,
            name=profile.name,
            status=profile.status.value if hasattr(profile.status, 'value') else str(profile.status),
            last_login_at=profile.last_login_at,
            email_domain=profile.email_domain
        )

    async def get_user_detail(self, user_id: str) -> Optional[UserDetailResponse]:
        """
        Get comprehensive user detail.
        Uses UserIdIndex GSI to support admin deep links by raw user ID.
        """
        if not self.enabled:
            return None

        # Get user profile using UserIdIndex (for deep link support)
        profile = await self._user_repo.get_user_by_user_id(user_id)
        if not profile:
            return None

        # Parallel fetch of related data
        current_period = datetime.utcnow().strftime("%Y-%m")

        # Create a mock User object for quota resolution
        user = User(
            user_id=profile.user_id,
            email=profile.email,
            name=profile.name,
            roles=profile.roles
        )

        # Start all async operations
        cost_summary_coro = self._cost_aggregator.get_user_cost_summary(
            user_id=user_id,
            period=current_period
        )
        quota_coro = self._quota_resolver.resolve_user_quota(user)
        events_coro = self._quota_repo.get_user_events(
            user_id=user_id,
            limit=5
        )

        # Await all in parallel
        cost_data, resolved_quota, recent_events = await asyncio.gather(
            cost_summary_coro,
            quota_coro,
            events_coro,
            return_exceptions=True
        )

        # Build cost summary
        cost_summary = CostSummaryResponse(
            total_cost=0.0,
            total_requests=0,
            total_input_tokens=0,
            total_output_tokens=0,
            cache_savings=0.0
        )
        if cost_data and not isinstance(cost_data, Exception):
            cost_summary = CostSummaryResponse(
                total_cost=cost_data.total_cost,
                total_requests=cost_data.total_requests,
                total_input_tokens=cost_data.total_input_tokens,
                total_output_tokens=cost_data.total_output_tokens,
                cache_savings=cost_data.total_cache_savings,
                primary_model=self._get_primary_model(cost_data)
            )

        # Build quota status
        quota_status = QuotaStatusResponse()
        if resolved_quota and not isinstance(resolved_quota, Exception):
            tier = resolved_quota.tier
            usage_pct = 0.0
            remaining = None

            if tier and tier.monthly_cost_limit and tier.monthly_cost_limit != float('inf'):
                monthly_limit = float(tier.monthly_cost_limit)
                usage_pct = (cost_summary.total_cost / monthly_limit) * 100 if monthly_limit > 0 else 0.0
                remaining = max(0, monthly_limit - cost_summary.total_cost)

            quota_status = QuotaStatusResponse(
                tier_id=tier.tier_id if tier else None,
                tier_name=tier.tier_name if tier else None,
                matched_by=resolved_quota.matched_by,
                monthly_limit=float(tier.monthly_cost_limit) if tier and tier.monthly_cost_limit != float('inf') else None,
                current_usage=cost_summary.total_cost,
                usage_percentage=round(usage_pct, 1),
                remaining=remaining,
                has_active_override=resolved_quota.override is not None,
                override_reason=resolved_quota.override.reason if resolved_quota.override else None
            )

        # Build event summaries
        event_summaries = []
        if recent_events and not isinstance(recent_events, Exception):
            for event in recent_events:
                event_summaries.append(QuotaEventSummary(
                    event_id=event.event_id,
                    event_type=event.event_type,
                    timestamp=event.timestamp,
                    percentage_used=event.percentage_used
                ))

        # Build profile response
        profile_response = UserProfileResponse(
            user_id=profile.user_id,
            email=profile.email,
            name=profile.name,
            roles=profile.roles,
            picture=profile.picture,
            email_domain=profile.email_domain,
            created_at=profile.created_at,
            last_login_at=profile.last_login_at,
            status=profile.status.value if hasattr(profile.status, 'value') else str(profile.status)
        )

        return UserDetailResponse(
            profile=profile_response,
            cost_summary=cost_summary,
            quota_status=quota_status,
            recent_events=event_summaries
        )

    async def list_domains(self, limit: int = 50) -> List[str]:
        """
        List distinct email domains.
        Note: This requires a scan or maintaining a separate domain list.
        For now, return empty - implement if needed.
        """
        # TODO: Implement domain listing
        # Options:
        # 1. Maintain a separate DOMAINS item updated on user create
        # 2. Scan with projection (not recommended at scale)
        # 3. Use application-level aggregation
        return []

    def _get_primary_model(self, cost_data) -> Optional[str]:
        """Get the most-used model from cost data."""
        if not cost_data or not cost_data.models:
            return None

        # Find model with most requests
        primary = max(cost_data.models, key=lambda m: m.request_count)
        return primary.model_name if primary else None
