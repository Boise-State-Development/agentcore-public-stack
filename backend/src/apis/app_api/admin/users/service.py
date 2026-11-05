"""Business logic for user admin operations."""

import asyncio
import logging
import base64
import json
from typing import Optional, List
from datetime import datetime, timezone

from apis.shared.users.models import UserProfile
from apis.shared.users.repository import UserRepository
from apis.shared.costs.aggregator import CostAggregator
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
from .search_index import STATUS_ALL, AdminUserIndex, IndexedUser, get_admin_user_index

logger = logging.getLogger(__name__)


def _status_value(status) -> str:
    return status.value if hasattr(status, "value") else str(status)


def _encode_cursor(value: dict) -> str:
    return base64.b64encode(json.dumps(value).encode()).decode()


def _decode_cursor(cursor: Optional[str]) -> Optional[dict]:
    """The cursor's payload, or None when absent or malformed (the list restarts from the top)."""
    if not cursor:
        return None
    try:
        value = json.loads(base64.b64decode(cursor).decode())
    except Exception:
        logger.warning("Invalid pagination cursor, ignoring")
        return None
    return value if isinstance(value, dict) else None


def _profile_item(profile: UserProfile) -> UserListItemResponse:
    return UserListItemResponse(
        user_id=profile.user_id,
        email=profile.email,
        name=profile.name,
        status=_status_value(profile.status),
        last_login_at=profile.last_login_at,
        email_domain=profile.email_domain,
    )


def _indexed_item(user: IndexedUser) -> UserListItemResponse:
    return UserListItemResponse(
        user_id=user.user_id,
        email=user.email,
        name=user.name,
        status=user.status,
        last_login_at=user.last_login_at,
        email_domain=user.email_domain or None,
    )


class UserAdminService:
    """Service for user admin operations."""

    def __init__(
        self,
        user_repository: UserRepository,
        cost_aggregator: CostAggregator,
        quota_resolver: QuotaResolver,
        quota_repository: QuotaRepository,
        user_index: Optional[AdminUserIndex] = None,
    ):
        self._user_repo = user_repository
        self._cost_aggregator = cost_aggregator
        self._quota_resolver = quota_resolver
        self._quota_repo = quota_repository
        self._user_index = user_index

    @property
    def _index(self) -> AdminUserIndex:
        if self._user_index is None:
            self._user_index = get_admin_user_index()
        return self._user_index

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
        """List users with filters and pagination.

        One status with no domain is a page of ``StatusLoginIndex``: fresh and
        one query. Every status, or a domain, is served from the in-memory
        index, because no single index answers status and domain together.
        """
        if not self.enabled:
            return UserListResponse(users=[], next_cursor=None)

        if status == STATUS_ALL or domain:
            return await self.search_users("", status=status, domain=domain, limit=limit, cursor=cursor)

        last_key = _decode_cursor(cursor)
        users, next_key = await self._user_repo.list_users_by_status(
            status=status,
            limit=limit,
            last_evaluated_key=last_key if last_key and "offset" not in last_key else None,
        )

        return UserListResponse(
            users=[
                UserListItemResponse(
                    user_id=u.user_id,
                    email=u.email,
                    name=u.name,
                    status=_status_value(u.status),
                    last_login_at=u.last_login_at,
                    email_domain=u.email_domain,
                )
                for u in users
            ],
            next_cursor=_encode_cursor(next_key) if next_key else None,
        )

    async def search_users(
        self,
        query: str,
        status: str = STATUS_ALL,
        domain: Optional[str] = None,
        limit: int = 25,
        cursor: Optional[str] = None,
    ) -> UserListResponse:
        """Ranked matches on name, email or user id (see ``search_index``), paged.

        An email-shaped query also asks ``EmailIndex`` directly, so a profile
        created since the index last refreshed is still found, and every
        profile for that email is listed first with the live one on top, as
        the exact search always did.
        """
        if not self.enabled:
            return UserListResponse(users=[], next_cursor=None)

        decoded = _decode_cursor(cursor)
        offset = decoded.get("offset", 0) if isinstance(decoded, dict) else 0
        if not isinstance(offset, int) or offset < 0:
            offset = 0

        needle = query.strip().lower()
        exact: List[UserListItemResponse] = []
        if "@" in needle:
            wanted_domain = (domain or "").lower()
            exact = [
                _profile_item(p)
                for p in await self._user_repo.get_users_by_email(needle)
                if (status == STATUS_ALL or _status_value(p.status) == status)
                and (not wanted_domain or p.email_domain.lower() == wanted_domain)
            ]

        page = await asyncio.to_thread(
            self._index.search, query, status=status, domain=domain, offset=offset, limit=limit
        )
        exact_ids = {u.user_id for u in exact}
        users = [_indexed_item(u) for u in page.users if u.user_id not in exact_ids]
        if offset == 0:
            users = exact + users
        # Profiles too new for the index add to its total.
        unindexed = len(exact_ids) - self._index.count_user_ids(exact_ids) if exact_ids else 0

        return UserListResponse(
            users=users,
            next_cursor=_encode_cursor({"offset": page.next_offset}) if page.next_offset is not None else None,
            total_count=page.total + unindexed,
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
        current_period = datetime.now(timezone.utc).strftime("%Y-%m")

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
        """Distinct email domains, the most users first, read from the in-memory index."""
        if not self.enabled:
            return []
        return await asyncio.to_thread(self._index.domains, limit)

    def _get_primary_model(self, cost_data) -> Optional[str]:
        """Get the most-used model from cost data."""
        if not cost_data or not cost_data.models:
            return None

        # Find model with most requests
        primary = max(cost_data.models, key=lambda m: m.request_count)
        return primary.model_name if primary else None
