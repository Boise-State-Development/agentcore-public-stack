"""Tests for AdminCostService.

Covers period date ranges, top users, system summary, model usage,
tier usage (placeholder), daily trends, and the dashboard aggregator.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock

from apis.app_api.admin.costs.service import AdminCostService
from apis.app_api.admin.costs.models import (
    TopUserCost,
    SystemCostSummary,
    ModelUsageSummary,
    CostTrend,
    AdminCostDashboard,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def service(mock_storage):
    return AdminCostService(storage=mock_storage)


# ── _get_period_date_range ───────────────────────────────────────────────────


class TestGetPeriodDateRange:

    def test_january(self, service):
        assert service._get_period_date_range("2025-01") == ("2025-01-01", "2025-01-31")

    def test_february_non_leap(self, service):
        assert service._get_period_date_range("2025-02") == ("2025-02-01", "2025-02-28")

    def test_february_leap(self, service):
        assert service._get_period_date_range("2024-02") == ("2024-02-01", "2024-02-29")

    def test_december(self, service):
        assert service._get_period_date_range("2025-12") == ("2025-12-01", "2025-12-31")

    def test_april_30_days(self, service):
        assert service._get_period_date_range("2025-04") == ("2025-04-01", "2025-04-30")


# ── get_top_users ────────────────────────────────────────────────────────────


class TestGetTopUsers:

    @pytest.mark.asyncio
    async def test_returns_top_user_cost_list(self, service, mock_storage):
        mock_storage.get_top_users_by_cost.return_value = [
            {
                "userId": "user-1",
                "totalCost": 100.0,
                "totalRequests": 50,
                "lastUpdated": "2025-01-31T00:00:00Z",
            },
            {
                "userId": "user-2",
                "totalCost": 75.5,
                "totalRequests": 30,
                "lastUpdated": "2025-01-30T00:00:00Z",
            },
        ]

        result = await service.get_top_users(period="2025-01")

        assert len(result) == 2
        assert isinstance(result[0], TopUserCost)
        assert result[0].user_id == "user-1"
        assert result[0].total_cost == 100.0
        assert result[0].total_requests == 50
        assert result[1].user_id == "user-2"
        assert result[1].total_cost == 75.5

    @pytest.mark.asyncio
    async def test_enrichment_fields_are_none(self, service, mock_storage):
        mock_storage.get_top_users_by_cost.return_value = [
            {"userId": "u1", "totalCost": 10.0, "totalRequests": 5, "lastUpdated": ""},
        ]
        result = await service.get_top_users(period="2025-01")
        assert result[0].email is None
        assert result[0].tier_name is None
        assert result[0].quota_limit is None
        assert result[0].quota_percentage is None

    @pytest.mark.asyncio
    async def test_defaults_to_current_period(self, service, mock_storage):
        mock_storage.get_top_users_by_cost.return_value = []
        fixed = datetime(2025, 6, 15, tzinfo=timezone.utc)

        with patch("apis.app_api.admin.costs.service.datetime") as mock_dt:
            mock_dt.now.return_value = fixed
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await service.get_top_users()

        mock_storage.get_top_users_by_cost.assert_awaited_once_with(
            period="2025-06", limit=100, min_cost=None
        )

    @pytest.mark.asyncio
    async def test_caps_limit_at_1000(self, service, mock_storage):
        mock_storage.get_top_users_by_cost.return_value = []
        await service.get_top_users(period="2025-01", limit=5000)

        mock_storage.get_top_users_by_cost.assert_awaited_once_with(
            period="2025-01", limit=1000, min_cost=None
        )

    @pytest.mark.asyncio
    async def test_empty_storage_returns_empty_list(self, service, mock_storage):
        mock_storage.get_top_users_by_cost.return_value = []
        result = await service.get_top_users(period="2025-01")
        assert result == []


# ── get_system_summary ───────────────────────────────────────────────────────


class TestGetSystemSummary:

    @pytest.mark.asyncio
    async def test_monthly_returns_populated_summary(self, service, mock_storage):
        mock_storage.get_system_summary.return_value = {
            "totalCost": 1250.75,
            "totalRequests": 5000,
            "activeUsers": 125,
            "totalInputTokens": 1_000_000,
            "totalOutputTokens": 500_000,
            "totalCacheSavings": 50.25,
            "modelBreakdown": {"claude": {"cost": 800, "requests": 3500}},
            "lastUpdated": "2025-01-31T23:59:59Z",
        }

        result = await service.get_system_summary(period="2025-01", period_type="monthly")

        assert isinstance(result, SystemCostSummary)
        assert result.period == "2025-01"
        assert result.period_type == "monthly"
        assert result.total_cost == 1250.75
        assert result.total_requests == 5000
        assert result.active_users == 125
        assert result.total_input_tokens == 1_000_000
        assert result.total_output_tokens == 500_000
        assert result.total_cache_savings == 50.25
        assert "claude" in result.model_breakdown
        assert result.model_breakdown["claude"].cost == 800.0
        assert result.model_breakdown["claude"].requests == 3500
        assert result.last_updated == "2025-01-31T23:59:59Z"

    @pytest.mark.asyncio
    async def test_daily_defaults_to_current_date(self, service, mock_storage):
        mock_storage.get_system_summary.return_value = None
        fixed = datetime(2025, 3, 20, tzinfo=timezone.utc)

        with patch("apis.app_api.admin.costs.service.datetime") as mock_dt:
            mock_dt.now.return_value = fixed
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await service.get_system_summary(period_type="daily")

        mock_storage.get_system_summary.assert_awaited_once_with(
            period="2025-03-20", period_type="daily"
        )

    @pytest.mark.asyncio
    async def test_empty_storage_returns_zero_filled_summary(self, service, mock_storage):
        mock_storage.get_system_summary.return_value = None

        result = await service.get_system_summary(period="2025-01")

        assert result.total_cost == 0.0
        assert result.total_requests == 0
        assert result.active_users == 0
        assert result.total_input_tokens == 0
        assert result.total_output_tokens == 0
        assert result.total_cache_savings == 0.0
        assert result.model_breakdown is None

    @pytest.mark.asyncio
    async def test_empty_storage_period_preserved(self, service, mock_storage):
        mock_storage.get_system_summary.return_value = None

        result = await service.get_system_summary(period="2025-07", period_type="monthly")

        assert result.period == "2025-07"
        assert result.period_type == "monthly"


# ── get_usage_by_model ───────────────────────────────────────────────────────


class TestGetUsageByModel:

    @pytest.mark.asyncio
    async def test_returns_model_usage_list(self, service, mock_storage):
        mock_storage.get_model_usage.return_value = [
            {
                "modelId": "claude-sonnet",
                "modelName": "Claude Sonnet",
                "provider": "bedrock",
                "totalCost": 800.0,
                "totalRequests": 4000,
                "uniqueUsers": 80,
                "totalInputTokens": 500_000,
                "totalOutputTokens": 250_000,
            },
        ]

        result = await service.get_usage_by_model(period="2025-01")

        assert len(result) == 1
        assert isinstance(result[0], ModelUsageSummary)
        assert result[0].model_id == "claude-sonnet"
        assert result[0].model_name == "Claude Sonnet"
        assert result[0].provider == "bedrock"
        assert result[0].total_cost == 800.0
        assert result[0].total_requests == 4000
        assert result[0].unique_users == 80

    @pytest.mark.asyncio
    async def test_avg_cost_per_request_calculated(self, service, mock_storage):
        mock_storage.get_model_usage.return_value = [
            {
                "modelId": "m1",
                "modelName": "M1",
                "provider": "p1",
                "totalCost": 100.0,
                "totalRequests": 200,
                "uniqueUsers": 10,
                "totalInputTokens": 0,
                "totalOutputTokens": 0,
            },
        ]

        result = await service.get_usage_by_model(period="2025-01")
        assert result[0].avg_cost_per_request == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_zero_requests_gives_zero_avg(self, service, mock_storage):
        mock_storage.get_model_usage.return_value = [
            {
                "modelId": "m1",
                "modelName": "M1",
                "provider": "p1",
                "totalCost": 0.0,
                "totalRequests": 0,
                "uniqueUsers": 0,
                "totalInputTokens": 0,
                "totalOutputTokens": 0,
            },
        ]

        result = await service.get_usage_by_model(period="2025-01")
        assert result[0].avg_cost_per_request == 0.0

    @pytest.mark.asyncio
    async def test_empty_storage_returns_empty_list(self, service, mock_storage):
        mock_storage.get_model_usage.return_value = []
        result = await service.get_usage_by_model(period="2025-01")
        assert result == []

    @pytest.mark.asyncio
    async def test_missing_provider_defaults_to_unknown(self, service, mock_storage):
        mock_storage.get_model_usage.return_value = [
            {"modelId": "m1", "modelName": "M1", "totalCost": 10.0, "totalRequests": 5},
        ]
        result = await service.get_usage_by_model(period="2025-01")
        assert result[0].provider == "unknown"


# ── get_usage_by_tier ────────────────────────────────────────────────────────


class TestGetUsageByTier:

    @pytest.mark.asyncio
    async def test_returns_empty_list_placeholder(self, service):
        result = await service.get_usage_by_tier(period="2025-01")
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_without_period(self, service):
        result = await service.get_usage_by_tier()
        assert result == []


# ── get_daily_trends ─────────────────────────────────────────────────────────


class TestGetDailyTrends:

    @pytest.mark.asyncio
    async def test_returns_cost_trend_list(self, service, mock_storage):
        mock_storage.get_daily_trends.return_value = [
            {"date": "2025-01-01", "totalCost": 40.0, "totalRequests": 100, "activeUsers": 20},
            {"date": "2025-01-02", "totalCost": 45.0, "totalRequests": 110, "activeUsers": 22},
        ]

        result = await service.get_daily_trends("2025-01-01", "2025-01-02")

        assert len(result) == 2
        assert isinstance(result[0], CostTrend)
        assert result[0].date == "2025-01-01"
        assert result[0].total_cost == 40.0
        assert result[1].date == "2025-01-02"
        assert result[1].active_users == 22

    @pytest.mark.asyncio
    async def test_exceeds_90_days_limits_end_date(self, service, mock_storage):
        mock_storage.get_daily_trends.return_value = []

        await service.get_daily_trends("2025-01-01", "2025-06-01")

        mock_storage.get_daily_trends.assert_awaited_once_with(
            start_date="2025-01-01", end_date="2025-04-01"
        )

    @pytest.mark.asyncio
    async def test_exactly_90_days_not_limited(self, service, mock_storage):
        mock_storage.get_daily_trends.return_value = []

        await service.get_daily_trends("2025-01-01", "2025-04-01")

        mock_storage.get_daily_trends.assert_awaited_once_with(
            start_date="2025-01-01", end_date="2025-04-01"
        )

    @pytest.mark.asyncio
    async def test_invalid_date_format_raises_value_error(self, service):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            await service.get_daily_trends("01-01-2025", "01-31-2025")

    @pytest.mark.asyncio
    async def test_empty_storage_returns_empty_list(self, service, mock_storage):
        mock_storage.get_daily_trends.return_value = []
        result = await service.get_daily_trends("2025-01-01", "2025-01-31")
        assert result == []


# ── get_dashboard ────────────────────────────────────────────────────────────


class TestGetDashboard:

    @pytest.fixture
    def _setup_storage(self, mock_storage):
        """Pre-populate mock_storage with reasonable defaults for dashboard tests."""
        mock_storage.get_system_summary.return_value = {
            "totalCost": 500.0,
            "totalRequests": 2000,
            "activeUsers": 50,
            "totalInputTokens": 300_000,
            "totalOutputTokens": 150_000,
            "totalCacheSavings": 20.0,
            "modelBreakdown": None,
            "lastUpdated": "2025-06-15T12:00:00Z",
        }
        mock_storage.get_top_users_by_cost.return_value = [
            {"userId": "u1", "totalCost": 200.0, "totalRequests": 800, "lastUpdated": ""},
        ]
        mock_storage.get_model_usage.return_value = [
            {
                "modelId": "m1",
                "modelName": "M1",
                "provider": "bedrock",
                "totalCost": 500.0,
                "totalRequests": 2000,
                "uniqueUsers": 50,
                "totalInputTokens": 300_000,
                "totalOutputTokens": 150_000,
            },
        ]
        mock_storage.get_daily_trends.return_value = [
            {"date": "2025-06-01", "totalCost": 20.0, "totalRequests": 80, "activeUsers": 10},
        ]

    @pytest.mark.asyncio
    async def test_combines_all_sub_queries(self, service, mock_storage, _setup_storage):
        # Use a past period so end_date doesn't get capped
        result = await service.get_dashboard(period="2025-01")

        assert isinstance(result, AdminCostDashboard)
        assert isinstance(result.current_period, SystemCostSummary)
        assert len(result.top_users) == 1
        assert len(result.model_usage) == 1
        assert result.daily_trends is not None

    @pytest.mark.asyncio
    async def test_include_trends_true_fetches_trends(self, service, mock_storage, _setup_storage):
        result = await service.get_dashboard(period="2025-01", include_trends=True)

        assert result.daily_trends is not None
        mock_storage.get_daily_trends.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_include_trends_false_skips_trends(self, service, mock_storage, _setup_storage):
        result = await service.get_dashboard(period="2025-01", include_trends=False)

        assert result.daily_trends is None
        mock_storage.get_daily_trends.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tier_usage_always_none(self, service, mock_storage, _setup_storage):
        result = await service.get_dashboard(period="2025-01")
        assert result.tier_usage is None

    @pytest.mark.asyncio
    async def test_end_date_capped_to_today_for_current_month(self, service, mock_storage, _setup_storage):
        fixed = datetime(2025, 6, 15, tzinfo=timezone.utc)

        with patch("apis.app_api.admin.costs.service.datetime") as mock_dt:
            mock_dt.now.return_value = fixed
            mock_dt.strptime = datetime.strptime
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await service.get_dashboard(period="2025-06", include_trends=True)

        # end_date for June would be 2025-06-30 but should be capped to 2025-06-15
        mock_storage.get_daily_trends.assert_awaited_once_with(
            start_date="2025-06-01", end_date="2025-06-15"
        )


# ── get_top_sessions ─────────────────────────────────────────────────────────


class TestGetTopSessions:
    """The admin half of #833 PR-5: find a runaway conversation before the
    user calls about a spent quota.

    Assembled by fanning out over the period's top-cost users rather than
    scanning sessions-metadata — a session can only be expensive if its owner
    is — so these tests pin both the ranking and the honesty of the
    truncation reporting.
    """

    @pytest.fixture
    def _users_and_sessions(self, mock_storage):
        mock_storage.get_top_users_by_cost.return_value = [
            {"userId": "user-1", "totalCost": 30.45},
            {"userId": "user-2", "totalCost": 4.0},
        ]

        sessions = {
            "user-1": [
                {
                    "sessionId": "runaway",
                    "title": "Essay edits",
                    "totalCost": 36.5,
                    "lastMessageAt": "2026-08-04T22:31:43Z",
                    "partialMissCount": 48,
                    "partialMissUsd": 20.98,
                },
                {
                    "sessionId": "ordinary",
                    "totalCost": 0.12,
                    "lastMessageAt": "2026-08-02T10:00:00Z",
                },
                # Legacy row whose aggregates were never backfilled — unknown,
                # not zero, so it must not be listed as a $0 session.
                {"sessionId": "legacy", "lastMessageAt": "2026-08-01T10:00:00Z"},
            ],
            "user-2": [
                {
                    "sessionId": "middling",
                    "totalCost": 3.5,
                    "lastMessageAt": "2026-08-03T10:00:00Z",
                },
            ],
        }
        mock_storage.get_user_session_costs = AsyncMock(
            side_effect=lambda user_id, active_since=None: sessions.get(user_id, [])
        )
        return mock_storage

    @pytest.mark.asyncio
    async def test_ranks_sessions_by_cost_across_users(self, service, _users_and_sessions):
        result = await service.get_top_sessions(period="2026-08")

        assert [s.session_id for s in result.sessions] == [
            "runaway", "middling", "ordinary",
        ]
        assert result.sessions[0].total_cost == 36.5
        assert result.users_scanned == 2
        assert result.truncated is False

    @pytest.mark.asyncio
    async def test_skips_sessions_without_a_known_cost(self, service, _users_and_sessions):
        result = await service.get_top_sessions(period="2026-08")

        assert "legacy" not in [s.session_id for s in result.sessions]

    @pytest.mark.asyncio
    async def test_carries_cache_waste_and_user_share(self, service, _users_and_sessions):
        result = await service.get_top_sessions(period="2026-08")

        runaway = result.sessions[0]
        assert runaway.partial_miss_usd == 20.98
        assert runaway.user_period_cost == 30.45
        # Lifetime cost over a single period's spend can exceed 100% — the
        # conversation started before the period did, which is the shape this
        # view exists to catch.
        assert runaway.share_of_user_period == pytest.approx(119.87, abs=0.01)

    @pytest.mark.asyncio
    async def test_scopes_the_session_query_to_the_period(self, service, _users_and_sessions):
        await service.get_top_sessions(period="2026-08")

        _users_and_sessions.get_user_session_costs.assert_awaited_with(
            user_id="user-2", active_since="2026-08-01"
        )

    @pytest.mark.asyncio
    async def test_min_cost_filters_rows(self, service, _users_and_sessions):
        result = await service.get_top_sessions(period="2026-08", min_cost=1.0)

        assert [s.session_id for s in result.sessions] == ["runaway", "middling"]

    @pytest.mark.asyncio
    async def test_truncation_is_reported_not_implied(self, service, mock_storage):
        # One more user than the scan depth asks for.
        mock_storage.get_top_users_by_cost.return_value = [
            {"userId": "user-1", "totalCost": 10.0},
            {"userId": "user-2", "totalCost": 5.0},
        ]
        mock_storage.get_user_session_costs = AsyncMock(return_value=[])

        result = await service.get_top_sessions(period="2026-08", users_to_scan=1)

        assert result.truncated is True
        assert result.users_scanned == 1

    @pytest.mark.asyncio
    async def test_one_unreadable_user_does_not_empty_the_list(self, service, mock_storage):
        mock_storage.get_top_users_by_cost.return_value = [
            {"userId": "broken", "totalCost": 10.0},
            {"userId": "user-2", "totalCost": 5.0},
        ]

        async def _sessions(user_id, active_since=None):
            if user_id == "broken":
                raise Exception("throttled")
            return [{"sessionId": "kept", "totalCost": 2.0}]

        mock_storage.get_user_session_costs = AsyncMock(side_effect=_sessions)

        result = await service.get_top_sessions(period="2026-08")

        assert [s.session_id for s in result.sessions] == ["kept"]
